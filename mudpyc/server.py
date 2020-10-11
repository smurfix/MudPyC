from quart_trio import QuartTrio as Quart
from quart.logging import create_serving_logger
from quart import jsonify, websocket, Response, request
from quart.exceptions import NotFound
from quart.static import send_from_directory

from hypercorn.config import Config as HyperConfig
from hypercorn.trio import serve as hyper_serve

import outcome
from contextlib import asynccontextmanager
from functools import partial
from inspect import iscoroutine
import shlex
import yaml

from .util import attrdict, combine_dict, OSLineReader, ValueEvent
from .alias import Alias
import trio
import os
import sys
import errno
import json

import logging
logger = logging.getLogger(__name__)

ALL_EVT="*"

class PostEvent(BaseException):
    """
    Raised by an action handler if the message shall be added to the event queue instead.
    This is used to serialize processing.
    """
    def __init__(self, event=None):
        self.event = event


DEFAULTS = attrdict(
        server=attrdict(
            host="127.0.0.1", port=23817,
            ca_certs=None, certfile=None, keyfile=None, use_reloader=False,
        ),
        config=os.curdir,  # profile specific configuration
    )
try:
    DEFAULTS["server"]["fifo"] = os.environ["XDG_RUNTIME_DIR"]
except KeyError:
    pass

def run_in_task(x):
    x.run_in_task = True
    return x

class _CallMudlet:
    def __init__(self, server, name = [], meth=None):
        self.server = server
        self.name = name
        self.meth = meth

    def __getattr__(self, k):
        if k.startswith("_"):
            return super().__getattr__(k)

        return _CallMudlet(self.server, self.name+[k], self.meth)

    def __getitem__(self, k):
        return _CallMudlet(self.server, self.name+[k], self.meth)

    async def __call__(self, *args, meth=None, dest=None, noreply=None):
        msg = {}
        if meth is None:
            meth = self.meth
        if meth is not None:
            msg['meth'] = meth
        if dest is not None:
            if isinstance(dest,str):
                dest = dest.split(".")
            msg['dest'] = dest
        return await self.server.rpc(action="call", name=self.name, args=args, noreply=noreply, **msg)

    def __await__(self):
        return self._get().__await__()

    async def _get(self):
        if self.meth:
            raise RuntimeError("Only for direct calls")
        res = await self.server.rpc(action="get", name=self.name)
        if not res:
            return None  # nil, Lua can't store that in a table
        return res[0]

    async def _set(self, value):
        if self.meth:
            raise RuntimeError("Only for direct calls")
        return await self.server.rpc(action="set", name=self.name, value=value)

    @property
    async def _type(self):
        if self.meth:
            raise RuntimeError("Only for direct calls")
        return await self.server.rpc(action="type", name=self.name)

    @property
    async def _nil(self):
        return "nil" == await self._type

    async def _del(self):
        if self.meth:
            raise RuntimeError("Only for non-method calls")
        await self.server.rpc(action="delete", name=self.name)

