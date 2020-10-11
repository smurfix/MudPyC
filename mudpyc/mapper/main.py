#!/usr/bin/python3

from mudpyc import Server, WebServer, Alias, with_alias, run_in_task, PostEvent
import trio
from mudpyc.util import ValueEvent, attrdict, combine_dict
from functools import partial
import logging
import asyncclick as click
import yaml
import json
from contextlib import asynccontextmanager, contextmanager
from collections import deque
from weakref import ref
from inspect import iscoroutine
from datetime import datetime
import re
from importlib import import_module

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from .sql import SQL, NoData
from .const import SignalThis, SkipRoute, SkipSignal, Continue
from .const import ENV_OK,ENV_STD,ENV_SPECIAL,ENV_UNMAPPED
from .walking import PathGenerator, PathChecker, CachedPathChecker, RoomFinder, LabelChecker, FulltextChecker, VisitChecker, ThingChecker, SkipFound, Continue
    
from ..util import doc, AD

import logging
logger = logging.getLogger("S")

DEFAULT_CFG=attrdict(
        logfile=None,
        name="Morgengrauen",  # so far
        sql=attrdict(
            url='mysql://user:pass@server.example.com/morgengrauen'
            ),
        settings=attrdict(
            use_mud_area = False,
            force_area = False,
            mudlet_gmcp = True,
            mudlet_delete = True,
            mudlet_explode = True,
            add_reverse = True,
            show_intermediate = True,
            dir_use_z = True,
            debug_new_room = False,
            label_shift_x = 2.0,
            label_shift_y = 0.6,
            pos_x_delta = 5,
            pos_y_delta = 5,
            pos_small_delta = 2,
            ),
        server=attrdict(), # filled in from mudlet.Server.DEFAULTS
        log=attrdict(
            level="info",
            ),
        )

SPC = re.compile(r"\s{3,}")
WORD = re.compile(r"(\w+)")
DASH = re.compile(r"- +")

TIME_DIR = ":time:"

CFG_HELP=attrdict(
        use_mud_area=_("Use the MUD's area name"),
        force_area=_("Modify existing rooms' area when visiting them"),
        mudlet_gmcp=_("Use GMCP IDs stored in Mudlet's map"),
        mudlet_delete=_("forget rooms if they're deleted in Mudlet"),
        mudlet_explode=_("Spread Mudlet rooms when they're first seen"),
        add_reverse=_("Link back when creating an exit"),
        show_intermediate=_("show movement during command sequences"),
        dir_use_z=_("Allow new rooms in Z direction"),
        debug_new_room=_("Debug room allocation"),
        label_shift_x=_("X shift, moving room labels"),
        label_shift_y=_("Y shift, moving room labels"),
        pos_x_delta=_("X offset, new rooms"),
        pos_y_delta=_("Y offset, new rooms"),
        pos_small_delta=_("Diagonal offset, new rooms"),
        )

class MappedSkipMod:
    """
    A mix-in that doesn't walk through unmapped rooms
    and which processes a skip list.

    This works both with rooms and room cache entries.
    """
    def __init__(self, skiplist=(), **kw):
        self.skiplist = skiplist
        super().__init__(**kw)

    async def check(self, room):
        if not room.id_mudlet:
            return SkipRoute

        res = await super().check(room)
        if room.id_old in self.skiplist and not res.skip:
            res = res(skip=True)
        return res

class MappedLabelSkipChecker(MappedSkipMod, SkipFound, LabelChecker):
    pass

class MappedVisitSkipChecker(MappedSkipMod, VisitChecker):
    pass

class MappedFulltextSkipChecker(MappedSkipMod, FulltextChecker):
    pass

class MappedThingSkipChecker(MappedSkipMod, ThingChecker):
    pass

class MappedRoomFinder(MappedSkipMod, RoomFinder):
    pass

class Process:
    """
    Abstract class to assemble some job to be done
    """
    _n_proc = 0
    upstack = None

    def __init__(self, server, *, stopped=False):
        self.server = server
        self.stopped = stopped
        self.commands = deque()
        self.n_moves = 0

        Process._n_proc += 1
        self._n = Process._n_proc

    def append(self, cmd):
        """
        Add this command/process to those I sent, or saw, or whatever
        """
        self.n_moves += cmd.n_moves
        self.commands.appendleft(cmd)

    @property
    def last_move(self):
        """
        Return the last-recent command in my sequence that contained (perceived) movement.
        """
        for c in self.commands:
            res = c.last_move
            if res is not None:
                return res
        return None

    
    async def setup(self):
        """
        Set myself up.
        """
        logger.debug("Setup %r", self)
        s = self.server
        self.upstack = s.process
        s.process = self
        self.upstack.append(self)
        s.trigger_sender.set()

    async def finish(self):
        """
        Clean up after myself.

        Does NOT continue the sender loop.
        """
        logger.debug("Finish %r", self)
        s = self.server
        if s.process is not self:
            raise RuntimeError("Process hook mismatch")
        s.process = self.upstack

    def _repr(self):
        res = {}
        n = self.__class__.__name__
        if n == "Process":
            res["_t"] = "?"  # shouldn't happen
        elif n.endswith("Process"):
            res["_t"] = n[:-7]
        else:
            res["_t"] = n
        res["_n"] = self._n
        if self.stopped:
            res["stop"]="y"
        if self.commands:
            res["cmds"] = len(self.commands)
        return res

    def __repr__(self):
        attrs = self._repr()
        return "P‹%s›" % (" ".join("%s=%s" % (k,v) for k,v in self._repr().items()),)

    def _go_on(self):
        """Continue the sender loop"""
        s = self.server
        if s.process is self:
            s.trigger_sender.set()

    async def pause(self):
        """Stop this process"""
        if self.stopped:
            await self.server.print(_("The process is already stopped."))
        else:
            self.stopped = True
            await self.print(_("Paused."))

    async def resume(self):
        """Continue this command"""
        if not self.stopped:
            await self.server.print(_("The process is not stopped."))
        else:
            await self.print(_("Resumed."))
            self.stopped = False
            self._go_on()

    async def next(self):
        """
        Run the next step.

        This obeys the stop flag.
        """
        if self.stopped:
            logger.debug("Next / ignore, stopped %r", self)
            return
        logger.debug("Next %r", self)
        await self.queue_next()
        logger.debug("NextDone %r", self)

    async def queue_next(self):
        """
        Run the next job.

        The default does nothing, i.e. it finishes this command and gets
        back to the next one on the list.
        """
        if self.server.process is not self:
            return

        np = self.upstack
        await self.finish()
        if np is not None and self.server.process is np:
            await np.queue_next()

class TopProcess(Process):
    """
    Top process, does nothing
    """
    def __init__(self,*a,**kw):
        kw['stopped'] = True
        super().__init__(*a,**kw)

    def _repr(self):
        res = super()._repr()
        del res["stop"]  # always true
        return res

    def append(self, cmd):
        """
        Add this command/process to those I sent, or saw, or whatever
        """
        super().append(cmd)
        if len(self.commands) > 10:
            c = self.commands.pop()
            self.n_moves -= cmd.n_moves
        self.n_moves += cmd.n_moves

    async def setup(self):
        """
        Set myself up. Does NOT call Process.setup.
        """
        s = self.server
        if s.top is not None:
            raise RuntimeError("dup Top Process")
        s.top = self
        s.process = self

    async def finish(self):
        """
        Clean up after myself.
        """
        await super().finish()
        s.top = None

    async def pause(self):
        await self.server.print(_("The top process is always 'stopped'."))

    async def resume(self):
        await self.server.print(_("There is nothing to resume."))

    async def queue_next(self):
        """
        Would run the next step if there was one, which there is not.
        """
        return


class FixProcess(Process):
    """
    Fix me: suspends the current processing.
    """
    emitted = False

    def __init__(self, server, short_reason:str, reason:str):
        self.short_reason = reason
        self.reason = reason
        super().__init__(self, server)

    def _repr(self):
        r = super()._repr()
        r["reason"] = self.short_reason
        return r

    async def queue_next(self):
        if self.emitted:
            return
        self.emitted = True
        await self.server.print(_("*** Stopped ***"))
        await self.server.print(self.reason)

class _SeqProcess(Process):
    next_seq = None

    async def setup(self):
        """
        Set myself up.
        """
        s = self.server
        if isinstance(s.process, SeqProcess):
            p = s.process
            while p.next_seq is not None:
                p = p.next_seq
            p.next_seq = self
            return
        await super().setup()

    async def finish(self):
        """
        Clean up after myself.
        """
        await super().finish()
        if self.next_seq is not None:
            await self.next_seq.setup()

class SeqProcess(_SeqProcess):
    """
    Process that runs a sequence of commands
    """
    sleeping = False
    finished = False

    def __init__(self, server, name, cmds, exit=None, room=None, send_echo=True, **kw):
        self.cmds = cmds
        self.current = 0
        self.exit = exit
        self.room = room
        self.name = name
        if self.exit is not None:
            if self.room is None:
                self.room = self.exit.src
            elif self.room != self.exit.src:
                raise RuntimeError(f"exit does not match room: {self.exit.id_str} vs {self.room.id_str}")
        self.send_echo = send_echo
        super().__init__(server, **kw)

    def _repr(self):
        r = super()._repr()
        if self.exit:
            r["exit"] = self.exit.id_str
        if self.room:
            r["room"] = self.room.id_str
        if self.sleeping:
            r["delay"] = self.sleeping
        if self.next_seq:
            r["next"] = repr(self.next_seq)
        cp = "c%02d" if len(self.cmds) > 9 else "c%d"
        for i,c in enumerate(self.cmds):
            r[cp%i] = ("*" if i == self.current else "") + repr(c)
        return r

    async def _sleep(self, d, *, task_status=trio.TASK_STATUS_IGNORED):
        s = self.server
        self.sleeping = d or 0.1
        task_state.started()
        await trio.sleep(d)
        self.sleeping = False
        self._go_on()

    async def queue_next(self):
        if self.sleeping:
            return
        s = self.server
        if self.room is not None:
            if s.room != self.room:
                self.state = _("Wait for room {nr.id_str}:{nr.name}").format(nr=self.current_room)
                await s.print(self.state)
                return
            self.room = None
        while True:
            try:
                d = self.cmds[self.current]
                self.current += 1
            except IndexError:
                break

            if isinstance(d,str):
                logger.debug("seq has %r",d)
                if not d:
                    continue
                if d[0] != '#':
                    if s.command is not None:
                        if isinstance(s.command,IdleCommand):
                            await s.command.done()
                        # else Command raises an exception. TODO?
                    d = Command(s,d)
                    d.send_echo = self.send_echo
                elif d[1:4] == "dly": # time
                    d = float(d[4:])
                else:
                    pass
                    # will fall thru to "else dunno" below

            if isinstance(d,Command):
                logger.debug("sender sends %r",d.command)
                s.send_command(d)
            elif isinstance(d,(int,float)): 
                logger.debug("sender sleeps for %s",d)
                await s.main.start(self._sleep, d)
            elif callable(d):
                logger.debug("sender calls %r",d)
                res = d()    
                if iscoroutine(res):
                    logger.debug("sender waits for %r",d)
                    await res
            else:
                await s.print(_("Dunno what to do with {d!r}"), d=d)
                continue
            return

        # Done.
        if not self.finished:
            self.finished = True
            await self.finish_move()
        await super().queue_next()

    async def finish_move(self, moved=False):
        cmd=self.name or self.cmds[-1]
        await self.server.finish_move(cmd,False)


class MoveProcess(SeqProcess):
    async def finish_move(self):
        s = self.server
        if self.exit:
            s.this_exit = self.exit
        await super().finish_move(moved=True)


class WalkProcess(Process):
    """
    Process that walks from one room to the next
    """
    def __init__(self, server, rooms, dest=None):
        self.rooms = iter(rooms)
        self.dest = dest
        super().__init__(server)
        r = next(self.rooms)
        self.current_room = r

    def _repr(self):
        r = super()._repr()
        r["now"] = self.current_room.id_str
        if self.dest:
            r["to"] = self.dest.id_str
        return r

    async def queue_next(self):
        s = self.server
        if s.room != self.current_room:
            self.state = _("Wait for room {nr.id_str}:{nr.name}").format(nr=self.current_room)
            return
        try:
            nr = next(self.rooms)
        except StopIteration:
            await super().queue_next()
        else:
            self.current_room = nr

            await self.gen_step()

    def resume(self, n):
        if not n:
            return
        while n:
            n -= 1
            try:
                r = next(self.rooms)
            except StopIteration:
                return False
        self.current_room = s.db.r_old(r)
        return True


    async def gen_step(self, stopped=False):
        """
        Queue a path's next step
        """
        s = self.server
        nr = self.current_room
        try:
            x = s.room.exit_to(nr)
        except KeyError:
            await s.print(_("No exit from {r.idn_str} to {nr.idn_str}").format(r=s.room, nr=nr))
            self.state = _("Please enter room {nr.id_str}:{nr.name}").format(nr=nr)
        else:
            self.state = _("step to {exit.dst.id_str}").format(exit=x)
            await s.send_exit_commands(x, send_echo=True)
            return True


    async def stepback(self):
        """
        We ran into a room without light or whatever.
        
        The idea of this code is to go back to the previous room, light a
        torch or whatever, and then continue.

        UNTESTED
        """
        s = self.server
        nr = self.current_room

        # queue the step forward, again
        if not await self.gen_step(stopped=True):
            await s.print(_("No step-forward from {r.idn_str} to {nr.idn_str} ??").format(r=s.room, nr=nr))
            return # Ugh

        # now queue the step back
        try:
            x = nr.exit_to(s.room)
        except KeyError:
            await s.print(_("No step-back from {r.idn_str} to {nr.idn_str}").format(r=nr, nr=s.room))
        else:
            self.state = _("stepback to {exit.dst.id_str}").format(exit=x)
            await self.send_exit_commands(x)


class BaseCommand:
    """
    Base class to collect lines from the MUD.
    """
    P_BEFORE = 0
    P_AFTER = 1

    info = None
    # None: not seen

    room = None
    # room I was in, presumably, when issuing this

    def __init__(self, server):
        if server.command is not None:
            raise RuntimeError(f"Command already running: {server.command}")
        self.server = server
        self.room = self.server.room
        self.lines = ([],[])
        self._done = trio.Event()
        self.current = self.P_BEFORE
        self.exits_text = None

    @property
    def n_moves(self):
        return 1 if self.last_move is not None else 0

    @property
    def last_move(self):
        """
        Return self if this command resulted in movement, according to the MUD.
        """
        if self.info or self.exits_text:
            return self
        return None

    def _repr(self):
        res = {}
        n = self.__class__.__name__
        if n == "Command":
            res["_t"] = "Std"
        elif n.endswith("Command"):
            res["_t"] = n[:-7]
        else:
            res["_t"] = n
        res["send"] = self.command
        if self._done.is_set:
            res["s"] = "done"
        else:
            res["s"] = ["before","after"][self.current]
        if self.exits_text:
            res["exits"] = "y"
        return res

    def __repr__(self):
        attrs = self._repr()
        return "C‹%s›" % (" ".join("%s=%s" % (k,v) for k,v in self._repr().items()),)

    def dir_seen(self, exit_matcher):
        """
        Directions.
        """
        if self.current == self.P_BEFORE:
            self.current = self.P_AFTER
            self.exits = exit_matcher
            exit_matcher.set_finish_cb(self.set_exits)
        # otherwise we have an "Outside you see:" situation
        # those exits must be ignored

    def add(self, msg):
        self.lines[self.current].append(msg)

    async def done(self):
        # prompt seen: finished
        s = self.server
        self._done.set()
        if s.command is self:
            s.command = None

    async def wait(self):
        await self._done.wait()

    async def set_info(self, info):
        """
        An Info message arrived while the command was running.
        """
        if self.info is not None:
            return False
        self.info = info
        return True

    def set_exits(self):
        pass


class IdleCommand(BaseCommand):
    """
    Something happened while you were not doing anything.

    This command auto-runs "timed_move" if it sees a new room.
    """
    location_trigger = None

    @property
    def command(self):
        return None

    def _repr(self):
        res = {}
        n = self.__class__.__name__
        res["_t"] = "‹Idle›"
        return res

    async def done(self):
        if self.info or self.exits_text:
            await self.location_seen()
        await super().done()

    async def location_seen(self):
        if self.location_trigger is not None:
            if self.location_trigger.is_set():
                return
            self.location_trigger.set()
        await self.done()
        await self.server.timed_move(info=self.info, exits_text=self.exits_text)

    async def set_info(self, info):
        """
        An Info message arrived while the command was running.
        """
        if not await super().set_info(info):
            return
        id_gmcp = info.get("id",None)
        now = self.exits_text is not None
        r = None
        db = self.server.db

        if not now:
            if id_gmcp:
                try:
                    r = db.r_hash(id_gmcp)
                except NoData:
                    pass
            if self.server.room and not r:
                try:
                    x = self.server.room.exit_at(TIME_DIR)
                except KeyError:
                    pass
                else:
                    r = x.dst
            if r and r.flag & db.Room.F_NO_GMCP_ID:
                now = True

        if now:
            self.location_trigger.set()
            await self.location_seen()
            return
        self.server.main.start_soon(self._wait_for_location)

    async def _wait_for_location(self):
        if self.location_trigger is not None:
            raise RuntimeError("Trigger already primed?")
            return
        self.location_trigger = trio.Event()
        with trio.move_on_after(0.5):
            await self.location_trigger.wait()
            return
        await self.location_seen()

    def set_exits(self):
        if not super().set_exits():
            return
        start_soon = self.server.main.start_soon
        if self.info is not None:
            self.location_trigger.set()
            start_soon(self.location_seen)
            return
        start_soon(self._wait_for_location)


class Command(BaseCommand):
    """
    Send one line to the MUD and collect whatever input you get in response.
    """
    send_seen = False
    # Echo of our own send command present?
    
    send_echo = True
    # echo this output to the user?

    def __init__(self, server, command):
        super().__init__(server)
        self.command = command

    def _repr(self):
        res = super()._repr()
        n = self.__class__.__name__
        res["send"] = self.command
        return res


class LookCommand(Command):
    def __init__(self, server, command, *, force=False):
        super().__init__(server, command)
        self.force = force

    async def done(self):
        s = self.server
        room = s.room
        db = s.db
        if self.current == self.P_BEFORE:
            await s.print("?? no descr")
            return
        old = room.long_descr.descr if room.long_descr else ""
        nld = "\n".join(self.lines[self.P_BEFORE])
        if self.force or not old:
            if room.long_descr:
                room.long_descr.descr = nld
            else:
                descr = db.LongDescr(descr=nld, room_id=room.id_old)
                db.add(descr)
            await s.print(_("Long descr updated."))
        elif old != nld:
            await s.print(_("Long descr differs."))
            logger.debug(f"""\
*** 
*** Descr: {room.idnn_str}
*** old:
{old}
*** new:
{nld}
***""")
        if self.exits_text:
            await s.process_exits_text(room, self.exits_text)

        for t in self.lines[self.P_AFTER]:
            for tt in SPC.split(t):
                room.has_thing(tt)

        db.commit()

class WordProcessor(Command):
    """
    A Command that takes your result and adds it to the room's search
    items
    """
    def __init__(self, server, room, word, command):
        super().__init__(server, command)
        self.word = word
        self.room = room

    async def done(self):
        db = self.server.db
        l = self.lines[self.P_BEFORE][:]
        if not l:
            await self.server.print(_("No data?"))
            return
        # TODO check for NPC status in last line
        await self.server.add_words(" ".join(l), room=self.room)
        if self.server.room == self.room:
            if self.word:
                if self.server.next_word and self.server.next_word.word == self.word:
                    await self.server.next_search(mark=True)
            else:
                await self.server.next_search(mark=False, again=False)

        await super().done()


class NoteProcessor(Command):
    async def done(self):
        await self.server.add_processor_note(self, self.server.room)
        await super().done()


