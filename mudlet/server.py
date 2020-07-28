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

from .util import attrdict, combine_dict, OSLineReader
import trio
import os
import errno
import json

import logging
logger = logging.getLogger(__name__)

DEFAULTS = attrdict(
        host="127.0.0.1", port=23817,
        fifo=os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "mudlet_fifo"),
        ca_certs=None, certfile=None, keyfile=None, use_reloader=False,
    )
if "XDG_RUNTIME_DIR" in os.environ:
    DEFAULTS["fifo"] = os.path.join(os.environ["XDG_RUNTIME_DIR"], "mudlet_fifo")

class _CallMudlet:
    def __init__(self, server, name = [], meth=None):
        self.server = server
        self.name = name
        self.meth = meth

    def __getattr__(self, k):
        if k.startswith("_"):
            return super().__getattr__(k)

        return _CallMudlet(self.server, self.name+[k], self.meth)

    async def __call__(self, name, *args, meth=None, dest=None):
        msg = {}
        if meth is None:
            meth = self.meth
        if meth is not None:
            msg['meth'] = meth
        if dest is not None:
            msg['dest'] = dest
        return await self.server.rpc(action="call_fn", fn=self.name, args=args, **msg)

    def __await__(self):
        return self._get().__await__()

    async def _get(self):
        if self.meth:
            raise RuntimeError("Only for method calls")
        return (await self.server.rpc(action="get", name=self.name))[0]

    async def _set(self, value):
        if self.meth:
            raise RuntimeError("Only for method calls")
        return await self.server.rpc(action="set", name=self.name, value=value)