class Server:
    """
    One instance corresponds to a specific Mudlet profile.
    """
    _seq = 1

    def __init__(self, name, cfg):
        self.name = name
        self.__logger = logging.getLogger(__name__+"."+name)

        d = cfg.get('configdir', None)
        if d is not None:
            cf = os.path.join(d,name+".cfg")
            with open(cf,"r") as f:
                cfg = combine_dict(yaml.safe_load(f), cfg, cls=attrdict)
        self.cfg = cfg

        self._handlers = {}
        self._calls = {}
        self._is_running = trio.Event()

        self.fifo = None
        try:
            fifo = cfg["server"]['fifo']
        except KeyError:
            pass
        else:
            fifo = os.path.join(fifo, self.name+".fifo")
            try:
                os.unlink(fifo)
            except EnvironmentError as err:
                if err.errno != errno.ENOENT:
                    raise
            try:
                os.mkfifo(fifo, 0o600)
            except EnvironmentError as err:
                self.__logger.info("No FIFO. Using HTTP. (%r)", err)
            else:
                self.fifo = fifo

        self._to_send = []
        self._to_send_wait = trio.Event()
        self._replies = {}
        self._is_connected = trio.Event()

    @property
    async def is_running(self):
        await self._is_running.wait()

    async def process_request(self, msg):
        for m in msg:
            await self._msg_in(m)

    async def make_reply(self):
        while not self._to_send:
            await self._to_send_wait.wait()
            self._to_send_wait = trio.Event()
        msg, self._to_send = self._to_send, []
        return json.dumps(msg)

    async def _reader(self, task_status=trio.TASK_STATUS_IGNORED):
        if self.fifo is None:
            task_status.started(None)
            return

        fx = os.open(self.fifo, os.O_RDONLY|os.O_NDELAY)
        f = OSLineReader(fx)
        task_status.started(fx)
        try:
            while True:
                line = await f.readline()
                msglen = int(line)
                msg = await f.read_all(msglen)
                msg = json.loads(msg)
                try:
                    await self._msg_in(msg)
                except Exception as exc:
                    self.__logger.exception("CRASH")
        except EnvironmentError as err:
            if err.errno == errno.EBADF:
                return  # closed from outside
            raise

    async def _msg_in(self, msg):
        if msg.get("result",()) and msg["result"][0] != "Pong":
            self.__logger.debug("IN %r",msg)
        seq = msg.get("seq",None)
        if seq is not None:
            try:
                ev = self._replies[seq]
            except KeyError:
                self.__logger.warning("Unknown Reply %r",msg)
                return
            else:
                if not isinstance(ev, trio.Event):
                    self.__logger.warning("Dup Reply %r",msg)
                    return
            try:
                self._replies[seq] = outcome.Value(msg["result"])
            except KeyError:
                self._replies[seq] = outcome.Error(RuntimeError(msg.get("error","Unknown error")))
            ev.set()
        else:
            await self._dispatch(msg)
        self.__logger.debug("IN done")
        
    async def _dispatch(self, msg):
        """
        Process all messages that are not replies
        """
        action = msg.get("action",None)
        if action:
            try:
                await getattr(self, "_action_"+action, self._action_unknown)(msg)
            except PostEvent as exc:
                msg['event'] = exc.event or action
            else:
                return
        event = msg.get("event",None)
        if event:
            async def _disp(qq):
                qdel = set()
                for q in list(qq):
                    try:
                        q.send_nowait(msg)
                    except trio.ClosedResourceError:
                        await q.aclose()
                        qdel.add(q)
                if qdel:
                    qq -= qdel
            await _disp(self._handlers.get(event, ()))
            if (event != ALL_EVT):
                await _disp(self._handlers.get(ALL_EVT, ()))
            return
        self.__logger.warning("Unhandled message: %r", msg)

    async def _action_poll(self, msg):
        pass

    async def _action_unknown(self, msg):
        self.__logger.warning("Unknown: %r", msg)
        pass

    async def _action_call(self, msg):
        seq = msg.get("cseq", None)

        def done(res):
            if seq is not None:
                res["cseq"] = seq
                res["action"] = "result"
                self._send(res)

        def done_ok(res):
            if not isinstance(res, (list, tuple)):
                res = [res]
            done({"result":res})

        def done_err(err):
            done({"error":str(err)})
            if seq is None:
                self.__logger.exception("Ignored error: %r", msg, exc_info=err)
            else:
                self.__logger.warning("Error (sent to Lua): %r", msg, exc_info=err)

        async def capture(fn, data):
            try:
                res = fn(*data)
                if iscoroutine(res):
                    res = await res
            except Exception as err:
                done_err(err)
            else:
                done_ok(res)

        try:
            data = msg["data"] or []
            fn = getattr(self, "called_"+msg["call"], None)
            if fn is None:
                fn = self._calls[msg["call"]]
            if getattr(fn,"run_in_task", False):
                self.main.start_soon(capture, fn, data)
            else:
                await capture(fn, data)

        except Exception as e:
            done_err(e)


    async def _action_init(self, msg):
        profile = msg.get("profile", None)
        home = msg.get("home", None)

        res = dict(action="init")
        if self.fifo is not None:
            res['fifo'] = self.fifo
        self._send(res)

    async def _action_up(self, msg):
        self._is_connected.set()
        self.main.start_soon(self._ping)

    async def _ping(self):
        while True:
            t1 = trio.current_time()
            try:
                with trio.fail_after(5):
                    res = await self.rpc(action="ping")
            except trio.TooSlowError:
                if "pdb" in sys.modules:
                    self.__logger.error("PING ?!?")
                else:
                    raise
            if res[0] != "Pong":
                raise ValueError(res)
            t2 = trio.current_time()
            if t2-t1 > 0.1:
                self.__logger.info("LAG %f",t2-t1)
            await trio.sleep(3)
            # Mudlet may time out after 4 or 5 seconds

    @asynccontextmanager
    async def event_monitor(self, event=ALL_EVT):
        """
        Listen to some event from Mudlet.
        """
        qw,qr = trio.open_memory_channel(1000)
        try:
            qq = self._handlers[event]
        except KeyError:
            self._handlers[event] = qq = set()
            await self.rpc(action="handle", event=event)
        qq.add(qw)
        try:
            yield qr
        finally:
            with trio.move_on_after(2) as cg:
                cg.shield = True
                try:
                    qq.remove(qw)
                except KeyError:
                    pass
                await qw.aclose()
                if not qq:
                    await self.rpc(action="unhandle", event=event)
                    del self._handlers[event]

    async def __aenter__(self):
        self._mgr = mgr = self._run()
        return await mgr.__aenter__()

    async def __aexit__(self, *tb):
        try:
            return await self._mgr.__aexit__(*tb)
        finally:
            del self._mgr

    async def run(self):
        """
        Run this instance.

        This default implementation simply opens+holds the context.
        You may or may not want to do something else instead.
        """
        self.__logger.debug("Starting %s", self.name)
        async with self:
            while True:
                await trio.sleep(99999)

    @asynccontextmanager
    async def _run(self) -> None:
        """
        Context manager for this instance.
        """

        # TODO nothing sets this ... yet
        self._close = trio.Event()

        async with trio.open_nursery() as n:
            # fx is the FIFO, opened in read-write mode
            fx = await n.start(self._reader)
            self.main = n
            self._is_running.set()

            try:
                await self._is_connected.wait()
                if self._handlers:
                    async with trio.open_nursery() as nn:
                        for event in self._handlers.keys():
                            await nn.start_soon(partial(self.rpc,action="handle", event=event))
                self.do_register_aliases()
                yield self

            finally:
                if fx is not None:
                    os.close(fx)
                for k,v in self._replies.items():
                    if isinstance(v,trio.Event):
                        self._replies[k] = outcome.Error(EOFError())
                        v.set()
                n.cancel_scope.cancel()
                pass # end finally
            pass # end nursery

    def _send(self, data):
        # if data.get("action","") != "ping":
        self.__logger.debug("OUT %r",data)
        json.dumps(data)  # functional no-op but catches errors early
        self._to_send.append(data)
        self._to_send_wait.set()
    
    async def rpc(self, noreply=False, **kw):
        if noreply:
            self._send(kw)
            return
        kw['seq'] = seq = self._seq
        self._seq += 1
        self._replies[seq] = ev = trio.Event()
        self._send(kw)
        try:
            await ev.wait()
            return self._replies[seq].unwrap()
        finally:
            del self._replies[seq]

    async def event(self, name, *args):
        await self.rpc(action="event", name=name, args=args)

    async def lua_eval(self, msg, name="unknown"):
        await self.rpc(action="eval",  code=msg, name=name)

    @property
    def mud(self):
        return _CallMudlet(self, meth=False)

    @property
    def mmud(self):
        return _CallMudlet(self, meth=True)

    def register_call(self, name, callback):
        if name in self._calls:
            raise KeyError("Already registered")
        if hasattr(self, "called_"+name):
            raise KeyError("Already a function")

        self._calls[name] = callback

    def unregister_call(self, name):
        del self._calls[name]

    def do_register_aliases(self):
        self.alias = ali = Alias(self, "#", helptext=_("Alias shortcuts"))
        for k in dir(self):
            if k.startswith('alias_'):
                v = getattr(self, k)
                try:
                    k = getattr(v,"real_alias")
                except AttributeError:
                    k = k[6:]
                al = ali.at(k, create=True)
                al.helptext = v.__doc__
                al.func = v

    @run_in_task
    async def called_alias(self, cmd):
        ali = self.alias
        ocmd = cmd
        while cmd:
            if cmd[0] == " ":
                break
            try:
                ali = ali.sub[cmd[0]]
            except KeyError:
                if cmd[0] == "?":
                    await ali.print_help()
                else:
                    await ali.print_help(_("Unknown alias"), with_sub=True)
                return
            else:
                cmd = cmd[1:]
        try:
            await ali(cmd)
        except Exception as err:
            self.__logger.warning("Error in alias %r", ocmd, exc_info=err)
            await self.mud.print(_("Error: {err!r}").format(err=err))
            await self.post_error()

    async def post_error(self):
        pass

    def _cmdfix_i(self, cmd):
        return int(cmd)

    def _cmdfix_f(self, cmd):
        return float(cmd)

    def _cmdfix_w(self, cmd):
        if cmd == "":
            raise ValueError("Empty names are not allowed.")
        return cmd

    def cmdfix(self, types, cmd, min_words=0):
        """
        Split the command for an alias into words.

        Understands shell-like quoting and escapes and whatnot.
        """
        res = []
        cmd = shlex.split(cmd)

        for x in types:  
            if not cmd:
                break

            if x == "*":
                res.append(" ".join(cmd))
                return res

            v = cmd[0]
            cmd = cmd[1:]

            try:
                f = getattr(self,"_cmdfix_"+x)
            except AttributeError:
                raise RuntimeError(_("cmdfix:{x} unknown!").format(x=x))
            res.append(f(v))
        else:
            if cmd:
                raise ValueError(_("Too many parameters"))
        if len(res) < min_words:
            raise ValueError(_("Too few parameters"))
        return res