class S(Server):

    _input_grab = None
    logfile = None

    blocked_commands = set(("lang","kurz","ultrakurz"))

    MP_TEXT = 1
    MP_ALIAS = 2
    MP_TELNET = 3
    MP_INFO = 4

    # Prompt handling
    _prompt_evt = None
    _prompt_state = None
    _send_recheck = None

    def __init__(self, name, cfg):
        super().__init__(name, cfg)
        self.dr = import_module(self.cfg['driver']).Driver(self)
        self.__logger = logging.getLogger(self.name)

    async def setup(self):
        db = self.db
        self.send_command_lock = trio.Lock()
        self.me = attrdict(blink_hp=None)

        self.trigger_sender = trio.Event()
        self.exit_matcher = None
        self.start_rooms = deque()
        self.room = None
        self.view_room = None
        self.selected_room = None  # on the map
        self.is_new_room = False
        self.last_room = None

        self.this_exit = None  # direction that we're using right now

        self.room_info = None
        self.last_room_info = None
        self.command = None

        self.top = None
        self.process = tp = TopProcess(self)
        await tp.setup()
        self.last_commands = deque()
        self.last_cmd = None  # (compound) command sent
        self.idle_info = None
        self.cmd1_q = []  # already-sent user input
        self.cmd2_q = []  # queued by commands
        self.cmd3_q = []  # user input
        self.dring_move = False

        self.next_word = None  # word to search for

        self.long_mode = True  # long
        self.long_lines = []

        self.quest = None

        #self.player_room = None
        self.path_gen = None
        self.skiplist = set()
        self.last_saved_skiplist = None
        self._text_monitors = set()

        self._text_w,rd = trio.open_memory_channel(1000)
        self.main.start_soon(self.db.cache_updater)
        self.main.start_soon(self._text_writer,rd)
        self.main.start_soon(self._send_loop)
        self.main.start_soon(self._monitor_selection)
        self.main.start_soon(self._sql_keepalive)

        #await self.set_long_mode(True)
        await self.init_gui()
        await self.init_mud()

        self._area_name2area = {}
        self._area_id2area = {}
        await self.sync_areas()

        await self.initGMCP()
        await self.mud.setCustomEnvColor(ENV_OK, 0,255,0, 255)
        await self.mud.setCustomEnvColor(ENV_STD, 0,180,180, 255)
        await self.mud.setCustomEnvColor(ENV_SPECIAL, 65,65,255, 255)
        await self.mud.setCustomEnvColor(ENV_UNMAPPED,255,225,0, 255)

        c = (await self.mud.getAllMapUserData())[0]
        if c:
            conf = {}
            for k,v in c.items():
                if k.startswith("conf."):
                    conf[k[5:]] = json.loads(v)
        else:
            conf = {}
        self.conf = combine_dict(conf, self.cfg['settings'])

        await self.dr.gmcp_initial(await self.mud.gmcp)

        self.keymap = km = self.dr.keymap
        for k in db.q(db.Keymap).all():
            if k.mod in km:
                kk = km[k.mod]
            else:
                km[k.mod] = kk = dict()
            kk[k.val] = k
        db.commit()
        print("Connected.")

    async def _monitor_selection(self):
        db = self.db
        selected = set()
        while True:
            await trio.sleep(1)
            sel = await self.mud.getMapSelection()
            if sel and sel[0]:
                try:
                    self.selected_room = db.r_mudlet(sel[0].get("center",None))
                except NoData:
                    self.selected_room = None
                sel = set(sel[0]["rooms"])
            else:
                sel = set()
                self.selected_room = None
            selected, sel = sel, selected - sel

            chg = False
            for r in sel:
                try:
                    room = db.r_mudlet(r)
                except NoData:
                    await self.print(_("Not mapped: room {id} is not known"),id=r)
                    continue
                m = await self.mud.getRoomCoordinates(r)
                if m:
                    op = [room.pos_x,room.pos_y,room.pos_z]
                    if op != m:
                        room.pos_x,room.pos_y,room.pos_z = m
                        await self.print(_("Moved: {room.idn_str}"),room=room)
                        chg = True
                else:  # deleted
                    if self.conf['mudlet_delete']:
                        await self._delete_room(room)
                        await self.print(_("Deleted: {room.idn_str}"),room=room)
                    else:
                        await self.print(_("Unmapped: -{room.id_old}: {room.idn_str}"),room=room)
                        room.id_mudlet = None
                    chg = True
            if chg:
                db.commit()

    @property
    def current_exit(self):
        if self.this_exit is not None:
            return self.this_exit
        if self.last_room is None:
            return None
        try:
            return self.last_room.exit_to(self.room)
        except KeyError:
            return None

    async def init_gui(self):
        """
        Tell Mudlet to look nice
        """
        n = 0
        for s in self.dr.lua_setup:
            n += 1
            await self.lua_eval(s, f"setup {n}")
        await self.mud.initGUI()

    async def init_mud(self):
        """
        Send mud specific commands to set the thing to something we understand
        """
        for cmd in self.dr.init_mud():
            self.send_command(cmd)

    async def post_error(self):
        self.db.rollback()
        await super().post_error()

    async def _text_writer(self, rd):
        async for line in rd:
            if isinstance(line,trio.Event):
                line.set()
            else:
                await self.mud.print(line, noreply=True)

    async def print(self, _msg, **kw):
        msg = self._format(_msg, kw)
        await self._text_w.send(msg)

    def _format(self, msg, kw):
        if kw:
            try:
                msg = msg.format(**kw)
            except Exception as exc:
                try:
                    msg = f"{msg}: {exc!r}, {kw!r}"
                    self.__logger.exception("Format %r with %r", msg,kw)
                except Exception:
                    msg = f"{msg}: {exc!r} [data not printable]"
                    self.__logger.exception("Format %r", msg)
        return msg

    @property
    def view_or_room(self):
        return self.view_room or self.room

    async def set_long_mode(self, long_mode):
        """
        Set description mode. Called by walk code. Currently a no-op
        """
        return

        if long_mode:
            ml = "lang"
        elif long_mode is False:
            ml = "kurz"
        else:
            ml = "ultrakurz"
        await self.send_commands("",ml)

    async def _sql_keepalive(self):
        """
        Small task to not let our SQL connection time out.
        """
        while True:
            await trio.sleep(600)
            self.db.db.execute("select 1")

    async def sync_areas(self):
        """
        Sync up area names+IDs in our database and in Mudlet,
        """
        seen = set()
        db=self.db

        for area in db.q(db.Area).all():
            self._area_name2area[area.name.lower()] = area
            self._area_id2area[area.id] = area
            seen.add(area.id)
        ts = (await self.mud.getAreaTableSwap())[0]
        if isinstance(ts,dict):
            ts = ts.items()
        else:
            ts = ((i+1,x) for i,x in enumerate(ts))
        for k,v in ts:
            k = int(k)
            if k in self._area_id2area:
                seen.remove(k)
                continue
            # areas in the mud but not in the database
            self.__logger.info(_("SYNC: add area {v} to DB").format(v=v))
            area = db.Area(id=k, name=v)
            db.add(area)
            self._area_name2area[area.name.lower()] = area
            self._area_id2area[area.id] = area
        for k in sorted(seen):
            # areas in the database but not in the mud
            v = self._area_id2area[k].name
            self.__logger.info(_("SYNC: add area {v} to Mudlet").format(v=v))
            kk = (await self.mud.addAreaName(v))[0]
            print("KK1",kk)
            if kk is None:
                # ugh it's actually present but hasn't been returned
                kk = (await self.mud.getRoomAreaName(v))[0]
                print("KK2",kk)
            if kk != k:
                # Ugh now we have to renumber it
                self.__logger.warning(_("SYNC: renumber area {v}: {k} > {kk}").format(kk=kk, k=k, v=v))
                del self._area_id2area[k]
                self._area_name2area[area.name.lower()] = area
                self._area_id2area[kk] = area
                area = db.q(db.Area).filter(db.Area.id == k).one()
                area.id = kk
        db.commit()


    @staticmethod
    def exits_from_line(line):
        """
        Split a text line into exit names
        """
        line = line.strip()
        line = line.rstrip(".")
        ui = line.find(" und ")
        if ui < 0:
            return [ line ]
        else:
            res = line[:ui].split(", ")
            res.append(line[ui+5:])
            return res


    def do_register_aliases(self):
        """
        Add some help textx to our sub-aliases
        """
        super().do_register_aliases()

        self.alias.at("cf").helptext = _("Change boolean settings")
        self.alias.at("co").helptext = _("Room and name positioning")
        self.alias.at("d").helptext = _("Debugging")
        self.alias.at("db").helptext = _("Python console debugging")
        self.alias.at("ds").helptext = _("Command processor debugging")
        self.alias.at("f").helptext = _("Find things, rooms")
        self.alias.at("g").helptext = _("Walking, paths")
        self.alias.at("k").helptext = _("Shortcut keys")
        self.alias.at("mc").helptext = _("Map Colors")
        self.alias.at("md").helptext = _("Mudlet functions")
        self.alias.at("m").helptext = _("Mapping")
        self.alias.at("mu").helptext = _("Find unmapped rooms/exits")
        self.alias.at("q").helptext = _("Quests")
        self.alias.at("qq").helptext = _("Quest management")
        self.alias.at("r").helptext = _("Rooms")
        self.alias.at("xf").helptext = _("Exit features")
    
    def _cmdfix_r(self,v):
        """
        cmdfix add-on that interprets room names.
        Negative=ours, positive=Mudlet's
        . current room
        : last room
        ! map viewpoint
        ? map selection center
        #n the n'th generated path
        """
        if v == ".":
            return self.room
        elif v == ":":
            return self.last_room
        elif v == "?":
            return self.selected_room or self.room
        elif v == "!":
            return self.view_or_room
        elif v[0] == '#':
            return self.path_gen.results[int(v[1:])-1][0]
        v = int(v)
        if not v:
            # zero
            return None
        if v < 0:
            return self.db.r_old(-v)
        else:
            return self.db.r_mudlet(v)

    def _cmdfix_x(self,v):
        """
        cmdfix add-on that interprets exits, by canonicalizing them.
        """
        return self.dr.short2loc(v)

    @doc(_(
        """
        Configuration.

        Shows config data. No parameters.
        """))
    async def alias_c(self, cmd):
        if cmd:
            try:
                v = self.conf[cmd]
            except KeyError:
                await self.print(_("Config item '{cmd}' unknown."), cmd=cmd)
            else:
                await self.print(_("{cmd} = {v}."), v=v, cmd=cmd)

        else:
            for k,vt in DEFAULT_CFG.settings.items():
                # we do it this way because dicts are sorted
                v = self.conf[k]
                if isinstance(vt,bool):
                    v=_("ON") if v else _("off")
                await self.print(f"{str(v):>5} = {CFG_HELP[k]}")

    @doc(_(
        """
        Remove rooms from the database if they're deleted in Mudlet?
        """))
    async def alias_cfx(self, cmd):
        await self._conf_flip("mudlet_delete")

    @doc(_(
        """
        Remove rooms from the database if they're deleted in Mudlet?
        """))
    async def alias_cfp(self, cmd):
        await self._conf_flip("mudlet_explode")

    @doc(_(
        """
        Use GMCP room IDs stored in Mudlet?
        """))
    async def alias_cfg(self, cmd):
        await self._conf_flip("mudlet_gmcp")

    @doc(_(
        """
        Modify existing rooms' area when you visit them?
        """))
    async def alias_cff(self, cmd):
        await self._conf_flip("force_area")

    @doc(_(
        """
        Use the MUD's area name?
        If unset, use the last-visited room's area, or 'Default'.
        """))
    async def alias_cfm(self, cmd):
        await self._conf_flip("use_mud_area")

    @doc(_(
        """Delete a room.
        You can't delete the room you're in or the one you're coming from.
        """))
    @with_alias("r-")
    async def alias_r_m(self, cmd):
        db = self.db
        cmd = self.cmdfix("r", cmd, min_words=1)
        room = cmd[0]
        if self.room == room:
            await self.print(_("You can't delete the room you're in."))
            return
        if self.last_room == room:
            await self.print(_("You can't delete the room you just came from."))
            return
        if self.view_room == room:
            await self.view_to(None)
        await self._delete_room(room)

    async def _delete_room(self, room):
        id_mudlet = room.id_mudlet
        self.db.delete(room)
        self.db.commit()
        if id_mudlet:
            await self.mud.deleteRoom(id_mudlet)
            await self.mud.updateMap()

    @doc(_(
        """
        Find room
        List rooms with this text in their shortname.
        """))
    async def alias_fr(self, cmd):
        cmd = self.cmdfix("*", cmd, min_words=1)
        db = self.db
        n = 0
        for r in db.q(db.Room).filter(db.Room.name.like('%'+cmd[0]+'%')).all():
            n += 1
            await self.print(r.idnn_str)
        if n == 0:
            await self.print(_("No room name with {txt!r} found."), txt=cmd[0])
        
    @doc(_(
        """
        Find descriptions
        List rooms with this text in their long description.
        """))
    async def alias_fd(self, cmd):
        cmd = self.cmdfix("*", cmd, min_words=1)
        db = self.db
        n = 0
        for r in db.q(db.Room).join(db.Room.long_descr).filter(db.LongDescr.descr.like('%'+cmd[0]+'%')).all():
            n += 1
            await self.print(r.idnn_str)
        if n == 0:
            await self.print(_("No room text with {txt!r} found."), txt=cmd[0])
        
    @doc(_(
        """
        Find label
        List rooms with this label
        """))
    async def alias_fl(self, cmd):
        cmd = self.cmdfix("*", cmd)
        db = self.db
        if not cmd:
            for label,count in db.q(db.Room.label, func.count(db.Room.label)).group_by(db.Room.label).all():
                await self.print(f"{count} {label}")
        else:
            n = 0
            for r in db.q(db.Room).filter(db.Room.label == cmd[0]).order_by(db.Room.name).all():
                n += 1
                await self.print(r.idnn_str)
            if n == 0:
                await self.print(_("No room text with {txt!r} found."), txt=cmd[0])
        
    @doc(_(
        """
        Find note
        List rooms with this text in their notes.
        """))
    async def alias_fn(self, cmd):
        cmd = self.cmdfix("*", cmd, min_words=1)
        db = self.db
        n = 0
        for r in db.q(db.Room).join(db.Room.note).filter(db.Note.note.like('%'+cmd[0]+'%')).all():
            n += 1
            await self.print(r.idnn_str)
        if n == 0:
            await self.print(_("No room note with {txt!r} found."), txt=cmd[0])
        

    @doc(_(
        """
        Find thing
        List rooms with this text in the names of stuff in them
        """))
    async def alias_ft(self, cmd):
        cmd = self.cmdfix("*", cmd, min_words=1)
        db = self.db
        n = 0
        for r in db.q(db.Room).join(db.Room.things).filter(db.Thing.name.like('%'+cmd[0]+'%')).all():
            n += 1
            await self.print(r.idnn_str)
        if n == 0:
            await self.print(_("No room with {txt!r} found."), txt=cmd[0])
        


    @doc(_(
        """Show room flags
        """))
    async def alias_rf(self, cmd):
        db = self.db
        cmd = self.cmdfix("r", cmd)
        if cmd:
            room = cmd[0]
        else:
            room = self.view_or_room
        flags = []
        if not room.flag:
            await self.print(_("No flag set."))
            return
        if room.flag & db.Room.F_NO_EXIT:
            flags.append(_("Description doesn't have exits"))
        if room.flag & db.Room.F_NO_GMCP_ID:
            flags.append(_("ignore this GMCP ID"))
        if room.flag & db.Room.F_NO_AUTO_EXIT:
            flags.append(_("Don't auto-set exits"))
        await self.print(_("Flags: {flags}"), flags=" ".join(flags))

    @doc(_(
        """Flag: no-exit-line
        Marker that this room's description doesn't list its exits.
        """))
    async def alias_rfe(self, cmd):
        db = self.db
        F = db.Room.F_NO_EXIT
        cmd = self.cmdfix("r", cmd)
        if cmd:
            room = cmd[0]
        else:
            room = self.view_or_room
        flags = []
        if room.flag & F:
            room.flag &=~ F
            await self.print(_("Room normal: has Exits line."))
        else:
            room.flag |= F
            await self.print(_("Room special: has no Exits line."))
        db.commit()

    @doc(_(
        """Flag: no-exits
        Exits are special, don't auto-set them.
        """))
    async def alias_rfx(self, cmd):
        db = self.db
        F = db.Room.F_NO_AUTO_EXIT
        cmd = self.cmdfix("r", cmd)
        if cmd:
            room = cmd[0]
        else:
            room = self.view_or_room
        flags = []
        if room.flag & F:
            room.flag &=~ F
            await self.print(_("Room normal: exits work normally."))
        else:
            room.flag |= F
            await self.print(_("Room special: unset exits are special."))
        db.commit()

    @doc(_(
        """Flag: no-GMCP-ID
        Marker that this room is moveable by the player and thus its GMCP ID
        should be ignored. Used for boats, lifts, etc.
        """))
    async def alias_rfg(self, cmd):
        db = self.db
        F = db.Room.F_NO_GMCP_ID
        cmd = self.cmdfix("r", cmd)
        if cmd:
            room = cmd[0]
        else:
            room = self.view_or_room
        flags = []
        if not room.id_gmcp:
            await self.print(_("The Room doesn't have a GMCP ID!"))
            return
        if room.flag & F:
            room.flag &=~ F
            await self.print(_("Room normal: normal GMCP ID."))
        else:
            room.flag |= F
            await self.print(_("Room special: GMCP ID ignored."))
        db.commit()

    @doc(_(
        """List for labels
        Show a list of labels / rooms with that label.
        No arguments: show the labels + room count.
        Label: show the list of rooms with that label.
        """))
    async def alias_rtl(self, cmd):
        db = self.db
        cmd = self.cmdfix("w", cmd)
        if not cmd:
            for label,count in db.q(db.Room.label, func.count(db.Room.label)).group_by(db.Room.label).all():
                await self.print(f"{count} {label}")
        else:
            for r in db.q(db.Room).filter(db.Room.label == cmd[0]).all():
                await self.print(r.idnn_str)


    @doc(_(
        """Show/change the current room's label.
        No arguments: show the label.
        '-': delete the label
        Anything else is set as label (single word only).
        """))
    async def alias_rt(self, cmd):
        cmd = self.cmdfix("w", cmd)
        room = self.view_or_room
        if not room:
            await self.print(_("No active room."))
            return
        if not cmd:
            if room.label:
                await self.print(_("Label of {room.idn_str}: {room.label}"), room=room)
            else:
                await self.print(_("No label for {room.idn_str}."), room=room)
        else:
            cmd = cmd[0]
            if cmd == "-":
                if room.label:
                    await self.print(_("Label of {room.idn_str} was {room.label}"), room=room)
                    room.label = None
                    self.db.commit()
            elif room.label != cmd:
                if room.label:
                    await self.print(_("Label of {room.idn_str} was {room.label}"), room=room)
                else:
                    await self.print(_("Label of {room.idn_str} set"), room=room)
                room.label = cmd
            else:
                await self.print(_("Label of {room.idn_str} not changed"), room=room)
        self.db.commit()
        await self.dr.show_room_label(room)

    async def add_processor_note(self, proc, room):
        """
        Take the output+input from a processor and add it to the text
        """
        txt = _("> {cmd}").format(room=room, cmd=proc.command)
        txt += "\n"+"\n".join(proc.lines[proc.P_BEFORE])
        await self._add_room_note(txt, room)

    @doc(_(
        """Show room note.
        Provide a room# to use that room.
        """))
    async def alias_rn(self, cmd):
        cmd = self.cmdfix("r", cmd)
        room = cmd[0] if cmd else self.view_or_room
        if not room:
            await self.print(_("No active room."))
            return
        if room.note:
            await self.print(_("Note of {room.idn_str}:\n{room.note.note}"), room=room)
        else:
            await self.print(_("No note for {room.idn_str}."), room=room)

    @doc(_(
        """Add old command as room note.
        Add the n'th last command and its output to the note.
        """))
    async def alias_rna(self, cmd):
        cmd = self.cmdfix("ir", cmd)
        room = cmd[1] if len(cmd)>1 else self.view_or_room
        if not cmd:
            for i,s in enumerate(self.last_commands):
                await self.print(_("{i}: {cmd}"), i=i+1,cmd=s.command)
            return
        await self.add_processor_note(self.last_commands[cmd[0]-1], room)

    @doc(_(
        """Add new command as room note.
        Send this command and add it + the resulting MUD output to the note.
        """))
    async def alias_rnc(self, cmd):
        if not cmd:
            await self.print(_("No command given."))
            return
        self.main.start_soon(self.send_commands, "",NoteProcessor(self, cmd))

    @with_alias("rn=")
    @doc(_(
        """Delete a room's notes."""))
    async def alias_rn_m(self, cmd):
        cmd = self.cmdfix("i", cmd)
        room = cmd[0] if cmd else self.view_or_room
        if not room:
            await self.print(_("No room given and no active room."))
            return
        if room.note:
            await self.print(_("Note of {room.idn_str} deleted."), room=room)
            self.db.delete(room.note)
            self.db.commit()
        else:
            await self.print(_("No note known."))

    @with_alias("rn.")
    @doc(_(
        """Add text to a / the current room's note.
        Append whatever you type next to the notes.
        End input with a single dot .
        """))
    async def alias_rn_dot(self, cmd):
        cmd = self.cmdfix("r", cmd)
        room = cmd[0] if cmd else self.view_or_room
        if not room:
            await self.print(_("No room given and no active room."))
            return
        txt = cmd +"\n" if cmd else ""
        await self.print(_("Extending note. End with '.'."))
        async with self.input_grab_multi() as g:
            async for line in g:
                txt += line+"\n"
        await self._add_room_note(txt, room)

    @with_alias("rn+")
    @doc(_(
        """Add to the current room's note.
        This command's arguments are added as-is to the current room's note.
        """))
    async def alias_rn_plus(self, cmd):
        room = self.view_or_room
        if not room:
            await self.print(_("No active room."))
            return
        if not cmd:
            await self.print(_("No text."))
            return
        await self._add_room_note(cmd, room)

    async def _add_room_note(self, txt, room, prefix=False):
        db = self.db
        rn = room.note
        if room.note:
            if prefix:
                rn.note = txt + "\n" + room.note
            else:
                rn.note += "\n" + txt
            await self.print(_("Note of {room.idn_str} extended."), room=room)
        else:
            note = db.Note(note=txt, room_id=room.id_old)
            db.add(note)
            await self.print(_("Note of {room.idn_str} created."), room=room)
        db.commit()
        await s.dr.show_room_note()


    @with_alias("rn*")
    @doc(_(
        """Prepend a line to the current room's note."""))
    async def alias_rn_star(self, cmd):
        room = self.view_or_room
        if not room:
            await self.print(_("No active room."))
            return
        if not cmd:
            await self.print(_("No text."))
            return
        await self._add_room_note(cmd, room, prefix=True)


    @doc(_(
        """Show/change the room's area/domain.
        Parameters: domain ‹room›"""))
    async def alias_ra(self, cmd):
        cmd = self.cmdfix("wr", cmd)
        room = cmd[1] if len(cmd)>1 else self.view_or_room
        if not room:
            await self.print(_("No active room."))
            return
        if not cmd or not cmd[0]:
            if room.area:
                await self.print(room.area.name)
            else:
                await self.print(_("No area/domain set."))
            return

        area = await self.get_named_area(cmd[0], False)
        if room.info_area == area:
            pass
        elif room.orig_area != area:
            room.orig_area = room.area
        await room.set_area(area, force=True)
        self.db.commit()

    @doc(_(
        """Show/change the area's ignore flag."""))
    async def alias_rai(self, cmd):
        db = self.db
        cmd = self.cmdfix("w", cmd, min_words=1)
        area = await self.get_named_area(cmd[0], False)
        if area.flag & db.Area.F_IGNORE:
            area.flag &=~ db.Area.F_IGNORE
            await self.print(_("Area no longer ignored."))
        else:
            area.flag |= db.Area.F_IGNORE
            await self.print(_("Area ignored."))
        db.commit()

    @with_alias("ra!")
    @doc(_(
        """Set the room to its 'other' area, or to a new named one"""))
    async def alias_ra_b(self, cmd):
        cmd = self.cmdfix("wr", cmd)
        room = cmd[1] if len(cmd)>1 else self.view_or_room
        if cmd and cmd[0]:
            cmd = cmd[0]
            area = await self.get_named_area(cmd, True)
        else:
            if room.orig_area and room.orig_area != room.area:
                area = room.orig_area
            elif room.info_area and room.info_area != room.area:
                area = room.info_area
            else:
                await self.print(_("No alternate area/domain known."))
                return
        return await self.alias_ra(area.name)

    @doc(_(
        """Update on-map location during movement"""))
    async def alias_cfi(self, cmd):
        await self._conf_flip("show_intermediate")

    @doc(_(
        """When creating an exit, also link back?"""))
    async def alias_cfr(self, cmd):
        await self._conf_flip("add_reverse")

    @doc(_(
        """Can new rooms be created in Z direction?"""))
    async def alias_cfz(self, cmd):
        await self._conf_flip("dir_use_z")

    @doc(_(
        """Debug traps when allocating a new room?"""))
    async def alias_cfd(self, cmd):
        await self._conf_flip("debug_new_room")

    async def _conf_flip(self, name):
        self.conf[name] = not self.conf[name]
        await self.print(_("Setting '{name}' {set}."), name=name, set=_('set') if self.conf[name] else _('cleared'))
        await self._save_conf(name)

    @doc(_(
        """X shift for moving room labels (Ctrl-left/right)"""))
    async def alias_cox(self, cmd):
        await self._conf_float("label_shift_x", cmd)

    @doc(_(
        """Y shift for moving room labels (Ctrl-Shift-left/right)"""))
    async def alias_coy(self, cmd):
        await self._conf_float("label_shift_x", cmd)

    @doc(_(
        """X offset for placing rooms"""))
    async def alias_cop(self, cmd):
        await self._conf_int("pos_x_delta", cmd)

    @doc(_(
        """Y offset for placing rooms"""))
    async def alias_coq(self, cmd):
        await self._conf_int("pos_x_delta", cmd)

    @doc(_(
        """Diagonal offset for placing rooms (Z, nonstandard exits)"""))
    async def alias_cor(self, cmd):
        await self._conf_int("pos_small_delta", cmd)

    async def _conf_float(self, name, cmd):
        if cmd:
            v = float(cmd)
            self.conf[name] = v
            await self.print(_("Setting '{name}' to {v}."), v=v, name=name)
            await self._save_conf(name)
        else:
            v = self.conf[name]
            await self.print(_("Setting '{name}' is now {v}."), v=v, name=name)

    async def _conf_int(self, name, cmd):
        if cmd:
            v = int(cmd)
            self.conf[name] = v
            await self.print(_("Setting '{name}' to {v}."), v=v, name=name)
            await self._save_conf(name)
        else:
            v = self.conf[name]
            await self.print(_("Setting '{name}' is now {v}."), v=v, name=name)

    async def _save_conf(self, name):
        v = json.dumps(self.conf[name])
        await self.mud.setMapUserData("conf."+name, v)

    @with_alias("x=")
    @doc(_(
        """Remove an exit from a / the current room
        Usage: #x= ‹exit› ‹room›"""))
    async def alias_x_del(self, cmd):
        cmd = self.cmdfix("xr",cmd, min_words=1)
        room = self.view_or_room if len(cmd) < 2 else cmd[1]
        cmd = cmd[0]
        try:
            x = room.exit_at(cmd)
        except KeyError:
            await self.print(_("Exit unknown."))
            return
        if self.this_exit is x:
            self.this_exit = None
        if (await room.set_exit(cmd.strip(), None))[1]:
            await self.update_room_color(room)
        await self.mud.updateMap()
        await self.print(_("Removed."))

    @with_alias("x-")
    @doc(_(
        """Set an exit from a / the current room to 'unknown'
        Usage: #x- ‹exit› ‹room›"""))
    async def alias_x_m(self, cmd):
        cmd = self.cmdfix("xr",cmd, min_words=1)
        room = self.view_or_room if len(cmd) < 2 else cmd[1]
        cmd = cmd[0]
        try:
            x = room.exit_at(cmd)
        except KeyError:
            await self.print(_("Exit unknown."))
            return
        if (await room.set_exit(cmd.strip(), False))[1]:
            await self.update_room_color(room)
        await self.mud.updateMap()
        await self.print(_("Set to 'unknown'."))

    @with_alias("x+")
    @doc(_(
        """Add an exit from this room to some other / an unknown room.
        Usage: #x+ ‹exit› ‹dest_room›
        """))
    async def alias_x_p(self, cmd):
        cmd = self.cmdfix("xr",cmd)
        room = self.view_or_room
        if not room:
            await self.print(_("No active room."))
            return
        if len(cmd) == 2:
            d,r = cmd
        else:
            d = cmd[0]
            r = True
        if (await room.set_exit(d, r))[1]:
            await self.update_room_color(room)
        await self.mud.updateMap()

    @doc(_(
        """Show exit details
        Either of a single exit (including steps if present), or all of them.
        The second parameter can be an explicit room.
        """))
    async def alias_xs(self,cmd):
        cmd = self.cmdfix("xr",cmd)
        room = cmd[1] if len(cmd)>1 else self.view_or_room
        def get_txt(x):
            txt = x.dir
            if x.cost != 1:
                txt += f" /{x.cost}"
            txt += ": "
            if x.dst:
                txt += x.dst.idnn_str
            else:
                txt += _("‹unknown›")
            if x.feature:
                txt += " +"+x.feature.name
            if x.back_feature:
                txt += " -"+x.back_feature.name
            if x.steps:
                txt += " ."+str(len(x.moves))
            return txt

        if len(cmd) == 0:
            for x in room.exits:
                txt = get_txt(x)
                await self.print(txt)
        else:
            try:
                x = room.exit_at(cmd[0])
            except KeyError:
                await self.print(_("Exit unknown."))
                return
            await self.print(get_txt(x))
            if x.steps:
                for m in x.moves:
                    await self.print("… "+m)

    @doc(_(
        """
        Cost of walking through an exit
        Set the cost of using an exit.
        Params: exit cost [room]
        """))
    async def alias_xp(self, cmd):
        cmd = self.cmdfix("xir", cmd, min_words=1)
        room = cmd[2] if len(cmd) > 2 else self.view_or_room
        x = room.exit_at(cmd[0])
        if len(cmd) == 1:
            await self.print(_("Cost of {exit.dir!r} from {room.idn_str} is {exit.cost}"), room=room,exit=x)
            return
        c = x.cost
        if c != cmd[1]:
            await self.print(_("Cost of {exit.dir!r} from {room.idn_str} changed from {exit.cost} to {cost}"), room=room,exit=x,cost=cmd[1])
            x.cost = cmd[1]
            self.db.commit()
        else:
            await self.print(_("Cost of {exit.dir!r} from {room.idn_str} is {exit.cost} (unchanged)"), room=room,exit=x)


    @doc(_(
        """
        Cost of walking through this room
        Set the cost of passing through this room, defined as the min cost
        of leaving it
        Params: [cost [room]]
        """))
    async def alias_rp(self, cmd):
        cmd = self.cmdfix("ir", cmd, min_words=0)
        room = cmd[1] if len(cmd) > 1 else self.view_or_room
        if not cmd:
            await self.print(_("Cost of {room.idn_str} is {room.cost}"), room=room)
        elif cmd[0] != room.cost:
            await self.print(_("Cost of {room.idn_str} changed from {room.cost} to {cost}"), room=room, cost=cmd[0])
            room.set_cost(cmd[0])
            self.db.commit()
        else:
            await self.print(_("Cost of {room.idn_str} is {room.cost} (unchanged)"), room=room)



    @doc(_(
        """
        delay for an exit
        Set the delay when using an exit.
        Params: exit delay [room]
        The delay is in tenth seconds and does NOT affect the cost of ways
        through the room.
        """))
    async def alias_xd(self, cmd):
        cmd = self.cmdfix("xir", cmd, min_words=2)
        room = cmd[2] if len(cmd) > 2 else self.view_or_room
        x = room.exit_at(cmd[0])
        c = x.delay
        if c != cmd[1]:
            await self.print(_("Delay of {exit.dir} from {room.idn_str} changed from {room.delay} to {delay}"), room=room,exit=x,delay=cmd[1])
            x.delay = cmd[1]
            self.db.commit()
        else:
            await self.print(_("Delay of {exit.dir} from {room.idn_str} unchanged"), room=room,exit=x)


    @doc(_(
        """
        Commands for an exit.
        Usage: #xc ‹exit› ‹whatever to send›
        Add to the list of things to send.
        """))
    async def alias_xc(self, cmd):
        cmd = self.cmdfix("x*", cmd, min_words=2)
        room = self.view_or_room
        if not room:
            await self.print(_("No active room."))
            return
        x = (await room.set_exit(cmd[0]))[0]
        x.steps = ((x.steps + "\n") if x.steps else "") + cmd[1]
        await self.print(_("Added."))
        self.db.commit()

    @doc(_(
        """
        Pre-Command for an exit.
        Usage: #xcp ‹exit› ‹whatever to send›
        Prepend to the list of things to send.
        If the list was previously empty, the exit itself is also added.
        """))
    async def alias_xcp(self, cmd):
        cmd = self.cmdfix("x*", cmd, min_words=2)
        room = self.view_or_room
        if not room:
            await self.print(_("No active room."))
            return
        x = (await room.set_exit(cmd[0]))[0]
        x.steps = cmd[1] + "\n" + (x.steps or x.dir)
        await self.print(_("Prepended."))
        self.db.commit()

    @with_alias("xc-")
    @doc(_(
        """
        Remove special commands for this exit.
        """))
    async def alias_xc_m(self, cmd):
        cmd = self.cmdfix("xr", cmd, min_words=1)
        room = cmd[1] if len(cmd)>1 else self.view_or_room
        if not room:
            await self.print(_("No active room."))
            return
        x = (await room.set_exit(cmd[0]))[0]
        if x.steps:
            await self.print(_("Steps:"))
            for m in x.moves:
                await self.print("… "+m)
            x.steps = None
            await self.print(_("Deleted."))
            self.db.commit()
        else:
            await self.print(_("This exit doesn't have specials."))

    @doc(_(
        """
        Prefix for the last move.
        Applied to the room you just came from!
        Usage: #xtp ‹whatever to send›
        Put in front of the list of things to send.
        If the list was empty, the exit name is added also.
        """))
    async def alias_xtp(self, cmd):
        cmd = self.cmdfix("*", cmd, min_words=1)
        x = self.current_exit
        if not x:
            await self.print(_("I have no idea how you got here."))
            return
        if not x.steps:
            x.steps = x.dir
        x.steps = cmd[0] + "\n" + x.steps
        await self.print(_("Prepended."))
        self.db.commit()

    @doc(_(
        """
        Suffix for the last move.
        Applied to the exit you just arrived with!
        Usage: #xts ‹whatever to send›
        Put at end of the list of things to send.
        If the list was empty, the exit name is added also.
        If nothing to send is given, use the last command.
        """))
    async def alias_xts(self, cmd):
        cmd = self.cmdfix("*", cmd)
        x = self.current_exit
        if not x:
            await self.print(_("I have no idea how you got here."))
            return
        if not x.steps:
            x.steps = x.dir
        x.steps += "\n" + (cmd[0] if cmd else self.last_cmd or self.last_commands[0].command)
        await self.print(_("Appended."))
        self.db.commit()

    @doc(_(
        """
        Prepare a named exit
        Usage: you have an interesting and/or existing exit but don't know
        which command triggers it.
        So you say "#xn pseudo_direction", then try any number of things,
        and when you do move the exit will be created with the name you
        give here and with the command you used to get there.

        "#xn" without a name will clear this.
        """))
    async def alias_xn(self, cmd):
        cmd = self.cmdfix("x", cmd)
        if not cmd:
            self.named_exit = None
            await self.print(_("Exit name cleared."))
        else:
            await self._prep_exit(cmd[0])

    async def _prep_exit(self, d):
        try:
            x = self.room.exit_at(d)
        except KeyError:
            x = self.db.Exit(dir=d, src=self.room)
            self.db.add(x)
            self.db.commit()
            await self.print(_("Exit {exit.dir!r} prepated (new)"), exit=x)
        else:
            if x.dst is None:
                await self.print(_("Exit {exit.dir!r} prepared (unknown dest)"), exit=x)
            else:
                await self.print(_("Exit {exit.dir!r} prepared, goes to {exit.dst.idnn_str}"), exit=x)

        self.named_exit = x
        await self.print(_("Exit {exit.dir!r} prepared."), exit=x)

    @doc(_(
        """
        Timed exit
        Usage: you're in a room but know that you'll be swept to another
        one shortly.
        Use '#xn' to clear this.
        """))
    async def alias_xnt(self, cmd):
        cmd = self.cmdfix("", cmd)
        self.named_exit = TIME_DIR
        await self.print(_("Timed exit prepared."))
    
    @doc(_(
        """
        Rename the exit just taken.
        The exit just taken, usually a command like "enter house",
        is renamed to whatever you say here. The exit is aliased to the old
        name. Thus "#xt house", after the move, will rename that exit to
        "house" and save "enter house" as the command to use.
        """))
    async def alias_xt(self, cmd):
        cmd = self.cmdfix("x",cmd, min_words=1)[0]

        if not self.room or not self.last_room:
            await self.print(_("I don't know where I am or where I came from."))
            return

        try:
            x = self.last_room.exit_at(cmd)
        except KeyError:
            pass
        else:
            await self.print(_("This exit already exists:"))
            await self.alias_xs(x.dir)
            return

        try:
            x = self.last_room.exit_to(self.room)
        except KeyError:
            await self.print(_("{self.last_room.idn_str} doesn't have an exit to {self.room.idn_str}?"), self=self)
            return
        if not x.steps:
            x.steps = x.dir
        await self.print(_("Exit renamed from {src} to {dst}"),src=x.dir,dst=cmd)
        x.dir = cmd
        self.db.commit()

    @doc(_(
        """
        Match SQL to Mudlet rooms
        Given a SQL room ID (negative number) and a Mudlet room ID
        (positive number), teach the mapper that these are the same rooms.
        This is automatic if the Mudlet map contains hashes.
        """))
    async def alias_mda(self, cmd):
        db = self.db
        cmd = self.cmdfix("ii", cmd, min_words=2)
        if cmd[0]>=0 or cmd[1]<0:
            await self.print(_("Params: negative-DB-ID positive-Mudlet-ID-or-zero"))
            return
        r1 = db.r_old(-cmd[0])
        if cmd[1]:
            try:
                r2 = db.r_mudlet(cmd[1])
            except NoData:
                m = await self.mud.getRoomCoordinates(cmd[1])
                if m:
                    r1.pos_x,r1.pos_y,r1.pos_z = m
                    r1.id_mudlet = cmd[1]
                else:
                    await self.print(_("Huh? Mudlet doesn't know room {id}!"), id=cmd[1])
            else:
                await self.print(_("Huh? This Mudlet ID is room -{room.id_old}: {room.idnn_str}."), room=r2)
        else:
            if r1.id_mudlet:
                await self.print(_("Disassociated, was {mudlet}."), mudlet=r1.id_mudlet)
                r1.id_mudlet = None
            else:
                await self.print(_("Huh? This room didn't have a Mudlet ID."))
        db.commit()


    @doc(_(
        """
        Merge rooms
        
        Given a room / the room you're viewing / located at, and a selected
        room on the map, zap your current room and associate the selected
        room with it.

        Exits are taken from the database.
        """))
    async def alias_mdm(self, cmd):
        db = self.db
        cmd = self.cmdfix("r", cmd)
        room = cmd[0] if cmd else self.view_or_room

        sel = await self.mud.getMapSelection()
        if not sel or not sel[0] or len(sel[0].get("rooms",())) != 1:
            await self.print(_("No single room selected."))
            return
        r = sel[0]["rooms"][0]
        try:
            xroom = db.r_mudlet(r)
        except NoData:
            pass
        else:
            await self.print(_("Error: Selection is known: {room.idnn_str}"), room=xroom)
            return
        old_r,room.id_mudlet = room.id_mudlet,r

        m = await self.mud.getRoomCoordinates(r)
        if m:
            room.pos_x,room.pos_y,room.pos_z = m
        ra = (await self.mud.getRoomArea(r))[0]
        room.area_id = ra

        # will raise an error if the area doesn't exist.
        # Shouldn't play with them in Mudlet while running.
        db.commit()
        await self.print(_("Reassigned: {room.idnn_str}"), room=room)
        if self.view_or_room == room:
            await self.mud.centerview(room.id_mudlet)
        await self.mud.deleteRoom(old_r)
        for x in room.exits:
            await room.set_mud_exit(x.dir, x.dst if x.dst_id and x.dst.id_mudlet else True)
        await self.mud.updateMap()

    @doc(_(
        """
        Update Mudlet map from database.
        
        Assume that the Mudlet map is out of date / has not been saved:
        write our room data to Mudlet.

        Rooms in the Mudlet map which we don't have are not touched.
        """))
    async def alias_mds(self, cmd):
        db = self.db
        s=0

        await self.print("Sync rooms")
        await self.sync_areas()
        async def upd(r):
            nonlocal s
            s2 = r.id_mudlet // 200
            if s != s2:
                s = s2
                await self.print(_("… {room.id_mudlet}"), room=r)
                await self.mud.updateMap()
            m = await self.mud.getRoomCoordinates(r.id_mudlet)
            if not m:
                await self.mud.addRoom(r.id_mudlet)
            await self.mud.setRoomCoordinates(r.id_mudlet, r.pos_x, r.pos_y, r.pos_z)
            await self.mud.setRoomName(r.id_mudlet, r.name)
            await self.mud.setRoomNameOffset(r.id_mudlet, r.label_x,r.label_y)

            if r.area_id:
                await self.mud.setRoomArea(r.id_mudlet, r.area_id)
            db.commit()

        for r in db.q(db.Room).filter(db.Room.id_mudlet != None).order_by(db.Room.id_mudlet).all():
            await upd(r)
        for r in db.q(db.Room).filter(db.Room.id_mudlet == None, (db.Room.pos_x != 0) | (db.Room.pos_y != 0)).order_by(db.Room.id_mudlet).all():
            r.set_id_mudlet((await self.rpc(action="newroom"))[0])
            await upd(r)

        await self.print("Sync exits")
        s=0
        for r in db.q(db.Room).filter(db.Room.id_mudlet != None).order_by(db.Room.id_mudlet).all():
            s2 = r.id_mudlet // 500
            if s != s2:
                s = s2
                await self.print(_("… {room.id_mudlet}"), room=r)
                await self.mud.updateMap()
            for x in r.exits:
                await r.set_mud_exit(x.dir, x.dst if x.dst_id and x.dst.id_mudlet else True)

        await self.print("Done with map sync")
        await self.mud.updateMap()
         
    @doc(_(
        """
        Update database from Mudlet map.
        
        Assume that the Mudlet map has been modified (rooms moved):
        copy current positions to the database.

        Rooms in the Mudlet map which we don't have are not touched.
        """))
    async def alias_mdt(self, cmd):
        db = self.db
        s=0

        await self.print("Sync room positions")
        await self.sync_areas()
        for r in db.q(db.Room).filter(db.Room.id_mudlet != None).order_by(db.Room.id_mudlet).all():
            s2 = r.id_mudlet // 250
            if s != s2:
                s = s2
                await self.print(_("… {room.id_mudlet}"), room=r)
            m = await self.mud.getRoomCoordinates(r.id_mudlet)
            r.pos_x,r.pos_y,r.pos_z = m
        db.commit()
        await self.print("Sync done")
         
    async def alias_mdi(self, cmd):
        """
        Import Mudlet map

        Incrementally imports a Mudlet map by following exits that are only in Mudlet.
        Typical usage: select an initial room, then '#mdi ??'.
        """
        db = self.db
        if cmd.strip() == "??":
            sel = await self.mud.getMapSelection()
            if not sel or not sel[0] or not sel[0]["rooms"]:
                await self.print(_("You need to select unmapped rooms."),id=r)
                return
            cmd = []
            for r in sel[0]["rooms"]:
                try:
                    room = db.r_mudlet(r)
                except NoData:
                    nr = await self.new_room(id_mudlet=r)
                    cmd.append(nr)
            if not cmd:
                await self.print(_("All selected rooms are known."))
                return
        else:
            cmd = self.cmdfix("r"*99, cmd, min_words=1)
        await self.sync_from_mudlet(*cmd)

    @with_alias("mdi!")
    @doc(_("""
        Import Mudlet map, clearing all old associations

        Incrementally imports a Mudlet map by following exits that are only in Mudlet.
        Typical usage: select an initial room, then '#mdi ??'.

        WARNING: this command erases all old associations between the Mudlet
        and MudMyC maps.
        """))
    async def alias_mdi_b(self, cmd):
        """
        Basic sync when some mudlet IDs got deleted due to out-of-sync-ness
        """
        cmd = self.cmdfix("r"*99, cmd, min_words=1)
        await self.sync_from_mudlet(*cmd, clear=True)

    @doc(_(
        """Current description state
        Shows what the descriptions look like that the mapper has recorded.
        """))
    async def alias_mt(self, cmd):
        p = self.print
        cmd = self.cmdfix("i", cmd)
        if cmd:
            cmd = cmd[0]-1
        else:
            cmd = 0
        s = self.last_commands[cmd]
        await p(_("Dir: {dir}").format(dir=s.command))
        for i,b in enumerate(s.lines):
            for l in b:
                await p(_("{i} {l}").format(i=i,l=l))

    @doc(_(
        """
        Fix last move
        You went to another room instead.
        Mention a room# to use that room, or zero to create a new room.
        Unlike #ms this command adds an exit, either explicitly (second
        word) or using the one you used.
        This deletes the room you came from if it was new.
        """))
    async def alias_mn(self, cmd):
        if not self.last_room:
            await self.print(_("I have no idea where you were."))
            return

        cmd = self.cmdfix("r", cmd)
        if len(cmd)>1:
            d = cmd[1]
        elif self.last_dir is not None:
            d=self.last_dir
        else:
            await self.print(_("I have no idea how you got here."))
            return
        if cmd and cmd[0]:
            r = cmd[0]
        else:
            r = await self.new_room(self.dr.NAMELESS, offset_from=self.last_room, offset_dir=self.last_dir)
        await self.last_room.set_exit(self.last_dir,r, force=True)
        self.db.commit()
        lr = self.room if self.is_new_room else None
        await self.went_to_room(r, repair=True)

        if lr is not None:
            await self._delete_room(lr)


    @doc(_(
        """
        Set/show location
        You are here.
        Explicitly use zero to create a new room.
        """))
    async def alias_ms(self, cmd):
        cmd = self.cmdfix("r", cmd)
        if cmd:
            r = cmd[0]
            if not r:
                r = await self.new_room(self.dr.NAMELESS, offset_from=self.last_room, offset_dir=self.last_dir)
                self.db.commit()
            await self.went_to_room(r, repair=True)
        else:
            await self.view_to(None)

    @doc(_(
        """
        I moved. Use with nonstandard directions.
        Should be unnecessary if GMCP, or known exit to non-GMCP room.

        Usage: #mm ‹room› ‹dir›
        Leave room empty for new destination. Direction defaults to last command.
        """))
    async def alias_mm(self, cmd):
        cmd = self.cmdfix("rx", cmd)

        d = cmd[1] if len(cmd) > 1 else self.last_cmd or self.last_commands[0].command
        if cmd:
            room = cmd[0] or self.room
            await self.room.set_exit(d, room)
            await self.went_to_room(room, d)
        elif self.named_exit:
            await self.went_to_room(room, self.named_exit)
        else:
            await self.went_to_dir(d)

    @doc(_(
        """
        Set previous location
        You came from  here.
        Explicitly use zero to create a new room.
        """))
    async def alias_ml(self, cmd):
        cmd = self.cmdfix("rx", cmd)
        if not cmd:
            if not self.last_room:
                await self.print(_("I have no idea where you were."))
                return
            await self.print(_("You went {d} from {last.idn_str}."), last=self.last_room,d=self.last_dir,room=self.room)
            return
        r = cmd[0] or await self.new_room(self.dr.NAMELESS)
        self.last_room = r
        if not cmd[0]:
            await self.print(_("{last.idn_str} created."), last=self.last_room,d=self.last_dir,room=self.room)
        if len(cmd) > 1:
            self.last_dir = cmd[1]
            if (await r.set_exit(cmd[1],self.room or True))[1]:
                await self.update_room_color(r)
                if self.room:
                    await self.update_room_color(self.room)
        self.db.commit()
        if self.last_dir:
            await self.print(_("You came from {last.idn_str} and went {d}."), last=self.last_room,d=self.last_dir)
        else:
            await self.print(_("You came from {last.idn_str}."), last=self.last_room)



    @doc(_(
        """
        Patch map
        Teach the map that room A's exit B goes to C.
        Mention a room# to use that room, or leave empty to create a new room.
        Zero for A: use known last room
        Zero for C: not known
        """))
    async def alias_mp(self, cmd):
        cmd = self.cmdfix("rxr", cmd, min_words=3)
        prev,d,this = cmd
        if prev is None:
            prev = self.last_room
            if prev is None:
                await self.print(_("I have no idea where you were."))
                return
        if this is None:
            this = False
        if (await prev.set_exit(d,this))[1]:
            await self.update_room_color(prev)
        self.db.commit()
        await self.mud.updateMap()
        await self.print(_("Exit set."))

    @doc(_(
        """
        Set timed move
        Teach the map that room A goes to B after some time.
        Mention a room# to use that room, or leave empty to create a new room.
        Zero for A: use known last room
        Zero for B: not known
        """))
    async def alias_mpt(self, cmd):
        cmd = self.cmdfix("rr", cmd, min_words=2)
        prev,this = cmd
        d = TIME_DIR
        if prev is None:
            prev = self.last_room
            if prev is None:
                await self.print(_("I have no idea where you were."))
                return
        if this is None:
            this = False
        if (await prev.set_exit(d,this))[1]:
            await self.update_room_color(prev)
        self.db.commit()
        await self.mud.updateMap()
        await self.print(_("Timed exit set."))

    async def update_room_color(self, room):
        """TODO move this to the room"""
        if not room.id_mudlet:
            return
        await self.mud.setRoomEnv(room.id_mudlet, ENV_OK+room.open_exits)
        await self.mud.updateMap()

    @doc(_(
        """
        Database rooms not in Mudlet

        Find routes to those rooms. Use #gg to use one.

        No parameters. Use the skip list to ignore "not interesting" rooms.
        """))
    async def alias_mud(self, cmd):
        class NotInMudlet(CachedPathChecker):
            @staticmethod
            async def check(room):
                # exits that are in Mudlet.

                if room.id_mudlet is None:
                    return SkipRoute
                if room in self.skiplist:
                    return None
                for x in room.exits:
                    if x.dst_id is not None and x.dst.id_mudlet is None:
                        return SkipSignal
                return Continue
        await self.gen_rooms(NotInMudlet())

    @doc(_(
        """
        Exits with unknown destination

        Find routes to (but not through) rooms with those exits.

        No parameters. Use the skip list to ignore "not interesting" rooms.
        """))
    async def alias_mux(self, cmd):
        class UnknownExit(PathChecker):
            @staticmethod
            async def check(room):
                if room.id_mudlet is None:
                    return SkipRoute
                if room in self.skiplist:
                    return Continue
                for x in room.exits:
                    if x.dst_id is None:
                        return SkipSignal
                return Continue
        await self.gen_rooms(UnknownExit())

    @doc(_(
        """
        Exits with unknown destination

        Find routes to rooms with those exits.
        Paths through those rooms are *not* skipped.

        No parameters. Use the skip list to ignore "not interesting" rooms.
        """))
    async def alias_muxx(self, cmd):
        class UnknownExit(PathChecker):
            @staticmethod
            async def check(room):
                if room.id_mudlet is None:
                    return SkipRoute
                if room in self.skiplist:
                    return Continue
                for x in room.exits:
                    if x.dst_id is None:
                        return SignalThis
                return Continue
        await self.gen_rooms(UnknownExit())

    @doc(_(
        """
        Database area not in Mudlet

        Given a room, list all reachable form there until we get to the
        Mudlet-mapped area.

        Parameter: the room to start in.
        """))
    async def alias_muf(self, cmd):
        cmd = self.cmdfix("r", cmd, min_words=1)

        gen = cmd[0].reachable.__aiter__()
        v = None
        while True:
            try:
                h = await gen.asend(v)
            except StopAsyncIteration:
                break
            r = h[-1]
            await self.print(r.idnn_str)
            if r.id_mudlet:
                v = SkipRoute
                continue
            v = SignalThis

    @doc(_(
        """
        Rooms without long descr

        Find routes to those rooms. Use #gg to use one.

        No parameters. Use the skip list to block ways to dangerous rooms.
        """))
    async def alias_mul(self, cmd):
        class NoLong(PathChecker):
            @staticmethod
            async def check(room):
                if room in self.skiplist:
                    return SkipRoute
                if not room.long_descr:
                    return SkipSignal
                return Continue
        await self.gen_rooms(NoLong())

    @doc(_(
        """Mudlet rooms not in the database

        Find routes to those rooms. Use #gg to use one.

        No parameters."""))
    async def alias_mum(self, cmd):
        class NotInDb(PathChecker):
            @staticmethod
            async def check(room):
                # These rooms are not in the database, so we check for unmapped
                # exits that are in Mudlet.
                mx = None
                if room in self.skiplist:
                    return SkipRoute
                for x in room.exits:
                    if x.dst_id is not None:
                        continue
                    if mx is None:
                        try:
                            mx = await room.mud_exits
                        except NoData:
                            break
                    if mx.get(x.dir, None) is not None:
                        return SkipSignal
                return Continue
        await self.gen_rooms(NotInDb())

    async def gen_rooms(self, checker, n_results=None, off=999999):
        """
        Generate a room list. The check function is called with
        the room.

        checkfn may return any of the relevant control objects in
        mudlet.const, True for SkipSignal, or False for SkipRoute.

        If n_results is 1, walk the first path immediately.

        """
        async def gen_reporter(d,r,h,res):
            if res.signal:
                if n_results == 1:
                    await self.print(_("{r.idnn_str} ({lh} steps)"), r=r,lh=len(h),d=d+1)
                else:
                    await self.print(_("#gg {d} : {r.idn_str} ({lh})"), r=r,lh=len(h),d=d+1)

        # If a prev generator is running, kill it
        await self.clear_gen()

        checker.reporter = gen_reporter
        try:
            async with PathGenerator(self, self.room, checker=checker, **({"n_results":n_results} if n_results else {})) as gen:
                self.path_gen = gen
                while await gen.wait_stalled():
                    await self.print(_("More results? #gn"))
                if self.path_gen is gen:
                    # check that it is still current
                    if n_results == 1:
                        if not gen.results:
                            await self.print(_("Cannot find a way there!"))
                        elif len(gen.results[0]) > 1:
                            self.add_start_room(gen.start_room)
                            await self.run_process(WalkProcess(self, gen.results[0][1][:off]))
                        else:
                            await self.print(_("Already there!"))
                        await self.clear_gen()
                        return
                    await self.print(_("No more results."))
                # otherwise we've been cancelled
        except Exception:
            self.path_gen = None
            raise

    async def found_path(self, n, room, h):
        await self.print(_("#gg {n} :d{lh} f{room.info_str}"), room=room, n=n, lh=len(h))

    def gen_next(self):
        evt, self._gen_next = self._gen_next, trio.Event()
        evt.set()

    @doc(_(
        """
        Path generator status
        """))
    async def alias_gi(self, cmd):
        if self.path_gen:
            await self.print("Path: "+str(self.path_gen))
        else:
            await self.print(_("No active path generator."))
        w = self.current_walker
        if w:
            await self.print("Walk: "+str(w))

    @doc(_(
        """
        Generate more paths

        Parameter:
        If more paths, their number, default 3.
        If resume walking, skip this many rooms, default zero.
        """))
    async def alias_gn(self, cmd):
        cmd = self.cmdfix("i", cmd)
        if self.path_gen:
            self.path_gen.make_more_results(cmd[0] if cmd else 3)
        else:
            await self.print(_("No path generator is active."))

    @doc(_(
        """
        Jump rooms
        Skip N rooms, default one.
        """))
    async def alias_gj(self, cmd):
        cmd = self.cmdfix("i", cmd)
        w = self.current_walker
        if w:
            w.resume(cmd[0] if cmd else 1)
        else:
            await self.print(_("No walker is active."))

    @property
    def current_walker(self):
        p = self.process
        while p:
            if isinstance(p, WalkProcess):
                return p
            p = p.upstack
        return None

    @doc(_(
        """
        Pause/resume walking
        The current walker, if any, is stopped or resumed.
        """))
    async def alias_gp(self, cmd):
        w = self.current_walker
        if not w:
            await self.print(_("No walker is active"))
        elif w.stopped:
            await w.resume()
        else:
            await w.pause()


    @doc(_(
        """
        Use one of the generator's paths
        No parameters: use the first result
        Otherwise: use the n'th result
        Param 2: how many rooms to skip at the end
        """))
    async def alias_gg(self, cmd):
        gen = self.path_gen
        if gen is None:
            await self.print(_("No route search active"))
            return
        if gen.results is None:
            if gen.is_running():
                await self.print(_("No route search results yet"))
            else:
                await self.print(_("No route search results. Sorry."))
            return
        cmd = self.cmdfix("ii",cmd)
        pos = cmd[0]-1 if cmd else 0
        off = -cmd[1] if len(cmd) > 1 and cmd[1]>0 else 999999
        if pos < len(gen.results):
            self.add_start_room(self.room)
            await self.run_process(WalkProcess(self, gen.results[pos][1][:off]))
        else:
            await self.print(_("I only have {lgr} results."), lgr=len(gen.results))

    async def show_path(self, dest, res):
        """
        Show the result of one path lookup
        """
        await self.print(dest.info_str)
        prev = None
        for room in res:
            if prev is None:
                d = _("Start")
            else:
                d = prev.exit_to(room).dir
            prev = room
            await self.print(f"{d}: {room.idnn_str}")

    @doc(_(
        """
        Show details for generated paths
        No parameters: short details for all results
        Otherwise: complete list for all results
        """))
    async def alias_gv(self, cmd):
        if self.path_gen is None:
            await self.print(_("No route search active"))
            return
        cmd = self.cmdfix("i",cmd)
        if cmd:
            await self.show_path(*self.path_gen.results[cmd[0]-1])

        else:
            i = 0
            if not self.path_gen.results:
                if self.path_gen.is_running():
                    await self.print(_("No route search results yet"))
                else:
                    await self.print(_("No route search results. Sorry."))
                return
            if self.room is None or self.room.id_old != self.path_gen.results[0][1][0]:
                room = self.db.r_old(self.path_gen.results[0][1][0])
                await self.print(_("Start at {room.idnn_str}:"), room=room)
            for dest,res in self.path_gen.results:
                i += 1
                res = res[1:]
                if len(res) > 7:
                    res = res[:2]+[None]+res[-2:]
                res = (self.db.r_old(r).idn_str if r else "…" for r in res)
                await self.print(f"{i}: {dest.idnn_str}")
                await self.print("   "+" ".join(res))

    @doc(_(
        """
        Return to previous room
        No parameter: list the last ten rooms you started a speedwalk from.
        Otherwise, go to the N'th room in the list.
        Param 2: stop N rooms before the goal
        """))
    async def alias_gr(self, cmd):
        cmd = self.cmdfix("ii", cmd)
        if not cmd:
            if self.start_rooms:
                for n,r in enumerate(self.start_rooms):
                    await self.print(_("{n}: {r.idn_str}"), r=r, n=n+1)
            else:
                await self.print(_("No rooms yet remembered."))
            return

        off = -cmd[1] if len(cmd) > 1 and cmd[1]>0 else 999999
        r = self.start_rooms[cmd[0]-1]
        await self.run_to_room(r, off)


    @with_alias("gr+")
    @doc(_(
        """
        Add a room to the #gr list
        Remember the current (or a numbered) room for later walking-back-to
        """))
    async def alias_gr_p(self, cmd):
        cmd = self.cmdfix("r", cmd)
        if cmd:
            room = cmd[0]
        else:
            room = self.room
        if room in self.start_rooms:
            await self.print(_("Room {room.id_str} is already on the list."), room=room)
            return
        self.add_start_room(cmd)


    def add_start_room(self, room):
        """
        Add a room to the list of start rooms.

        The new room is at the front of the queue. Duplicates are removed.
        """
        try:
            self.start_rooms.remove(room)
        except ValueError:
            pass
        self.start_rooms.appendleft(room)
        if len(self.start_rooms) > 10:
            self.start_rooms.pop()


    @doc(_(
        """
        skiplist for searches.
        Rooms on the list are skipped while searching.
        """))
    async def alias_gs(self,cmd):
        if not self.skiplist:
            await self.print(_("Skip list is empty."))
            return
        await self._wrap_list(*(x.idn_str for x in self.skiplist))

    async def _wrap_list(self, *words):
        res = []
        rl = 0
        maxlen = 100

        for w in words:
            if rl+len(w) >= maxlen:
                await self.print(" ".join(res))
                res = [""]
                rl = 1
            res.append(w)
            rl += len(w)+1
        if res:
            await self.print(" ".join(res))

    @with_alias("gs+")
    @doc(_(
        """
        Add room the skiplist.
        No room given: use the current room.
        """))
    async def alias_gs_p(self,cmd):
        db = self.db
        cmd = self.cmdfix("r", cmd)
        room = cmd[0] if cmd else self.view_or_room
        if not room:
            await self.print(_("No current room known"))
            return
        if room in self.skiplist:
            await self.print(_("Room {room.id_str} already is on the list."), room=room)
            return
        self.skiplist.add(room)
        await self.print(_("Room {room.idn_str} added."), room=room)
        db.commit()

    @with_alias("gs-")
    @doc(_(
        """
        Remove room from the skiplist.
        No room given: use the current room.
        """))
    async def alias_gs_m(self,cmd):
        db = self.db
        cmd = self.cmdfix("r", cmd)
        room = cmd[0] if cmd else self.view_or_room
        if not room:
            await self.print(_("No current room known"))
            return
        try:
            self.skiplist.remove(room)
            db.commit()
        except KeyError:
            await self.print(_("Room {room.id_str} is not on the list."), room=room)
        else:
            await self.print(_("Room {room.id_str} removed."), room=room)

    @with_alias("gs=")
    @doc(_(
        """
        Forget a / clear the current skiplist.
        If a name is given, delete the named list, else clear the current
        in-memory list"""))
    async def alias_gs_eq(self,cmd):
        db = self.db
        cmd = self.cmdfix("w", cmd)
        if cmd:
            cmd = cmd[0]
            try:
                sk = db.skiplist(cmd)
            except NoData:
                await self.print(_("List {cmd} doesn't exist"))
            else:
                db.delete(sk)
                db.commit()

        else:
            self.skiplist = set()
            await self.print(_("List cleared."))


    @doc(_(
        """
        Store the skiplist (by name)
        Stored lists are merged when one with the same name exists.
        No name given: list stored skiplists.
        """))
    async def alias_gss(self,cmd):
        db = self.db
        cmd = self.cmdfix("w", cmd)
        if not cmd:
            seen = False
            for sk in db.q(db.Skiplist).all():
                seen = True
                await self.print(_("{sk.name}: {lsk} rooms"), sk=sk, lsk=len(sk.rooms))
            if not seen:
                await self.print(_("No skiplists found"))
            return

        cmd = cmd[0]
        self.last_saved_skiplist = cmd
        sk = db.skiplist(cmd, create=True)
        for room in self.skiplist:
            sk.rooms.append(room)
        db.commit()
        await self.print(_("skiplist '{cmd}' contains {lsk} rooms."), cmd=cmd, lsk=len(sk.rooms))

    @doc(_(
        """
        Restore the named skiplist by merging with the current list.
        No name given: merge/restore the last-saved list.
        """))
    async def alias_gsr(self,cmd):
        db = self.db
        cmd = self.cmdfix("w", cmd)
        if cmd:
            cmd = cmd[0]
        else:
            cmd = self.last_saved_skiplist
            if not cmd:
                await self.print(_("No last-saved skiplist."))
                return
        try:
            sk = db.skiplist(cmd)
            onr = len(sk.rooms)
            for room in sk.rooms:
                self.skiplist.add(room)
        except NoData:
            await self.print(_("skiplist '{cmd}' not found."), cmd=cmd)
            return
        nr = len(self.skiplist)
        await self.print(_("skiplist '{cmd}' merged, now {nr} rooms (+{nnr})."), nr=nr, cmd=cmd, nnr=nr-onr)



    @doc(_(
        """Go to labeled room
        Find the closest room(s) with that label.
        Routes will not go through rooms on the current skiplist,
        but they may end at a room that is.
        """))
    async def alias_gt(self, cmd):
        cmd = self.cmdfix("w",cmd)
        if not cmd:
            await self.print(_("Usage: #gt Kneipe / Kirche / Laden"))
            return
        checker = MappedLabelSkipChecker(label=cmd[0].lower(), skiplist=self.skiplist)
        await self.gen_rooms(checker=checker)

    @doc(_(
        """Go to first labeled room
        Find the closest room with that label and immediately go there.
        Routes will not go through rooms on the current skiplist,
        but they may end at a room that is.
        """))
    async def alias_gtt(self, cmd):
        cmd = self.cmdfix("w",cmd)
        if not cmd:
            await self.print(_("Usage: #gt Kneipe / Kirche / Laden"))
            return
        checker = MappedLabelSkipChecker(label=cmd[0].lower(), skiplist=self.skiplist)
        await self.gen_rooms(checker=checker, n_results=1)


    @doc(_(
        """Go to a not-recently-visited room
        Find the closest room not visited recently.
        Routes will not go through rooms on the current skiplist,
        but they may end at a room that is on it.
        Adding a number uses that as the minimum counter.
        """))
    async def alias_mv(self, cmd):
        cmd = self.cmdfix("i",cmd)
        lim = cmd[0] if cmd else 1

        checker = MappedVisitSkipChecker(last_visit=lim, skiplist=self.skiplist)
        await self.gen_rooms(checker, n_results=1)


    @doc(_(
        """Find something.
        Find the closest room(s) with that string in its notes
        or its long description.
        Routes will not go through rooms on the current skiplist,
        but they may end at a room that is.
        """))
    async def alias_gf(self, cmd):
        cmd = self.cmdfix("*",cmd)
        if not cmd:
            await self.print(_("Usage: #gf busch"))
            return
        txt = cmd[0].lower()

        checker = MappedFulltextSkipChecker(txt=txt, skiplist=self.skiplist)
        await self.gen_rooms(checker)

    @doc(_(
        """Find something.
        Find the closest room(s) with that string in one of the things there.
        Routes will not go through rooms on the current skiplist,
        but they may end at a room that is.
        """))
    async def alias_gft(self, cmd):
        cmd = self.cmdfix("*",cmd)
        if not cmd:
            await self.print(_("Usage: #gft ruestung"))
            return
        txt = cmd[0].lower()

        checker = MappedThingSkipChecker(thing=txt, skiplist=self.skiplist)
        await self.gen_rooms(checker)

    async def clear_gen(self):
        """Cancel path generation"""
        if self.path_gen:
            await self.path_gen.cancel()
            self.path_gen = None

    @doc(_(
        """
        Cancel path generation and whatnot.

        No parameters.
        """))
    async def alias_gc(self, cmd):
        await self.clear_gen()

    @doc(_(
        """
        Exits (short)
        Print a one-line list of exits of the current / a given room"""))
    async def alias_x(self, cmd):
        cmd = self.cmdfix("r",cmd)
        room = cmd[0] if cmd else self.view_or_room
        if not room:
            await self.print(_("No current room known"))
            return
        await self.print(room.exit_str)

    @doc(_(
        """
        Exits (long)
        Print a multi-line list of exits of the current / a given room"""))
    async def alias_xx(self, cmd):
        cmd = self.cmdfix("r",cmd)
        room = cmd[0] if cmd else self.view_or_room
        if not room:
            await self.print(_("No current room known"))
            return
        exits = room.exits
        rl = max(len(x.dir) for x in exits)
        for x in exits:
            d = x.dir+" "*(rl-len(x.dir))
            if x.dst is None:
                await self.print(_("{d} - unknown"), d=d)
            else:
                await self.print(_("{d} = {dst.info_str}"), dst=x.dst, d=d)


    @doc(_(
        """Detail info for current room / a specific room"""))
    async def alias_ri(self, cmd):
        cmd = self.cmdfix("r",cmd)
        room = (cmd[0] if cmd else None) or self.view_or_room
        if not room:
            await self.print(_("No current room known!"))
            return
        await self.print(room.info_str)
        if room.note:
            await self.print(room.note.note)

    @doc(_(
        """Internal room info"""))
    async def alias_rii(self, cmd):
        cmd = self.cmdfix("r",cmd)
        room = (cmd[0] if cmd else None) or self.view_or_room
        if not room:
            await self.print(_("No current room known!"))
            return
        await self.print(room.info_str)
        await self.print(_("old={room.id_old} mudlet={room.id_mudlet} gmcp={room.id_gmcp} flag={room.flag}"), room=room)
        await self.print(_("pos={room.pos_x}/{room.pos_y}/{room.pos_z} layer={areaname}"), room=room, areaname=room.area.name if room.area else "-")

    @doc(_(
        """Things/NPCs in current room / a specific room"""))
    async def alias_rd(self, cmd):
        db = self.db
        cmd = self.cmdfix("r",cmd)
        room = (cmd[0] if cmd else None) or self.view_or_room
        if not room:
            await self.print(_("No current room known!"))
            return
        await self.print(room.info_str)
        for s in db.q(db.Thing).filter(db.Thing.rooms.contains(room)).all():
            await self.print(s.name)

    @doc(_(
        """Show rooms that have exits to this one"""))
    async def alias_rq(self, cmd):
        cmd = self.cmdfix("r",cmd)
        room = (cmd[0] if cmd else None) or self.view_or_room
        if not room:
            await self.print(_("No current room known!"))
            return
        for x in room.r_exits:
            await self.print(x.src.info_str)

    @doc(_(
        """Info for selected room(s) on the map"""))
    async def alias_rm(self, cmd):
        sel = await self.mud.getMapSelection()
        if not sel or not sel[0]:
            await self.print(_("No room selected."))
            return
        sel = sel[0]
        for r in sel["rooms"]:
            room = self.db.r_mudlet(r)
            await self.print(room.info_str)

    @with_alias("g#")
    @doc(_(
        """
        Walk to a mapped room.
        This overrides any other walk code
        Parameter: the room's ID.
        """))
    async def alias_g_h(self, cmd):
        cmd = self.cmdfix("ri", cmd, min_words=1)
        dest = cmd[0]
        off = -cmd[1] if len(cmd) > 1 and cmd[1]>0 else 999999
        await self.run_to_room(dest, off)

    async def run_to_room(self, room, off=999999):
        """Run from the current room to the mentioned room."""
        if self.room is None:
            await self.print(_("No current room known!"))
            return

        if isinstance(room,int):
            room = self.db.r_old(room)
        await self.clear_gen()

        self.add_start_room(self.room)
        await self.gen_rooms(MappedRoomFinder(room=room), n_results=1, off=off)

    @doc(_(
        """
        Show route to a room / between two rooms
        Walk code is unaffected.
        Parameter: [start and] goal room ID(s).

        This includes rooms not yet on the Mudlet map.
        """))
    async def alias_gd(self, cmd):
        cmd = self.cmdfix("rr", cmd, min_words=1)
        dest = cmd[-1]
        src = cmd[0] if len(cmd) > 1 else self.view_or_room

        await self.print(_("Scanning the map"))
        async with PathGenerator(self, src, RoomFinder(dest), n_results=1) as gen:
            while await gen.wait_stalled():
                raise RuntimeError("A lookup with n=1 cannot stall")
            if not gen.results:
                await self.print(_("Cannot find a way there!"))
            else:
                await self.show_path(*gen.results[0])

    @doc(_(
        """Recalculate colors
        A given room, or all of them"""))
    async def alias_mcr(self, cmd):
        db = self.db
        cmd = self.cmdfix("r", cmd)
        if len(cmd):
            room = cmd[0]
            if room.id_mudlet:
                await self.update_room_color(room)
            else:
                await self.print(_("Not yet mapped: {room.idn_str}"), room=room)
            return
        for room in db.q(db.Room).filter(db.Room.id_mudlet != None):
            await self.update_room_color(room)
        await self.mud.updateMap()

    async def get_named_area(self, name, create=False):
        db = self.db
        try:
            area = self._area_name2area[name.lower()]
            if create is True:
                raise ValueError(_("Area '{name}' already exists").format(name=name))
        except KeyError:
            # ask the MUD to make a new one
            if create is False:
                raise ValueError(_("Area '{name} does not exist").format(name=name))
            aid = await self.mud.addAreaName(name)
            if len(aid) > 1:
                aid,err = aid
            else:
                aid = aid[0]
            if aid is None or aid < 1 or err:
                await self.print(_("Could not create area {name!r}: {err}"), name=name,err=err or _("unknown error"))
                return None

            area = self.db.Area(id=aid, name=name)
            db.add(area)
            db.commit()
        return area

    async def send_commands(self, name, *cmds, err_str="", move=False, exit=None, send_echo=True, now=False):
        """
        Send this list of commands to the MUD.
        The list may include special processing or delays.
        """
        if move:
            PP = MoveProcess
        else:
            PP = SeqProcess
        p = PP(self, name, cmds, exit=exit, send_echo=send_echo)
        await self.run_process(p)
        if now:
            self._send_recheck = True

    async def sync_from_mudlet(self, *rooms, clear=False):
        """
        Given these rooms, follow those of their exits that are only in
        Mudlet. Create targets. Recurse.

        Mudlet coordinates are expanded by pos_x/y_delta if "mudlet_explode"
        is set.

        If @clear is set, any old associations are deleted.
        """
        db=self.db
        done = set()
        broken = set()
        todo = deque()
        explore = set()
        more = set()
        area_names = {int(k):v for k,v in (await self.mud.getAreaTableSwap())[0].items()}
        area_known = set()
        area_rev = {}
        for k,v in area_names.items():
            area_rev[v] = k
        self.__logger.debug("AREAS:%r",area_names)
        await self.print(_("Start syncing. Please be patient."))

        if clear:
            r_old = {}
            for r in rooms:
                r_old[r.id_old] = id_mudlet
            db.q(db.Room).update().values(id_mudlet = None).execute()
            for r in rooms:
                r.id_mudlet = r_old[r.id_old]

        for r in rooms:
            todo.append(r)

        def know_area(self, ra):
            if isinstance(ra,str):
                ra = area_rev[area]
            if ra not in area_known:
                if db.q(db.Area).filter(db.Area.id == ra).one_or_none() is None:
                    a = db.Area(id=ra,name=area_names[ra])
                    db.add(a)
                    db.commit()
                area_known.add(ra)
            return ra

        while todo:
            r = todo.pop()
            if r.id_old in done:
                continue
            done.add(r.id_old)
            if not (len(done)%100):
                await self.print(_("{done} rooms ..."), done=len(done))

            try:
                y = await r.mud_exits
            except NoData:
                # didn't go there yet?
                self.__logger.debug("EXPLORE %s %s",r.id_str,r.name)
                explore.add(r.id_old)
                continue

            # Iterate exits but do the "standard" directions first,
            # then the reversible nonstandard ways, then the others.
            def exits(y):
                for d,mid in y.items():
                    if self.dr.is_std_dir(d):
                        yield d,mid
                for d,mid in y.items():
                    if not self.dr.is_std_dir(d) and self.dr.loc2rev(d) is not None:
                        yield d,mid
                for d,mid in y.items():
                    if not self.dr.is_std_dir(d) and self.dr.loc2rev(d) is None:
                        yield d,mid

            for d,mid in exits(y):
                if not mid:
                    continue  # unknown
                try:
                    x = r.exit_at(d)
                    nr = x.dst
                except KeyError:
                    x = None
                    nr = None
                if nr is not None:
                    continue

                try:
                    if nr is None:
                        nr = db.r_mudlet(mid)
                    elif nr.id_mudlet is not None:
                        continue
                except NoData:
                    name = await self.mud.getRoomName(mid)
                    name = self.dr.clean_shortname(name[0]) if name and name[0] else None
                    gmcp = await self.mud.getRoomHashByID(mid)
                    gmcp = gmcp[0] if gmcp and gmcp[0] else None

                    nr = await self.new_room(name, id_gmcp=gmcp, id_mudlet=mid, offset_from=r, offset_dir=d, explode=self.conf['mudlet_explode'])

                if x is None:
                    x,_xf = await r.set_exit(d,nr,skip_mud=True)
                elif x.dst is None:
                    x.dst = nr
                todo.append(nr)
            db.commit()

        await self.print(_("Finished, {done} rooms processed"), done=len(done))

    @asynccontextmanager
    async def input_grab_multi(self):
        w,r = trio.open_memory_channel(1)
        try:
            async def send(x):
                if x == ".":
                    await w.aclose()
                    self._input_grab = None
                    return
                await w.send(x)
            if self._input_grab is not None:
                raise RuntimeError("already capturing")
            self._input_grab = send
            yield r
        finally:
            with trio.move_on_after(2) as cg:
                cg.shield = True
                if self._input_grab is send:
                    self._input_grab = None
                    await w.aclose()
                    # otherwise done above

    @asynccontextmanager
    async def input_grab(self):
        evt = ValueEvent()
        try:
            async def send(x):
                evt.set(x)
                self._input_grab = None
            if self._input_grab is not None:
                raise RuntimeError("already capturing")
            self._input_grab = send
            yield evt
        finally:
            if self._input_grab is send:
                self._input_grab = None

    async def called_input(self, msg):
        """
        Text which the user entered
        """
        if self._input_grab:
            await self._input_grab(msg)
            return None
        if msg.strip() in self.blocked_commands:
            await self.print(_("You really shouldn't send {msg!r}."), msg=msg)
            return None

        if not self.room:
            return msg
        if msg.startswith("#"):
            return None
        if msg.startswith("lua "):
            return None
        self.cmd3_q.append((msg,False))
        self.trigger_sender.set()

    async def _do_manual_command(self, msg, send_echo=None):
        """
        return None if the input has been handled.
        Otherwise return the same (or some other) text to be sent.
        """
        self.this_exit = None

        ms = self.dr.short2loc(msg)
        if self.room is None:
            await self.send_commands(msg, msg)
        else:
            self.last_cmd = msg
            try:
                x = self.room.exit_at(msg)
            except KeyError:
                await self.send_commands(msg, msg, send_echo=False if send_echo is None else send_echo)
            else:
                self.this_exit = x
                await self.send_exit_commands(x, send_echo=send_echo)

        if self.logfile:
            print(">>>",msg, file=self.logfile)

    async def send_exit_commands(self, x, send_echo=None):
        m = list(x.moves)
        if x.feature:
            m = x.feature.enter_moves + m
        if x.back_feature:
            m = m + x.back_feature.exit_moves
        await self.send_commands(x.dir, *m, send_echo=bool(x.steps) if send_echo is None else send_echo)


    @doc(_(
        """
        Stored long description
        Print the current long descr from the database
        """))
    async def alias_rl(self, cmd):
        cmd = self.cmdfix("r", cmd)
        room = cmd[0] if cmd else self.view_or_room
        if not room:
            await self.print(_("No current room known!"))
            return
        if room.long_descr:
            await self.print(_("{room.info_str}:\n{room.long_descr.descr}"), room=room)
        else:
            await self.print(_("No text known for {room.idnn_str}"), room=room)
        if room.note:
            await self.print(_("* Note:\n{room.note.note}"), room=room)

    async def called_fnkey(self, key, mod):
        try:
            s = self.keymap[mod][key]
        except KeyError:
            await self.print(f"No shortcut {mod}/{key} found.")
        else:
            if isinstance(s,self.db.Keymap):
                s = s.text
            if isinstance(s,str):
                self.cmd3_q.append((s,True))
                self.trigger_sender.set()
                return
            if isinstance(s,Command):
                s = (s,)
            await self.send_commands("",*s)


    # ### Basic command handling ### #


    async def called_prompt(self, msg):
        """
        Called by Mudlet when the MUD sends a Telnet GA
        signalling readiness for the next line
        """
        self.__logger.debug("Queue GA")
        raise PostEvent("prompt")

    async def event_prompt(self,msg):
        self.prompt(self.MP_TELNET)

    @doc(_(
        """Go-ahead, send next
        Use this command if the engine fails to recognize
        the MUD's prompt / Telnet GA message"""))
    async def alias_ga(self,cmd):
        self.prompt(self.MP_ALIAS)


    def send_command(self, cmd, cls=Command):
        """
        Send a single command to the MUD.
        """
        if isinstance(cmd,str):
            cmd = cls(self, command=cmd)
        self.cmd2_q.append(cmd)
        self.trigger_sender.set()
        return cmd

    async def run_process(self, p):
        """
        Run a complex command.
        """
        assert isinstance(p, Process)
        await p.setup()

    async def check_more(self, msg):
        if not msg.startswith("--mehr--("):
            return False
        await self.mud.send("\n",False, noreply=True)
        return True

    async def log_text(self, msg):
        if self.logfile:
            print(msg, file=self.logfile)

    async def called_text(self, msg, colors=None):
        """Incoming text from the MUD"""
        # TODO: store and interpret colors
        if await self.check_more(msg):
            return
        if self.logfile:
            print(msg, file=self.logfile)
        m = self.is_prompt(msg)
        if isinstance(m,str):
            msg = m
            self.prompt(self.MP_TEXT)
            if not msg:
                return

        msg = msg.rstrip()
        self.__logger.debug("IN  : %s", msg)
        if self.filter_text(msg) is False:
            return

        if not self.filter_exit(msg):
            if self.command:
                self.command.add(msg)

        await self._to_text_watchers(msg)
        await self._text_w.send(msg)

    async def _to_text_watchers(self, msg):
        for m in list(self._text_monitors):
            try:
                m.send_nowait(msg)
            except trio.WouldBlock:
                await m.aclose()
                self._text_monitors.discard(m)
            except trio.ClosedResourceError:
                self._text_monitors.discard(m)

    @asynccontextmanager
    async def text_watch(self):
        """
        Monitor the text stream from the MUD.

        Usage:
            async with S.text_watch() as tw:
                async for line in tw:
                    ...
        """
        w,r = trio.open_memory_channel(100)
        self._text_monitors.add(w)
        try:
            yield r
        finally:
            self._text_monitors.discard(w)
            await w.close()

    @doc(_("""
        List keymap shortcuts
    """))
    async def alias_kl(self, cmd):
        cmd = self.cmdfix("ii", cmd)
        if len(cmd) == 0:
            await self.print(_("Known modifiers: ")+" ".join(str(x) for x in self.keymap.keys()))
        elif len(cmd) == 1:
            try:
                km = self.keymap[cmd[0]]
            except KeyError:
                await self.print(_("No such modifier defined."))
            else:
                await self.print(_("Known keycodes: ")+" ".join(str(x) for x in km.keys()))
        else:
            try:
                k = self.keymap[cmd[0]][cmd[1]]
            except KeyError:
                await self.print(_("No such key defined."))
            else:
                if isinstance(k,self.db.Keymap):
                    await self.print(_("Send {key!r} (stored)"), key=k.text)
                else:
                    await self.print(_("Send {key!r} (default)"), key=k)

    @doc(_("""
        Dump keymap
        Parameter: modifier
    """))
    async def alias_kll(self, cmd):
        cmd = self.cmdfix("i", cmd, min_words=1)
        try:
            km = self.keymap[cmd[0]]
        except KeyError:
            await self.print(_("No such modifier defined."))
        else:
            if km:
                for c,k in km.items():
                    if isinstance(k,self.db.Keymap):
                        k = k.text
                    await self.print(_("{code}: Send {key!r}"), code=c, key=k)
            else:
                await self.print(_("empty."))

    @doc(_("""
        Add a keymap shortcut
    """))
    @with_alias("k+")
    async def alias_k_p(self, cmd):
        db = self.db
        cmd = self.cmdfix("ii*", cmd, min_words=3)
        try:
            km = self.keymap[cmd[0]]
        except KeyError:
            self.keymap[cmd[0]] = km = dict()
        try:
            k = km[cmd[1]]
            if isinstance(k,str):
                raise KeyError("default")
        except KeyError:
            km[cmd[1]] = k = db.Keymap(mod=cmd[0], val=cmd[1])
            db.add(k)
        k.text = cmd[2]
        db.commit()


    async def alias_dss(self, cmd):
        """
        Sender state
        When used with a number, dump its n'th command
        """
        cmd = self.cmdfix("iiiiiiiii", cmd)
        await self._state_dumper(self.print, cmd)

    async def _log_state(self):
        async def _logger(msg, **kw):
            msg = self._format(msg, kw)
            self.__logger.debug(msg)
        await self._state_dumper(_logger)

    async def _state_dumper(self, printer, cmd=()):
        if len(cmd):
            x = self.process
            n = 1
            while n < cmd[0]:
                x = x.upstack
                n += 1
            cmd = cmd[1:]
            while cmd:
                x = x.commands[cmd[0]-1]
                cmd = cmd[1:]
            await printer(_("Target: {cmd!r}"), cmd=x)
            if hasattr(x,"cmds"):
                for i,command in enumerate(x.cmds):
                    await printer(_("Step {n}: {cmd}"), n=i+1, cmd=command)
            if hasattr(x,"commands"):
                for i,command in enumerate(x.commands):
                    await printer(_("Sub {n}: {cmd!r}"), n=i+1, cmd=command)
            return

        await printer(_("Send Recheck {recheck!r}"), recheck=self._send_recheck)
        if not self.command:
            await printer(_("No command"))
        else:
            await printer(_("Command: {s.command}"), s=self)
        if not self._prompt_evt:
            await printer(_("Not waiting for prompt"))
        elif self._prompt_evt.is_set():
            await printer(_("Prompt trigger set"))
        else:
            await printer(_("Waiting for prompt (state={ps})"), ps=self._prompt_state)
        if not self.trigger_sender.is_set():
            await printer(_("waiting for continuation"))

        for x in self.cmd1_q:
            await printer(_("Mudlet: {x!r}"), x=x)
        for x in self.cmd2_q:
            await printer(_("Mapper: {x!r}"), x=x)
        for x in self.cmd3_q:
            await printer(_("Input: {x!r}"), x=x)

        if self.this_exit:
            await printer(_("Current Move: {x.dir} from {x.src.idn_str}"), x=self.this_exit)
        else:
            x = self.current_exit
            if x:
                await printer(_("Last Move: {x.dir} from {x.src.idn_str}"), x=x)

        x = self.process
        n = 0
        while x:
            n += 1
            await printer(_("Stack {n}: {x!r}"), n=n, x=x)
            x = x.upstack

    @with_alias("##")
    @doc(_("""
        Sender reset
        Shortcut to '#dsr' for emergencies
    """))
    async def alias_hash_hash(self, cmd):
        await self._reset_stack()

    async def alias_dsr(self, cmd):
        """
        Sender reset
        """
        await self._reset_stack()

    async def _reset_stack(self):
        self.command = None
        # self.cmd1_q = []  # already sent, so no
        self.cmd2_q = []
        self.cmd3_q = []
        while self.process is not self.top:
            p = self.process
            try:
                await p.finish()
                if self.process is p:
                    raise RuntimeError("No takedown")
            except Exception as exc:
                await self.print(f"ERROR {p!r}: {exc!r}")
                self.__logger.exception(f"Takedown {p!r}")
                self.process = None
        if self._prompt_evt:
            self._prompt_evt.set()
        await self.print(_("Cleared."))

    @with_alias("#")
    @doc(_("""
        Stop automation
        Halt command execution until '#g'.
    """))
    async def alias__hash(self, cmd):
        p = FixProcess(self, "halt", "paused by console command")
        await self.run_process(p)

    @with_alias("#g")
    @doc(_("""
        Sender unblock
        Unblock command execution and continue.
    """))
    async def alias__hash_g(self, cmd):
        p = self.process
        if p is None:
            await self.print(_("No command is running."))
        elif not isinstance(p, FixProcess):
            await self.print(_("Sorry but the current command is {cmd!r}."), cmd=p)
            self.trigger_sender.set()
        else:
            await p.finish()
            await self.print(_("The current command is now {cmd!r}."), cmd=self.process)
            self.trigger_sender.set()


    async def _send_loop(self):
        """
        Main send-some-commands loop.
        """
        self._send_recheck = True
        while True:
            try:
                if self.cmd1_q:
                    # Command transmitted by Mudlet.
                    cmd = self.cmd1_q.pop(0)
                    self.__logger.debug("Prompt saw %r",cmd)
                    xmit = False
                elif self.cmd2_q:
                    # Command queued here.
                    # There should be only one, but sometimes the user is
                    # faster than the MUD.
                    cmd = self.cmd2_q.pop(0)
                    xmit = True
                else:
                    # No command queued, so we ask the stack.
                    if self._send_recheck:
                        self._send_recheck = False
                        await self.process.next()
                    elif self.cmd3_q:
                        cmd,echo = self.cmd3_q.pop(0)
                        await self._do_manual_command(cmd, send_echo=echo)
                        self._send_recheck = True
                    else:
                        await self.trigger_sender.wait()
                        self.trigger_sender = trio.Event()
                        self._send_recheck = True
                    continue
                self._send_recheck = True

                if self.command is not None:
                    await self.command.done()
                if isinstance(cmd,str):
                    cmd = Command(self,command=cmd)

                self.last_commands.appendleft(cmd)
                if len(self.last_commands) > 10:
                    self.last_commands.pop()
                self.command = cmd

                self._prompt_evt = trio.Event()
                self._prompt_state = None
                if self.logfile:
                    print(">>>",cmd.command, file=self.logfile)
                if xmit:
                    await self.mud.send(cmd.command, cmd.send_echo, noreply=True)
                else:
                    cmd.send_seen = True
                await self._prompt_evt.wait()
                self._prompt_evt = None
                await self._process_prompt()
                await self._wait_output()

            except Exception as exc:
                await self.print(f"*** internal error: {exc!r} ***")
                self.__logger.exception("internal error: %r", exc)
                await self._reset_stack()
                if self._prompt_evt:
                    # should not happen but be safe
                    self._prompt_evt.set()
                    self._prompt_evt = None

    async def _wait_output(self):
        evt = trio.Event()
        await self._text_w.send(evt)
        await evt.wait()

    def filter_exit(self, msg):
        m = self.exit_matcher
        if m is not None:
            if m.match_line(msg):
                return True
        else:
            m = self.dr.match_exits(msg)
            if m:
                self.exit_matcher = m
                if self.command:
                    self.command.dir_seen(m)
                return True
        return False

    ### mud specific

    def is_prompt(self, msg):
        """
        If the line is / starts with / contains the MUD's input prompt,
        remove it and return the rest of the line. 

        Otherwise return None.
        """
        if msg.startswith("> "):
            return msg[2:]

    def filter_text(self, msg):
        """
        Filter the incoming text.
        Return False if it should be suppressed
        """
        if msg.startswith("Der GameDriver teilt Dir mit:"):
            return False


    @property
    def waiting_for_prompt(self):
        return self._prompt_evt is not None

    def _trigger_prompt(self, cause):
        self._prompt_state = None
        p = self._prompt_evt
        if p is not None:
            p.set()

    async def _trigger_prompt_later(self, timeout):
        p = self._prompt_evt
        if p is None:
            return
        with trio.move_on_after(timeout):
            await p.wait()
            return  # not timed out if we get here
        p.set()

    def prompt(self, mode=None):
        """
        We see a prompt from the MUD: finish processing and send the next command

        Mode is:
        MP_TEXT: '> ' seen
        MP_TELNET: GoAhead received
        MP_ALIAS: '#ga' entered
        MP_INFO: new room info message seen
        """
        if self.logfile:
            self.logfile.flush()

        if self._prompt_evt is None:
            return

        if mode == self.MP_ALIAS:
            self._trigger_prompt("D")
            return

        if mode == self.MP_TEXT:
            if self.cmd1_q:
                # The user typed a command while we were working.
                self._trigger_prompt("A")
            elif self._prompt_state is None:
                self._prompt_state = True
                self.main.start_soon(self._trigger_prompt_later, 0.5)
            elif self._prompt_state is False:
                self._trigger_prompt("B")
            return

        if mode == self.MP_TELNET:
            if self._prompt_state is None:
                self._prompt_state = False
                self.main.start_soon(self._trigger_prompt_later, 0.5)
            elif self._prompt_state is True:
                self._trigger_prompt("C")
            return

        if mode == self.MP_INFO:
            self.main.start_soon(self._trigger_prompt_later, 1.0)
            return

        raise RuntimeError("Unknown prompt mode %r" % (mode,))


    async def _process_prompt(self):
        """
        A prompt has been seen. Finish the current command,
        which may include creating a new room / moving the avatar.
        """
        exits_seen = False
        m = self.exit_matcher
        if m is not None:
            if not self.exit_matcher.prompt():
                await self.print(_("WARNING: Exit matching failed halfway!"))
            exits_seen = True
        self.exit_matcher = None
        await self._to_text_watchers(None)

        # finish the current command
        s = self.command
        if s is None:
            # no command
            return
        self.command = None
        await s.done()
        self.process.append(s)
        s.exits_seen = exits_seen

        # figure out whether we should run the "moved" code
        r = self.room
        if not r:
            # No source room, so simply set to GMCP if present
            if s.info and s.info.get("id"):
                rn = self.db.r_hash(s.info["id"])
                if rn is not None:
                    await self.went_to_room(rn)
            # TODO uniqueness test via room shortname / description
            return

        # at this point we have an old room.
        # In case of GMCP we might have a new room to set the map to.
        # (This is purely informational.)

        nr = None
        moved = False
        if s.info:
            i_gmcp = s.info.get("id","")

            # Changing info indicates movement.
            nr = self.room_by_gmcp(i_gmcp)
            if nr is not None:
                if nr is not r and nr.id_mudlet is not None and self.conf['show_intermediate']:
                    await self.mud.centerview(nr.id_mudlet)
                moved = r.id_gmcp != nr.id_gmcp
            elif i_gmcp or self.room.id_gmcp:
                moved = True
            elif i_short and self.dr.clean_shortname(r.name) != self.dr.clean_shortname(i_short) and not (r.flag & self.db.Room.F_MOD_SHORTNAME):
                await self.print(_("Shortname differs. #mm?"))

        return  ### !!!

        # If we're in the middle of a known move then skip the rest because
        # the processor will call it when it is done.
        if self.this_exit is not None:
            return

        await self._end_command(s.command,nr,moved,exits_seen)


    async def finish_move(self, d, moved):
        """
        Called when a SeqProcess ends
        """
        mv = self.process.last_move
        nr = None
        if self.this_exit:
            nr = self.this_exit.dst
            if mv is None and (nr is None or not (nr.flag & self.db.Room.F_NO_GMCP_ID)):
                return
        await self._end_command(d,nr,moved,None)


    async def _end_command(self,d,nr,moved,exits_seen):
        x = self.this_exit
        if x is None:
            d = self.dr.short2loc(d)
            if self.room is not None:
                try:
                    x = self.room.exit_at(d)
                except KeyError:
                    x = None
        if x is not None:
            d = x.dir

        move_cmd = self.process.last_move
        info = move_cmd.info if move_cmd else None
        r = self.room

        moved = False
        if info:
            i_gmcp = info.get("id","")

            # Changing info indicates movement.
            nr = self.room_by_gmcp(i_gmcp)
            if nr is not None:
                moved = r.id_gmcp != nr.id_gmcp
                if not moved and not x:
                    nr = None
            elif i_gmcp or self.room.id_gmcp:
                moved = True
            elif i_short and self.dr.clean_shortname(r.name) != self.dr.clean_shortname(i_short) and not (r.flag & self.db.Room.F_MOD_SHORTNAME):
                await self.print(_("Shortname differs. #mm?"))

        if x and not exits_seen:
            if x.dst and x.dst.flag & self.db.Room.F_NO_EXIT:
                exits_seen = True
        elif exits_seen is None and move_cmd is not None:
            exits_seen = move_cmd.exits_seen

        if move_cmd and isinstance(move_cmd, LookCommand):
            # This command does not generate movement. Thus if it did
            # anyway the move was probably timed, except when the exit
            # already exists. Life is complicated.
            if x is None:
                d = TIME_DIR

        if exits_seen:
            if x is not None:
                # There is an exit thus we seem to have used it
                moved = True
            elif self.dr.is_std_dir(d):
                # Standard directions generally indicate movement.
                # Exit may be 'created' by opening a door or similar.
                moved = True

        if nr or moved:
            # Consume the movement.
            self.this_exit = None

            await self.went_to_dir(d, info=info, exits_text=move_cmd.exits_text)
        self.db.commit()

    def room_by_gmcp(self, id_gmcp):
        if not id_gmcp:
            return None
        try:
            return self.db.r_hash(id_gmcp)
        except NoData:
            return None

    async def process_exits_text(self, room, txt):
        for x in self.exits_from_line(txt):
            try:
                room.exit_at(x)
            except KeyError:
                await room.set_exit(x)
                await self.print(_("New exit: {exit}"), exit=x,room=room)


    async def handle_event(self, msg, evt=None):
        if msg:
            evt = msg[0]
        name = evt.replace(".","_")
        hdl = getattr(self.dr, "event_"+name, None)
        if hdl is not None:
            await hdl(msg)
            return
        hdl = getattr(self, "event_"+name, None)
        if hdl is not None:
            await hdl(msg)
            return
        if msg[0].startswith("gmcp.") and msg[0] != msg[1]:
            # not interesting, will show up later
            return
        if msg[0] == "sysTelnetEvent":
            msg[3] = "".join("\\x%02x"%b if b<32 or b>126 else chr(b) for b in msg[3].encode("utf8"))
        self.__logger.debug("%r", msg)

    async def initGMCP(self):
        for s,d in self.dr.gmcp_setup_data():
            await self.mud.sendGMCP(s+" "+json.dumps(d), noreply=True)

    async def event_sysProtocolEnabled(self, msg):
        if msg[1] == "GMCP":
            await self.initGMCP()

    async def event_sysWindowResizeEvent(self, msg):
        pass

    async def event_sysManualLocationSetEvent(self, msg):
        try:
            room = self.db.r_mudlet(msg[1])
        except NoData:
            if self.room and not self.room.id_mudlet:
                # this mostly can't happen.
                self.room.set_id_mudlet(msg[1])
                self.db.commit()
                await self.print(_("MAP: Room ID is now {room.id_str}."), room=self.room)
            else:
                await self.print(_("MAP: I do not know room ?/{id}."), id=msg[0])
            room = None
        self.room = room
        await self.view_to(None)
        self.next_word = None
        self.trigger_sender.set()
        await self.dr.show_room_data(room)

    async def event_sysDataSendRequest(self, msg):
        """
        This will report both the commands we send, and those Mudlet emits
        when bypassing our macros.
        The latter isn't supposed to happen, but ...
        """
        self.__logger.debug("OUT : %s", msg[1])
        if not self.command or self.command.send_seen:
            pass
        elif self.command.command != msg[1]:
            await self.print(_("WARNING last_command differs, stored {sc!r} vs seen {lc!r}"), sc=self.command.command,lc=msg[1])
        else:
            self.command.send_seen = True
            return

        self.cmd1_q.append(msg[1])
        self.trigger_sender.set()

    def current_command(self, no_info=False):
        c = self.command
        if c is not None:
            if not no_info or not c.info:
                return c
        self.__logger.debug("*** IDLE ***")
        c = IdleCommand(self)
        self.process.append(c)
        return c

    def maybe_trigger_sender(self):
        if self._prompt_evt is None:
            # not waiting, so process it from the main loop
            self.trigger_sender.set()

    async def new_room(self, descr="", *, id_gmcp=None, id_mudlet=None,
            offset_from=None, offset_dir=None, area=None, explode=False):

        if self.conf['debug_new_room']:
            import pdb;pdb.set_trace()

        if id_mudlet:
            try:
                room = self.db.r_mudlet(id_mudlet)
            except NoData:
                if not descr:
                    descr = (await self.mud.getRoomName(id_mudlet))[0]
                if area is None:
                    area = (await self.mud.getRoomArea(id_mudlet))[0]
                    area = self._area_id2area[area]
            else:
                if offset_from and offset_dir:
                    self.__logger.error(_("Not in mudlet? but we know mudlet# {id_mudlet} at {offset_room.id_str}/{offset_dir}").format(offset_room=offset_room, offset_dir=offset_dir, id_mudlet=id_mudlet))
                else:
                    self.__logger.error(_("Not in mudlet? but we know mudlet# {id_mudlet}").format(id_mudlet=id_mudlet))
                return None

        if id_gmcp:
            try:
                room = self.db.r_hash(id_gmcp)
            except NoData:
                pass
            else:
                self.__logger.error(_("New room? but we know hash {hash} at {room.id_old}").format(room=room, hash=id_gmcp))
                return None
            mid = await self.mud.getRoomIDbyHash(id_gmcp)
            if mid and mid[0] and mid[0] > 0:
                if id_mudlet is None:
                    id_mudlet = mid[0]
                elif id_mudlet != mid[0]:
                    await self.print(_("Collision: mudlet#{idm} but hash#{idh} with {hash!r}"), idm=id_mudlet, idh=mid[0], hash=id_gmcp)
                    return

        if offset_from is None and id_mudlet is None:
            self.__logger.warning("I don't know where to place the room!")
            await self.print(_("I don't know where to place the room!"))

        room = self.db.Room(name=descr, id_mudlet=id_mudlet)
        if id_gmcp:
            room.id_gmcp = id_gmcp
        self.db.add(room)
        self.db.commit()

        is_new = await self.maybe_assign_mudlet(room, id_mudlet)
        await self.maybe_place_room(room, offset_from,offset_dir, is_new=is_new, explode=explode)

        if (area is None or area.flag & self.db.Area.F_IGNORE) and offset_from is not None:
            area = offset_from.area
        if area is not None and not (area.flag & self.db.Area.F_IGNORE):
            await room.set_area(area)

        self.db.commit()
        self.__logger.debug("ROOM NEW:%s/%s",room.id_old, room.id_mudlet)
        return room

    async def maybe_assign_mudlet(self, room, id_mudlet=None):
        """
        Assign a mudlet room# if we don't already have one.
        """
        if room.id_mudlet is None:
            room.set_id_mudlet(id_mudlet)
        elif id_mudlet and room.id_mudlet != id_mudlet:
            await self.print(_("Mudlet IDs inconsistent! old {room.idn_str}, new {idm}"), room=room, idm=id_mudlet)
        if not room.id_mudlet:
            room.set_id_mudlet((await self.rpc(action="newroom"))[0])
            return True

    async def maybe_place_room(self, room, offset_from,offset_dir, is_new=False, explode=False):
        """
        Place the room if it isn't already.
        """
        x,y,z = None,None,None
        if not room.id_mudlet:
            return
        try:
            x,y,z = await self.mud.getRoomCoordinates(room.id_mudlet)
        except ValueError:
            # Huh. Room deleted there. Get a new room then.
            room.set_id_mudlet((await self.rpc(action="newroom"))[0])

        if offset_from and (x is None or (x,y,z) == (0,0,0)):
            await self.place_room(offset_from,offset_dir,room)

        else:
            # mudlet positions are kindof authoritative
            if explode:
                x *= selfconf['pos_x_delta']
                y *= selfconf['pos_y_delta']
                await self.mud.setRoomCoordinates(room.id_mudlet, x,y,z)

            room.pos_x, room.pos_y, room.pos_z = x,y,z
            room.label_x,room.label_y = await self.mud.getRoomNameOffset(room.id_mudlet)
            room.area_id = (await self.mud.getRoomArea(room.id_mudlet))[0]

    def dir_off(self, d, use_z=True, d_x=5, d_y=5, d_small=2):
        """Calculate an (un)likely offset for placing a new room."""
        d = self.dr.short2loc(d)
        dx,dy,dz,ds,dt = self.dr.offset_delta(d)

        d_x=self.conf["pos_x_delta"]
        d_y=self.conf["pos_y_delta"]
        # d_z is always 1
        d_small=self.conf["pos_small_delta"]

        dx = dx * self.conf["pos_x_delta"] + ds * d_small + dt * d_small
        dy = dy * self.conf["pos_y_delta"] + ds * d_small - dt * d_small
        if dz and not self.conf['dir_use_z']:
            dx -= d_small*dz
            dy += d_small*dz*2
            dz = 0
        return dx,dy,dz


    async def place_room(self, start,dir,room):
        """
        If you went from start via dir to room, move room to be in the
        correct direction.
        """
        # x,y,z = start.pos_x,start.pos_y,start.pos_z
        x,y,z = await self.mud.getRoomCoordinates(start.id_mudlet)
        dx,dy,dz = self.dir_off(dir)
        self.__logger.debug("Offset for %s is %r from %s",dir,(dx,dy,dz),start.idn_str)
        x,y,z = x+dx, y+dy, z+dz
        room.pos_x, room.pos_y, room.pos_z = x,y,z
        await self.mud.setRoomCoordinates(room.id_mudlet, x,y,z)

    async def move_to(self, moved, src=None, dst=None):
        """
        Move from src / the current room to dst / ? in this direction

        Returns: a new room if "dst" is True, dst if that is set, the room
        in that direction otherwise, None if there's no room there (yet).
        """

    async def alias_dbg(self, cmd):
        """
        Enter Python debugger
        """
        breakpoint()
        pass

    async def update_exit(self, room, d):
        try:
            x = self.room.exit_to(room)
        except KeyError:
            x = None
        else:
            if self.dr.is_std_dir(d):
                x = None

        if x is None:
            if self.room.flag & self.db.Room.F_NO_AUTO_EXIT:
                return # don't touch
            x,_src = await self.room.set_exit(d, room, force=False)
        else:
            _src = False
        if x.dst != room:
            await self.print(_("""\
Exit {exit} points to {dst.idn_str}.
You're in {room.idn_str}.""").format(exit=x.dir,dst=x.dst,room=room))
        #src |= _src

        if self.conf['add_reverse']:
            rev = self.dr.loc2rev(d)
            if rev:
                try:
                    xr = room.exit_at(rev, NoData)
                except KeyError:
                    await self.print(_("This room doesn't have a back exit {dir}."), room=room,dir=rev)
                else:
                    if xr.dst is not None and xr.dst != self.room:
                        await self.print(_("Back exit {dir} already goes to {room.idn_str}!"), room=xr.dst, dir=xr.dir)
                    elif not (room.flag & self.db.Room.F_NO_AUTO_EXIT):
                        await room.set_exit(rev, self.room)
            else:
                try:
                    self.room.exit_to(room)
                except KeyError:
                    await self.print(_("I don't know the reverse of {dir}, way back not set"), dir=d)
                # else:
                #     some exit to where we came from exists, so we don't complain

    def clean_thing(self, name):
        """
        Clean up the MUD's thing names.
        """
        name = name.strip()
        name = name.rstrip(".")
        return name

    async def timed_move(self, info=None, exits_text=None):
        await self.went_to_dir(TIME_DIR, info=info, exits_text=exits_text)

    async def went_to_dir(self, d, info=None, exits_text=None, skip_if_noop=False):
        if not self.room:
            await self.print("No current room")
            return

        db = self.db
        room_gmcp = None
        id_gmcp = info.get("id",None) if info else None
        if id_gmcp:
            try:
                room_gmcp = db.r_hash(id_gmcp)
            except NoData:
                pass
            else:
                if skip_if_noop and room_gmcp == self.room:
                    return
        x,_x = await self.room.set_exit(d)
        is_new = False
        room = x.dst
        if id_gmcp:
            try:
                if room_gmcp is None:
                    raise NoData
                if room_gmcp.flag & db.Room.F_NO_GMCP_ID:
                    id_gmcp = ""
                    raise NoData("NO-GMCP")
            except NoData:
                pass
            else:
                if not room:
                    room = room_gmcp
                elif not room_gmcp:
                    pass
                elif room != room_gmcp:
                    await self.print(_("MAP: Conflict! {room.id_str} vs. {room2.id_str}"), room2=room_gmcp, room=room)
                    room = room_gmcp

        if room is None or room.id_mudlet is None:
            id_mudlet = await self.mud.getRoomIDbyHash(id_gmcp) if id_gmcp else None

            if id_mudlet and id_mudlet[0] > 0 and not self.conf['mudlet_gmcp']:
                # Ignore and delete this.
                id_mudlet = id_mudlet[0]
                await self.mud.setRoomIDbyHash(id_mudlet,"")
                id_mudlet = None

            if id_mudlet and id_mudlet[0] > 0:
                id_mudlet = id_mudlet[0]
            else:
                id_mudlet = (await self.room.mud_exits).get(d, None) if self.room else None

            try:
                rn = (getattr(info,"short",None) if info else None) or self.last_cmd or getattr(self.process,"name",None) or self.command.lines[0][-1]
            except (AttributeError,IndexError):
                rn = self.dr.NAMELESS
            else:
                rn = self.dr.clean_shortname(rn)
            if room is None:
                room = await self.new_room(rn, offset_from=self.room, offset_dir=d, id_mudlet=id_mudlet, id_gmcp=id_gmcp)
                is_new = True
            else:
                room.id_mudlet = id_mudlet
                room.area = None
            if not (self.room.flag & self.db.Room.F_NO_AUTO_EXIT):
                await self.room.set_exit(d, room)
            db.commit()

        await self.went_to_room(room, d=d, is_new=is_new, info=info, exits_text=exits_text)

    async def went_to_room(self, room, d=None, exit=None, repair=False, is_new=False, info=None, exits_text=None):
        """You went to `room` using direction `d`."""
        assert not d or not exit
        if exit:
            d = exit.dir

        db = self.db

        is_new = (await self.maybe_assign_mudlet(room)) or is_new
        await self.maybe_place_room(room, self.room,d, is_new=is_new)

        # name and area set/update.
        await self.maybe_set_name(room)
        dom = info.get("domain","") if info else ""
        if dom or self.room:
            await self.maybe_set_area(dom, room, is_new_mudlet=is_new)

        id_gmcp = info.get("id",None) if info else None
        if id_gmcp and not room.id_gmcp:
            room.id_gmcp = id_gmcp

        if exits_text:
            await self.process_exits_text(room, exits_text)
        if info and 'exits' in info:
            for x in info["exits"]:
                await room.set_exit(x, True)

        if self.room and d:
            await self.update_exit(room, d=d)

        last_room = None
        if not self.room or room.id_old != self.room.id_old:
            if not repair:
                self.last_room = self.room
            if d:
                self.last_dir = d
            last_room,self.room = self.room,room
            if self.view_room == last_room or self.view_room == room:
                await self.view_to(None)
            self.is_new_room = is_new
            self.next_word = None
            self.trigger_sender.set()

        await self.print(room.info_str)
        if room.id_mudlet is not None:
            room.pos_x,room.pos_y,room.pos_z = await self.mud.getRoomCoordinates(room.id_mudlet)
            db.commit()
        await self.dr.show_room_data()

        if not self.last_room:
            return

        p = self.top.last_move
        rn = ""
        if info:
            rn = info.get("short","")
        if not rn and p and p.current > Command.P_BEFORE and p.lines[0]:
            rn = p.lines[0][-1]
        if rn:
            rn = self.dr.clean_shortname(rn)
        if rn:
            room.last_shortname = rn
            if not room.name:
                room.name = rn
                db.commit()
            elif room.name == rn:
                pass
            elif self.dr.clean_shortname(room.name) == rn:
                room.name = rn
                db.commit()
            else:
                await self.print(_("Name: old: {room.name}"), room=room,name=rn)
                await self.print(_("Name: new: {name}"), room=room,name=rn)
                await self.print(_("Use '#rs' to update it"))

        if p and room.long_descr:
            for t in p.lines[p.P_AFTER]:
                for tt in SPC.split(t):
                    room.has_thing(tt)
        else:
            await self.send_commands("", LookCommand(self, self.dr.LOOK), now=True)
        room.visited()
        if last_room and last_room.id_mudlet:
            await self.update_room_color(last_room)
        if room.id_mudlet:
            await self.update_room_color(room)

        db.commit()

    async def maybe_set_name(self, room):
        if not room.name:
            room.name = room.last_shortname
        if not room.id_mudlet:
            return
        on = await self.mud.getRoomName(room.id_mudlet)
        if not on or not on[0]:
            await self.mud.setRoomName(room.id_mudlet, room.name)

    async def maybe_set_area(self, dom, room, is_new_mudlet=False):
        """Update a room's area.
        The area to prefer is either the current room's or the Mud's,
        depending on #cfm.
        The Mud may not send an area, or we don't have a current room yet.
        Also one of these may have the Ignore flag set, which is equivalent
        to it not being set, except if the flag is set on both.
        Likewise #cff is used to override the room's old area, except that
        it's forced if the old area's ignore flag is set, and cleared when
        the new area's is, or left alone when both are.
        """
        area_mud = await self.get_named_area(dom, create=None) if dom else None
        area_last = self.room.area if self.room else None
        area_default = await self.get_named_area("Default", create=None)
        prefer_mud = self.conf['use_mud_area']
        force = self.conf['force_area']

        # If the Ignore flag is set one only one of these, adhere to it
        if area_mud and area_mud.flag & self.db.Area.F_IGNORE:
            if area_last and area_last.flag & self.db.Area.F_IGNORE:
                pass
            else:
                if area_last is not None and not (area_default.flag & self.db.Area.F_IGNORE):
                    area_mud = None
        elif area_last and area_last.flag & self.db.Area.F_IGNORE:
            if area_mud is not None and not (area_default.flag & self.db.Area.F_IGNORE):
                area_last = None


        # The area to use
        area = ((area_mud or area_last) if prefer_mud else (area_last or area_mud)) or area_default

        # The Force flag is overridden if the old/new area's
        # ignore flag is set but not the new/old area's
        if room.area and room.area.flag & self.db.Area.F_IGNORE:
            if not (area.flag & self.db.Area.F_IGNORE):
                force = True
        elif area.flag & self.db.Area.F_IGNORE:
            force = False

        # Finally, the area is set if the room doesn't have one or if it's forced.
        if room.area and not force:
            if room.orig_area is None:
                room.orig_area = area
            area = room.area
        if is_new_mudlet or room.area != area:
            await room.set_area(area, force=True)

    @with_alias("rl!")
    @doc(_(
        """
        Update long descr

        get from MUD and set room to it
        """))
    async def alias_rl_b(self, cmd):
        self.main.start_soon(self.send_commands,"", LookCommand(self, self.dr.LOOK, force=True))

    @doc(_(
        """
        Update room shortname
        Either the last shortname, or whatever you say here.
        """))
    async def alias_rs(self, cmd):
        db = self.db
        cmd = self.cmdfix("*", cmd)
        if self.view_room and not cmd:
            await self.print(_("Use an explicit text while viewing!"))
            return
        room = self.view_or_room
        if not room:
            await self.print(_("No current room"))
            return
        sn = cmd[0] if cmd else room.last_shortname
        if not sn:
            await self.print(_("No shortname known"))
            return

        room.name = sn
        await self.mud.setRoomName(room.id_mudlet, sn)
        if cmd:
            room.flag |= db.Room.F_MOD_SHORTNAME
        else:
            room.flag &=~ db.Room.F_MOD_SHORTNAME
        db.commit()


    # ### Viewpoint movement ### #

    @doc(_(
        """Shift the view to the room in this direction"""))
    async def alias_vg(self, cmd):
        cmd = self.cmdfix("x", cmd, min_words=1)
        try:
            x = self.view_or_room.exit_at(cmd[0])
        except KeyError:
            await self.print(_("No exit to {d}"), d=cmd[0])
        else:
            vr = x.dst
            if not vr:
                await self.print(_("No room to {d}"), d=cmd[0])
            else:
                await self.view_to(vr)

    @doc(_(
        """Focus on a room.
        Room selection:
        .    the room you're in
        :    the room you came from
        !    the currently-viewed room
        ?    the room that's selected on the map
        NUM  some room on the Mudlet map
        -NUM some room in the database
        """))
    async def alias_v(self, cmd):
        cmd = self.cmdfix("r", cmd)
        if cmd:
            await self.view_to(cmd[0])
        else:
            # The background task might be too slow
            msg = await self.mud.getMapSelection()
            if msg and msg[0] and msg[0].get("center",None):
                room = self.db.r_mudlet(msg[0]["center"])
                if room:
                    await self.view_to(room)
                else:
                    await self.print(_("The selected room is not in the database."))
            elif self.room:
                await self.view_to(self.room)
            else:
                await self.print(_("No current room known!"))


    async def view_to(self, room=None):
        if not room:
            room = self.room
        self.view_room = None if room == self.room else room

        if room.id_mudlet:
            await self.mud.centerview(room.id_mudlet)
            if self.view_room:
                await self.print(_("Set view to {room.idnn_str}"), room=room)
            else:
                await self.print(_("Reset view to current room: {room.idnn_str}"), room=room)
        else:
            await self.print(_("Not yet mapped: {room.idn_str}"), room=room)

    @doc(_(
        """Shift the view to the player's"""))
    async def alias_vr(self, cmd):
        await self.view_to(None)


    @run_in_task
    async def called_label_shift(self, dx, dy):
        db = self.db

        dx *= self.conf['label_shift_x']
        dy *= self.conf['label_shift_y']
        msg = await self.mud.getMapSelection()
        if not msg or not msg[0]:
            rooms = await self.mud.getPlayerRoom()
        else:
            rooms = msg[0]['rooms']
        for r in rooms:
            x,y = await self.mud.getRoomNameOffset(r)
            try:
                room = db.r_mudlet(r)
            except NoData:
                pass
            else:
                x += dx; y += dy
                await self.mud.setRoomNameOffset(r, x, y)
                room.label_x,room.label_y = x,y
        await self.mud.updateMap()
        db.commit()

    # ### scan for words in the descriptions ### #

    async def add_words(self, txt, room=None):
        """
        Given a room and a text, create roomword entries of all (unscanned,
        if only_new is set) words found in the text and add them to the
        room.

        WARNING this only works with German words because nouns are
        capitalized. TODO: Use some sort of dictionary lookup to find nouns
        with other languages.
        """
        db = self.db
        if room is None:
            room = self.room
        txt = txt.replace("\n"," ")
        txt = DASH.sub("",txt)
        start = True
        nstart = False
        for ww in txt.split():
            for w in WORD.findall(ww):
                if txt.endswith(ww):  # ends with punctuation, so maybe ignore next word
                    nstart = False
                else:
                    nstart = ww[-1] in ".?!"

                if not w[0].isupper():  # not a noun
                    continue
                w = w.lower()
                w = db.word(w, create = not start)
                if not w:
                    continue
                if w.flag == db.WF_SKIP:
                    continue
                room.with_word(w, create=True)
                start = False
            start = nstart
        db.commit()

    @doc(_("""
    Search for interesting stuff
    Given a list of words from the description, get the next one and 'examine' it.
    """))
    async def alias_s(self, cmd):
        cmd = self.cmdfix("*", cmd)
        room = self.room
        if not room:
            await self.print(_("No active room."))
            return
        if cmd:
            w = self.db.word(cmd[0], create=True)
            self.next_word = self.room.with_word(w, create=True)
            await self.send_commands("", WordProcessor(self, self.room, w, "%s %s" % (self.dr.EXAMINE,cmd[0])))
        elif self.next_word:
            w = self.next_word.word
            await self.send_commands("", WordProcessor(self, self.room, w, ("%s %s" % (self.dr.EXAMINE,w.name)) if w.name else self.dr.LOOK))
        else:
            await self.next_search()

    @doc(_("""
    List of scanned words, so far
    Display which words from the description have been scanned.
    """))
    # 0 notscanned, 1 scanned, 2 skipped, 3 marked important?
    async def alias_sl(self, cmd):
        cmd = self.cmdfix("r", cmd)
        room = cmd[0] if cmd and cmd[0] else self.view_or_room
        if not room:
            await self.print(_("No active room."))
            return
        ri = ([],[],[],[])
        for rw in room.words:
            ri[rw.flag].append(rw.word.name.lower() if rw.word.name else _("‹Room›"))

        seen = False
        for n,w in zip(ri,(
            _("not scanned:"), _("scanned:"), _("skipped:"), _("important:")
            )):
            if not n:
                continue
            n.sort()
            await self._wrap_list(w,*n)
            seen = True
            
        if not seen:
            await self.print(_("Nothing yet."))

    @doc(_("""
    Clear search
    Reset discovery so that all known words are scanned again.
    Starts with a global "look".
    """))
    async def alias_sc(self, cmd):
        cmd = self.cmdfix("", cmd)
        room = self.view_or_room
        if not room:
            await self.print(_("No active room."))
            return
        room.reset_words()


    async def next_search(self, mark=False, again=True):
        if not self.room:
            await self.print(_("No active room."))
            return

        if mark:
            if not self.next_word:
                await self.print(_("No current word."))
                return
            if not self.next_word.flag:
                self.next_word.flag = 1
                self.db.commit()

        self.next_word = w = self.room.next_word()
        if w is not None:
            if w.word.name:
                await self.print(_("Next search: {word}"), word=w.word.name)
            else:
                await self.print(_("Next search: ‹look›"))
            return

        w = self.room.with_word("", create=True)
        if w.flag:
            await self.print(_("No (more) words."))
        else:
            self.next_word = w
            self.main.start_soon(self.send_commands, "", WordProcessor(self, self.room, w.word, self.dr.LOOK))
        self.db.commit()


    @doc(_("""
    Set an alias
    One word: The next word should actually be named X.
    Two words: A should be named B.
    """))
    async def alias_sa(self, cmd):
        db = self.db
        cmd = self.cmdfix("ww", cmd, min_words=1)
        if len(cmd) == 1:
            if not self.next_word:
                await self.print(_("No current word."))
                return
            alias,real = self.next_word.word,cmd[0]
        else:
            alias,real = db.word(cmd[0]), cmd[1]
        if not alias:
            await self.print(_("Not found? DEBUG"))
            return
        nw = self.room.with_word(alias.alias_for(real))
        if self.next_word and self.next_word.word == alias:
            self.next_word = nw
        else:
            nw = self.next_word
        db.commit()

        if not nw or nw.flag:
            await self.next_search()
        nw = self.next_word
        if nw and not nw.flag:
            await self.print(_("Next search: {word}"), word=nw.word.name)
        else:
            await self.print(_("No (more) words."))
            

    @doc(_("""
    Ignore a word
    The next word (or the one you enter) shall be ignored here.
    """))
    async def alias_si(self, cmd):
        db = self.db
        cmd = self.cmdfix("w", cmd)
        if cmd:
            w = self.db.word(cmd[0])
        else:
            w = self.next_word.word
        wr = self.room.with_word(w, create=True)
        if wr.flag == db.WF_SKIP:
            await self.print(_("Already ignored."))
            return
        wr.flag = db.WF_SKIP
        db.commit()
        await self.next_search()

    @doc(_("""
    Ignore a word globally
    The next word (or the one you enter) shall be ignored everywhere.
    """))
    async def alias_sii(self, cmd):
        db = self.db
        cmd = self.cmdfix("w", cmd)
        if cmd:
            w = self.db.word(cmd[0])
        else:
            w = self.next_word.word
        wr = self.room.with_word(w, create=False)
        if w.flag == db.WF_SKIP:
            await self.print(_("Already ignored."))
            return
        w.flag = db.WF_SKIP
        if wr is not None:
            # db.delete(wr)
            wr.flag = db.WF_SKIP
        db.commit()
        if not cmd:
            await self.next_search()

    # ### Features ### #

    @doc(_("""
    Feature list
    List all known features / a feature's commands.
    """))
    async def alias_xfl(self, cmd):
        db = self.db
        cmd = self.cmdfix("w", cmd)
        if cmd:
            f = db.feature(cmd[0])
            if f is None:
                await self.print(_("Feature unknown."))
            else:
                await self.print(_("* Enter:"))
                await self.print(f.enter)
                await self.print(_("* Leave:"))
                await self.print(f.exit)

        else:
            n = 0
            for f in db.q(db.Feature).all():
                await self.print(_("{f.id} {f.name}"), f=f)
                n += 1
            if n:
                await self.print(_("{n} features."), n=n)
            else:
                await self.print(_("No features yet known."))

    @with_alias("xfl+")
    @doc(_("""
    Add feature
    Creates a new feature.
    """))
    async def alias_xfl_p(self, cmd):
        db = self.db
        cmd = self.cmdfix("w", cmd, min_words=1)
        f = db.Feature(name=cmd[0])
        db.add(f)
        await self.print(_("{f.name}: created"), f=f)

    @with_alias("xfl=")
    @doc(_("""
    Remove feature
    Unconditionally deletes a feature.
    """))
    async def alias_xfl_eq(self, cmd):
        db = self.db
        cmd = self.cmdfix("w", cmd, min_words=1)
        f = db.feature(cmd[0])
        db.delete(f)
        await self.print(_("{f.name}: deleted"), f=f)

    @doc(_("""
    Commands for entering
    Set/replace the commands sent when passing a feature'd exit
    """))
    async def alias_xfla(self, cmd):
        db = self.db
        cmd = self.cmdfix("w", cmd, min_words=1)
        f = db.feature(cmd[0])

        txt = ""
        if f.enter:
            await self.print(_("Old content:\n{txt}"), txt=f.enter)
        await self.print(_("Set feature-enter text. End with '.'."))
        async with self.input_grab_multi() as g:
            async for line in g:
                if txt:
                    txt += "\n"
                txt += line

        f.enter = txt
        db.commit()
        await self.print(_("{f.name}: enter commands set"), f=f)

    @doc(_("""
    Commands for exiting
    Set/replace the commands sent when passing a feature'd exit
    """))
    async def alias_xflb(self, cmd):
        db = self.db
        cmd = self.cmdfix("w*", cmd, min_words=1)
        f = db.feature(cmd[0])

        txt = ""
        if f.exit:
            await self.print(_("Old content:\n{txt}"), txt=f.exit)
        await self.print(_("Set feature-enter text. End with '.'."))
        async with self.input_grab_multi() as g:
            async for line in g:
                if txt:
                    txt += "\n"
                txt += line

        f.exit = txt
        db.commit()
        await self.print(_("{f.name}: exit commands set"), f=f)

    @with_alias("xf+")
    @doc(_("""
    Set feature
    Set an exit to have this feature.
    Parameters: featurename [exit]
    If used without an exit, it's applied to the exit you just came in from.
    """))
    async def alias_xf_p(self, cmd):
        db = self.db
        cmd = self.cmdfix("wx", cmd, min_words=1)
        f = db.feature(cmd[0])
        if len(cmd) > 1:
            x = self.room.exit_at(cmd[1])
        else:
            x = self.last_room.exit_at(self.last_dir)
        x.feature = f
        db.commit()
        await self.print(_("Feature {f.name} set for {x.info_str}."), f=f, x=x)

    
    @with_alias("xf-")
    @doc(_("""
    Set back-feature
    Set the exit which you would use to go back to the room you just came
    from to have this feature.
    Does not actually un-apply the feature.
    """))
    async def alias_xf_m(self, cmd):
        db = self.db
        cmd = self.cmdfix("w", cmd, min_words=1)
        f = db.feature(cmd[0])
        d = self.dr.loc2rev(self.last_dir)
        if d == self.last_dir:  # could not reverse
            x = self.room.exit_to(self.last_room)
        else:
            x = self.room.exit_at(d, prefer_feature=True)
        x.feature = f
        db.commit()
        await self.print(_("Feature {f.name} set for {x.info_str}."), f=f, x=x)

    @with_alias("xf=")
    @doc(_("""
    Remove feature
    Drop a feature from the current or named room's exit
    Parameters: exit [room]
    """))
    async def alias_xf_q(self, cmd):
        db = self.db
        cmd = self.cmdfix("xr", cmd, min_words=1)
        room = cmd[1] if len(cmd) > 1 else self.view_or_room
        x = room.exit_at(cmd[0])
        if x.feature is None:
            await self.print(_("No feature set on {x.info_str}."), f=f, x=x)
            return
        f,x.feature = x.feature,None
        db.commit()
        await self.print(_("Feature {f.name} removed from {x.info_str}."), f=f, x=x)

    
    # ### Quests ### #

    async def _q_state(self, q, print_q=True):
        if print_q:
            await self.print(_("Quest: {q.id}: {q.name}"), q=q)
        qs = q.current_step
        if qs is None:
            await self.print(_("Not started."))
            return

        if qs.room == self.room:
            await self.print(_("Current: {qs.step}: {qs.command}."), q=q,qs=qs)
        else:
            await self.print(_("Current: {qs.step}: {qs.command} in {qs.room.idnn_str}."), q=q,qs=qs)


    @doc(_("""
    Quest state
    Display the state of a / the current quest.
    """))
    async def alias_qs(self, cmd):
        cmd = self.cmdfix("w", cmd)
        q = self.db.quest(cmd[0]) if cmd else self.quest
        if q is None:
            await self.print(_("Quest unknown."))
            return

        await self._q_state(q)


    @doc(_("""
    Quest list
    List all known quests.
    """))
    async def alias_qql(self, cmd):
        db = self.db
        n = 0
        for q in db.q(db.Quest).all():
            await self.print(_("{q.id} {q.name}"), q=q)
            n += 1
        if n:
            await self.print(_("{n} quests."), n=n)
        else:
            await self.print(_("No quests yet known."))

    @doc(_("""
    Step list
    List the current quests's steps
    Parameter: a room: list just that room's steps.
    """))
    async def alias_ql(self, cmd):
        if not self.quest:
            await self.print(_("No current quest."))
            return
        cmd = self.cmdfix("r", cmd)
        if cmd and not cmd[0]:
            cmd[0] = self.room
        await self.print(_("{q.id} {q.name}:"), q=self.quest)
        room = None
        n = 0
        for qs in self.quest.steps:
            if cmd and cmd[0] != qs.room:
                continue
            if room != qs.room:
                await self.print(_("{qs.step}: Go to {qs.room.idnn_str}"), qs=qs)
                room = qs.room
            await self.print(_("{qs.step}: {qs.command}"), qs=qs)
            n += 1
        if not n:
            await self.print(_("No steps yet."))


    @doc(_("""
    Activate quest
    Set this quest to be the active quest.
    """))
    async def alias_qqa(self, cmd):
        cmd = self.cmdfix("w", cmd, min_words=1)
        try:
            qn = int(cmd[0])
        except ValueError:
            qn = cmd[0]
        try:
            self.quest = q = self.db.quest(qn)
        except KeyError:
            await self.print(_("No quest with that name known."))
        else:
            if q.step is None:
                await self.print(_("Active, no current step."), q=self.quest)
            else:
                await self.print(_("Active, step {step.step}."), q=self.quest,step=q.current_step)


    @with_alias("qq+")
    @doc(_("""
    New Quest
    Create a (named) quest and activate it.
    """))
    async def alias_qq_p(self, cmd):
        db = self.db
        cmd = self.cmdfix("w", cmd, min_words=1)
        q = db.Quest(name=cmd[0])
        db.add(q)
        db.commit()
        self.quest = q


    @with_alias("q+")
    @doc(_("""
    New step
    Add this command to the current quest
    """))
    async def alias_q_p(self, cmd):
        cmd = self.cmdfix("*", cmd, min_words=1)
        if not self.quest:
            await self.print(_("No current quest."))
            return
        self.quest.add_step(command=cmd[0],room=self.view_or_room)
        self.db.commit()


    @with_alias("q-")
    @doc(_("""
    Drop step
    Remove the numbered command to the current quest
    """))
    async def alias_q_m(self, cmd):
        cmd = self.cmdfix("i", cmd, min_words=1)
        if not self.quest:
            await self.print(_("No current quest."))
            return
        qs = self.quest.step_at(cmd[0])
        qs.delete()
        self.db.commit()

    @doc(_("""
    Reorder step
    Tell step A that it should now be numbered B,
    and move all affected steps around
    One number: move the last step there
    Three: move A through B to C.
    """))
    async def alias_qo(self, cmd):
        cmd = self.cmdfix("iii", cmd, min_words=1)
        if not self.quest:
            await self.print(_("No current quest."))
            return
        ns = cmd[-1]
        if len(cmd) == 3:
            sa,sb,sc = cmd
            if sa>=sb or sa<=sc<=sb:
                await self.print(_("A and B must be a range and C can't be in that range."))
                return
            while sa <= sb:
                qs = self.quest.step_at(sa)
                qs.set_step_nr(sc)
                if sa<=sc:
                    # moving up. sa+1…sb moves down one step.
                    sb -= 1
                else:
                    # moving down. sa+1…sb stays where it is.
                    sa += 1
                # the destination is not affected either way.
                sc += 1
            else:
                while sa <= sb:
                    qs = self.quest.step_at(sa)
        else:
            qs = self.quest.step_at(cmd[0] if len(cmd) > 1 else self.quest.last_step_nr)
            qs.set_step_nr(ns)
        self.db.commit()


    @with_alias("qq=")
    @doc(_("""
    Delete Quest
    Remove a (named) quest.
    WARNING! this is not undoable.
    """))
    async def alias_qq_del(self, cmd):
        db = self.db
        cmd = self.cmdfix("w", cmd, min_words=1)
        q = db.quest(name=cmd[0])
        db.delete(q)
        db.commit()
        if self.quest == q:
            self.quest = None

    @doc(_("""
    Next Step
    Do the next thing in this quest
    Argument: set to continue there.
    """))
    async def alias_qn(self, cmd):
        cmd = self.cmdfix("i", cmd)
        if not self.quest:
            await self.print(_("No current quest."))
            return
        if cmd:
            self.quest.step = cmd[0]
            await self._q_state(self.quest, print_q=False)
            self.db.commit()
            return
        await self._q_next_step()

    async def _q_next_step(self):
        s = self.quest.step
        qs = self.quest.current_step
        if qs is None:
            qs = self.quest.step_at(1)
            if qs is None:
                await self.print(_("Quest: empty!"))
                return
            await self.print(_("Quest: starting."))
            self.quest.step = 1
            self.db.commit()
            return

        if qs.room != self.room:
            w = self.current_walker
            if w and w.dest == qs.room:
                await self.print(_("Quest: Already walking to room {room.idn_str}"), room=qs.room)
                await self.print(_("Walker: {w}"), w=w)

            elif self.path_gen and isinstance(self.path_gen.checker,RoomFinder) and self.path_gen.checker.room == qs.room:
                await self.print(_("Quest: Patience! Still finding room {room.idn_str}"), room=qs.room)
            else:
                await self.print(_("Quest: Going to room {room.idn_str}"), room=qs.room)
                await self.run_to_room(qs.room)
            return

        c = qs.command
        if c[0] != '#':
            await self._do_manual_command(c, send_echo=True)
        elif c == "#/ROOM":
            pass
        elif c == "#/DEAD":
            c = c[6:].strip()
            await self.print(_("wait until {what} is dead"), what=c)
        elif c[1] == ':':
            await self.print("*** "+c[2:])
        else:
            await self.print("***??? "+c[1:])
        s = qs.step + 1
        if self.quest.step_at(s):
            self.quest.step = s
        else:
            self.quest.step = None
            await self.print(_("*** END ***"))
        self.db.commit()
    

    # ### main code ### #

    async def run(self):

        if self.cfg.get("logfile") is not None:
            self.logfile = open(self.cfg['logfile'], "a")

        with SQL(self.cfg) as db:
            self.db = db

            migrate = self.cfg['sql'].get('migrate', False)
            if migrate:
                self.__logger.debug("checking database")
                run_alembic(db, migrate)

            self.__logger.debug("waiting for connection from Mudlet")
            async with self:
                db.setup(self)

                if self.logfile is not None:
                    print(f"""
*** Start *** {datetime.now().strftime("%Y-%m-%d %H:%M")} ***
""", file=self.logfile)
                    self.logfile.flush()

                try:
                    async with self.event_monitor("*") as h:
                        await self.setup()
                        async for msg in h:
                            await self.handle_event(msg.get('args',()),msg.get("event",None))
                except Exception as exc:
                    self.__logger.exception("END")
                    raise
                except BaseException as exc:
                    self.__logger.exception("END")
                    raise
                else:
                    self.__logger.error("END")
                    pass