class Server:
    _seq = 1

    def __init__(self, cfg):
        self.app = Quart(cfg['name'],
                # no, we do not want any of those default folders and whatnot
                static_folder=None,template_folder=None,root_path="/nonexistent",
                )
        self.cfg = cfg = combine_dict(cfg, DEFAULTS)
        self._handlers = {}
        self._calls = {}
        
        if 'fifo' in cfg:
            try:
                os.unlink(cfg['fifo'])
            except EnvironmentError as err:
                if err.errno != errno.ENOENT:
                    raise
            try:
                os.mkfifo(cfg['fifo'], 0o600)
            except EnvironmentError as err:
                logger.info("No FIFO. Using HTTP. (%r)", err)
                del cfg['fifo']


        @self.app.route("/test", methods=['GET'])
        async def _get_test():
            msg = json.dumps(dict(hello="This is a test."))
            return Response(msg, content_type="application/json")

        @self.app.route("/test", methods=['POST'])
        async def _echo_data():
            msg = await request.get_data()
            return Response(msg, content_type="application/x-fubar")

        # GET only fetches. PUT only sends. POST does both.

        @self.app.route("/json", methods=['GET'])
        async def _get_data():
            return await self._post_reply()

        @self.app.route("/json", methods=['PUT'])
        async def _put_data():
            msg = await request.get_data()
            if msg:
                msg = json.loads(msg)
                await self._msg_in(msg)
            msg = json.dumps([])
            return Response(msg, content_type="application/json")

        @self.app.route("/json", methods=['POST'])
        async def _post_data():
            msg = await request.get_data()
            if msg:
                msg = json.loads(msg)
                await self._msg_in(msg)
            return await self._post_reply()

    async def _post_reply(self):
        while not self._to_send:
            await self._to_send_wait.wait()
            self._to_send_wait = trio.Event()
        msg, self._to_send = self._to_send, []
        msg = json.dumps(msg)
        return Response(msg, content_type="application/json")

    async def _reader(self, task_status=trio.TASK_STATUS_IGNORED):
        if 'fifo' not in self.cfg:
            task_status.started(None)
            return
        fx = os.open(self.cfg['fifo'], os.O_RDONLY|os.O_NDELAY)
        f = OSLineReader(fx)
        task_status.started(fx)
        while True:
            line = await f.readline()
            msglen = int(line)
            msg = await f.read_all(msglen)
            msg = json.loads(msg)
            await self._msg_in(msg)

    async def _msg_in(self, msg):
        seq = msg.get("seq",None)
        if seq is not None:
            try:
                ev = self._replies[seq]
            except KeyError:
                logger.warning("Unknown Reply %r",msg)
                return
            else:
                if not isinstance(ev, trio.Event):
                    logger.warning("Dup Reply %r",msg)
                    return
            try:
                self._replies[seq] = outcome.Value(msg["result"])
            except KeyError:
                self._replies[seq] = outcome.Error(RuntimeError(msg.get("error","Unknown error")))
            ev.set()
        else:
            await self._dispatch(msg)
        
    async def _dispatch(self, msg):
        action = msg.get("action",None)
        if action:
            await getattr(self, "_action_"+action, self._action_unknown)(msg)
            return
        event = msg.get("event",None)
        if event:
            async def _disp(qq):
                qdel = set()
                for q in list(qq):
                    try:
                        q.send_nowait(msg)
                    except trio.WouldBlock:
                        await q.aclose()
                        qdel.add(q)
            await _disp(self._handlers.get(event, ()))
            await _disp(self._handlers.get("any", ()))
            return
        logger.warning("Unhandled message: %r", msg)

    async def _action_poll(self, msg):
        pass

    async def _action_unknown(self, msg):
        logger.warning("Unknown: %r", msg)
        pass

    async def _action_call(self, msg):
        seq = msg["cseq"]
        try:
            res = self._calls[msg["call"]](*msg["data"])
            if iscoroutine(res):
                res = await res
        except Exception as e:
            logger.exception("Error calling %r", msg)
            res = dict(error=str(e))
        else:
            if not isinstance(res, (list, tuple)):
                res = [res]
            res = dict(result=res)
        res["cseq"] = seq
        res["action"] = "result"
        self._send(res)

    async def _action_init(self, msg):
        res = dict(action="init")
        if 'fifo' in self.cfg:
            res['fifo'] = self.cfg['fifo']
        self._send(res)

    async def _action_up(self, msg):
        self._is_connected.set()
        self.main.start_soon(self._ping)

    async def _ping(self):
        while True:
            t1 = trio.current_time()
            with trio.fail_after(2):
                res = await self.rpc(action="ping")
            if res[0] != "Pong":
                raise ValueError(res)
            t2 = trio.current_time()
            if t2-t1 > 0.1:
                logger.info("LAG %f",t2-t1)
            await trio.sleep(10)

    @asynccontextmanager
    async def events(self, event="any"):
        """
        Listen to some event from Mudlet.
        """
        qw,qr = trio.open_memory_channel(10)
        try:
            qq = self._handlers[event]
        except KeyError:
            self._handlers[event] = qq = set()
            await self.rpc(action="handle", event=event)
        qq.add(qw)
        try:
            yield qr
        finally:
            qq.remove(qw)
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

    @asynccontextmanager
    async def _run(self) -> None:
        """
        Run this application.

        This is a simple Hypercorn runner.
        You should probably use something more elaborate in a production setting.
        """
        self._to_send = []
        self._to_send_wait = trio.Event()
        self._replies = {}
        self._is_connected = trio.Event()

        config = HyperConfig()
        cfg = self.cfg
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
        async with trio.open_nursery() as n:
            # fx is the FIFO, opened in read-write mode
            fx = await n.start(self._reader)
            n.start_soon(hyper_serve, self.app, config)
            self.main = n
            try:
                await self._is_connected.wait()
                if self._handlers:
                    async with trio.open_nursery() as nn:
                        for event in self._handlers.keys():
                            await nn.start_soon(partial(self.rpc,action="handle", event=event))
                yield self
            finally:
                if fx is not None:
                    os.close(fx)
                for k,v in self._replies.items():
                    if isinstance(v,trio.Event):
                        self._replies[k] = outcome.Error(EOFError())
                        v.set()
                n.cancel_scope.cancel()

    def _send(self, data):
        self._to_send.append(data)
        self._to_send_wait.set()
    
    async def rpc(self, **kw):
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

    @property
    def mud(self):
        return _CallMudlet(self, meth=False)

    @property
    def mmud(self):
        return _CallMudlet(self, meth=True)

    def register_call(self, name, callback):
        if name in self._calls:
            raise KeyError("Already registered")
        self._calls[name] = callback

    def unregister_call(self, name):
        del self._calls[name]