class WebServer:

    def __init__(self, cfg, factory=Server):
        self.app = Quart(cfg['name'],
                # no, we do not want any of those default folders and whatnot
                static_folder=None,template_folder=None,root_path="/nonexistent",
                )
        self.cfg = cfg = combine_dict(cfg, DEFAULTS)
        self._server = {}
        self.factory = factory
        

        @self.app.route("/test", methods=['GET'])
        async def _get_test():
            msg = json.dumps(dict(hello="This is a test."))
            return Response(msg, content_type="application/json")

        @self.app.route("/test", methods=['POST'])
        async def _echo_data():
            msg = await request.get_data()
            return Response(msg, content_type="application/x-fubar")


        # GET only sends. PUT only receives. POST does both.

        @self.app.route("/json", methods=['GET'])
        async def _get_data():
            s = await self.server
            msg = await s.make_reply()
            return Response(msg, content_type="application/json")

        @self.app.route("/json", methods=['PUT'])
        async def _put_data():
            s = await self.server
            msg = await request.get_data()
            if msg:
                msg = json.loads(msg)
                await s.process_request(msg)
            msg = json.dumps([])
            return Response(msg, content_type="application/json")

        @self.app.route("/json", methods=['POST'])
        async def _post_data():
            s = await self.server
            msg = await request.get_data()
            if msg:
                msg = json.loads(msg)
                await s.process_request(msg)
            msg = await s.make_reply()
            return Response(msg, content_type="application/json")

    def _make_server(self, name):
        return self.factory(name, self.cfg)

    @property
    async def server(self):
        sn = request.headers["Mudlet-Instance"]
        try:
            return self._server[sn]
        except KeyError:
            self._server[sn] = s = self._make_server(sn)
            self._srv_w.send_nowait(s)
        await s.is_running
        return s


    async def run_one(self, s):
        try:
            self._server[s.name] = s
            await s.run()
        except Exception as exc:
            logger.exception("Oops: %s died: %r", s.name, exc)
        finally:
            del self._server[s.name]


    async def run(self) -> None:
        """
        Run this application.

        This is a simple Hypercorn runner.
        You should probably use something more elaborate in a production setting.
        """
        config = HyperConfig()
        cfg = self.cfg['server']
        config.access_log_format = "%(h)s %(r)s %(s)s %(b)s %(D)s"
        config.access_logger = create_serving_logger()  # type: ignore
        config.bind = [f"{cfg['host']}:{cfg['port']}"]
        config.ca_certs = cfg['ca_certs']
        config.certfile = cfg['certfile']
#       if debug is not None:
#           config.debug = debug
        config.error_logger = config.access_logger  # type: ignore
        config.keyfile = cfg['keyfile']
        config.use_reloader = cfg['use_reloader']

        scheme = "http" if config.ssl_enabled is None else "https"
        self._srv_w,rdr = trio.open_memory_channel(1)

        async with trio.open_nursery() as n:
            n.start_soon(hyper_serve, self.app, config)
            async for s in rdr:
                n.start_soon(self.run_one, s)
            pass # end nursery