def run_alembic(db, migrate):
    """
    This magic incantation updates your database unconditionally.

    If migrate>1 then delete non-table things
    if migrate>2 then delete tables
    """
    from alembic.runtime.migration import MigrationContext
    from alembic.autogenerate import api
    from alembic.operations.base import Operations

    ctx = MigrationContext(dialect=db.db.bind.dialect, connection=db.db.connection(), opts={})
    res = api.produce_migrations(ctx,db.Room.__table__.metadata)
    ops = Operations(ctx, ctx.impl)
    def all_ops(op,drops):
        if hasattr(op,"ops"):
            for o in op.ops:
                yield from all_ops(o,drops)
        else:
            n = type(op).__name__
            if n.startswith("DropIndex") != drops:
                return

            if n.startswith("DropTable"):
                ml = 3
            elif n.startswith("Drop"):
                ml  = 2
            else:
                ml = 1
            logger.debug("DB update %s", op)
            if ml >= migrate:
                yield op
    for op in all_ops(res.upgrade_ops,False):
        ops.invoke(op)
    for op in all_ops(res.upgrade_ops,True):
        ops.invoke(op)

@click.command()
@click.option("-c","--config", type=click.File("r"), help="Config file")
@click.option("-d","--debug", is_flag=True, help="Debug output")
@click.option("-m","--migrate", count=True, help="do SQL migration? use -mm for deleting anything, -mmm for dropping tables")
async def main(config,debug,migrate):
    m={}
    if config is None:
        config = open("mapper.cfg","r")
    cfg = yaml.safe_load(config)
    cfg = combine_dict(cfg, DEFAULT_CFG, cls=attrdict)

    if 'logging' in cfg:
        from logging.config import dictConfig
        if debug:
            cfg['logging'].setdefault('root',{})['level'] = 'DEBUG'
        dictConfig(cfg['logging'])
    else:
        logging.basicConfig(level=logging.DEBUG if debug else getattr(logging,cfg.log['level'].upper()))

    s = WebServer(cfg, factory=S)
    await s.run()

if __name__ == "__main__":
    main()
