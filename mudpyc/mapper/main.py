#!/usr/bin/python3

from mudpyc import Server, Alias, with_alias, run_in_task
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

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from .sql import SQL, NoData
from .const import SignalThis, SkipRoute, SkipSignal, Continue
from .const import ENV_OK,ENV_STD,ENV_SPECIAL,ENV_UNMAPPED
from .walking import PathGenerator, PathChecker, RoomFinder, LabelChecker, FulltextChecker, VisitChecker, ThingChecker, SkipFound
    
from ..util import doc

import logging
logger = logging.getLogger(__name__)

def AD(x):
    return combine_dict(x, cls=attrdict, force=True)

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
            add_reverse = True,
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

EX1 = re.compile(r"^Es gibt \S+ sichtbare Ausgaenge:(.*)$")
EX2 = re.compile(r"^Es gibt einen sichtbaren Ausgang:(.*)$")
EX3 = re.compile(r"^Es gibt keinen sichtbaren Ausgang.\s*")
EX4 = re.compile(r"^Es gibt keine sichtbaren Ausgaenge.\s*")
EX5 = re.compile(r"^Du kannst keine Ausgaenge erkennen.\s*")
EX9 = re.compile(r"\.\s*$")
SPC = re.compile(r"\s{3,}")
WORD = re.compile(r"(\w+)")
DASH = re.compile(r"- +")

TIME_DIR = ":time:"

CFG_HELP=attrdict(
        use_mud_area=_("Use the MUD's area name"),
        force_area=_("Modify existing rooms' area when visiting them"),
        mudlet_gmcp=_("Use GMCP IDs stored in Mudlet's map"),
        add_reverse=_("Link back when creating an exit"),
        dir_use_z=_("Allow new rooms in Z direction"),
        debug_new_room=_("Debug room allocation"),
        label_shift_x=_("X shift, moving room labels"),
        label_shift_y=_("Y shift, moving room labels"),
        pos_x_delta=_("X offset, new rooms"),
        pos_y_delta=_("Y offset, new rooms"),
        pos_small_delta=_("Diagonal offset, new rooms"),
        )

_itl2loc = {
        "up":"oben", "down":"unten", "in":"rein", "out":"raus",
        "north":"norden", "south":"sueden", "east":"osten", "west":"westen",
        "northwest":"nordwesten", "southwest":"suedwesten",
        "northeast":"nordosten", "southeast":"suedosten",
        }
_loc2itl = {}
_short2loc = {
        "n":"norden", "s":"sueden",
        "o":"osten","w":"westen",
        "so":"suedosten","sw":"suedwesten",
        "no":"nordosten","nw":"nordwesten",
        "ob":"oben","u":"unten",
        }
_std_dirs = set()
for a in "nord","sued","":
    for b in "ost","west","":
        for c in "ob","unt","":
            _std_dirs.add(a+b+c+"en")
_std_dirs.remove("en")  # :-)

for k,v in _itl2loc.items():
    _loc2itl[v]=k
for k,v in _short2loc.items():
    _loc2itl[k] = _loc2itl[v]
def short2loc(x):
    return _short2loc.get(x,x)
def loc2itl(x):
    x = short2loc(x)
    return _loc2itl.get(x,x)
def loc2rev(x):
    r=(("nord","sued"),("ost","west"),("oben","unten"))
    y=x
    for a,b in r:
        if a in y:
            y = y.replace(a,b)
        else:
            y = y.replace(b,a)
    if x==y or y not in _std_dirs:
        return None
    return y
def is_std_dir(x):
    return x in _std_dirs

def dir_off(d, use_z=True, d_x=5, d_y=5, d_small=2):
    """Calculate an (un)likely offset for placing a new room."""
    x,y,z = 0,0,0
    d = short2loc(d)
    if "ost" in d: x += d_x
    if "west" in d: x -= d_x
    if "nord" in d: y += d_y
    if "sued" in d: y -= d_y
    if d.endswith("unten"): z -= 1
    if d.endswith("oben"): z += 1
    if d == "raus":
        x -= d_small
        y -= d_small
    # anything not moving out is regarded as moving in
    if (x,y,z) == (0,0,0):
        x += d_small
        y += d_small
    if not use_z:
        x -= d_small*z
        y += d_small*z
        z = 0
    return x,y,z

def itl2loc(x):
    if isinstance(x,dict):
        return { itl2loc(k):v for k,v in x.items() }
    else:
        return _itl2loc.get(x,x)

class MappedSkipMod:
    """
    A mix-in that doesn't walk through unmapped rooms
    and which processes a skip list
    """
    def __init__(self, skiplist=(), **kw):
        self.skiplist = skiplist
        super().__init__(**kw)

    async def check(self, room):
        if not room.id_mudlet:
            return SkipRoute

        res = await super().check(room)
        if room in self.skiplist and not res.skip:
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
    upstack = None

    def __init__(self, server, *, stopped=False):
        self.server = server
        self.stopped = stopped

    def _repr(self):
        res = {}
        n = self.__class__.__name__
        if self.stopped:
            res["stop"]="y"
        if n == "Process":
            res["_t"] = "?"  # shouldn't happen
        elif n.endswith("Process"):
            res["_t"] = n[:-7]
        else:
            res["_t"] = n
        return res

    def __repr__(self):
        attrs = self._repr()
        return "P‹%s›" % (" ".join("%s=%s" % (k,v) for k,v in self._repr().items()),)

    def _go_on(self):
        s = self.server
        if s.process is self:
            s.trigger_sender.set()

    async def pause(self):
        if self.stopped:
            await self.server.print(_("The process is already stopped."))
        else:
            self.stopped = True
            await self.print(_("Paused."))

    async def resume(self):
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
            return
        await self.queue_next()

    async def queue_next(self):
        """
        Run the next job.

        The default does nothing, i.e. it finishes this command and gets
        back to the next one on the list.
        """
        self.server.process = np = self.upstack
        if np is not None:
            await np.queue_next()

class FixProcess(Process):
    """
    Fix me.
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

class SeqProcess(Process):
    """
    Process that simply runs a sequence of events
    """
    sleeping = False
    def __init__(self, server, cmds, real_dir=None, room=None, send_echo=True, **kw):
        self.cmds = cmds
        self.current = 0
        self.real_dir = real_dir
        self.room = room
        self.send_echo = send_echo
        super().__init__(server, **kw)

    def _repr(self):
        r = super()._repr()
        if self.real_dir:
            r["dir"] = self.real_dir
        if self.room:
            r["room"] = self.room.id_str
        if self.sleeping:
            r["delay"] = self.sleeping
        for i,c in enumerate(self.cmds):
            r["cmd%02d"%i] = ("*" if i == self.current else "") + repr(c)
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
        if self.room is not None:
            if s.room != self.room:
                self.state = _("Wait for room {nr.id_str}:{nr.name}").format(nr=self.current_room)
                await s.print(self.state)
                return
            self.room = None
        s = self.server
        if self.real_dir:
            s.named_exit,self.real_dir = self.real_dir,None
        while True:
            try:
                d = self.cmds[self.current]
                self.current += 1
            except IndexError:
                break

            if isinstance(d,str):
                logger.debug("sender sends %r",d)
                if not d:
                    continue
                if d[0] != '#':
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

        await super().queue_next()


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
            r = next(self.rooms)
        self.current_room = s.db.r_old(r)


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
            self.named_exit = x.dir
            await s.send_commands(*x.moves)
            return True


    async def stepback(self):
        """
        We ran into a room without light or whatever.
        
        The idea of this code is to go back to the previous room, light a
        torch or whatever, and then continue.
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
            await s.send_commands(*x.moves, room=s.room)


class Command:
    """
    Class to send one line to the MUD and collect whatever input you get in
    response.
    """
    P_BEFORE = 0
    P_AFTER = 1
    P_LATE = 2

    info = None
    # None: not yet

    send_seen = False
    # Echo of our own send command present?
    
    send_echo = True
    # echo this output to the user?

    room = None
    # room I was in, presumably, when issuing this

    def __init__(self, server, command):
        self.server = server
        self.command = command
        self.lines = ([],[],[])
        self.exits_text = None
        self._done = trio.Event()
        # before reporting exits or whatever
        # after reporting exits but before the prompt
        # after the prompt
        self.current = self.P_BEFORE

    def _repr(self):
        res = {}
        n = self.__class__.__name__
        res["send"] = self.command
        if n == "Command":
            res["_t"] = "Std"
        elif n.endswith("Command"):
            res["_t"] = n[:-7]
        else:
            res["_t"] = n
        if self._done.is_set:
            res["s"] = "done"
        else:
            res["s"] = ["before","after","late"][self.current]
        if self.exits_text:
            res["exits"] = "y"
        return res

    def __repr__(self):
        attrs = self._repr()
        return "C‹%s›" % (" ".join("%s=%s" % (k,v) for k,v in self._repr().items()),)

    def dir_seen(self, exits_line):
        if self.current == self.P_BEFORE:
            self.current = self.P_AFTER
            if exits_line:
                self.exits_text = exits_line
        elif self.current == self.P_AFTER and not self.lines[1]:
            if exits_line:
                self.exits_text += " " + exits_line

    def prompt_seen(self):
        self.current = self.P_LATE

    def add(self, msg):
        self.lines[self.current].append(msg)

    async def done(self):
        # a prompt: text is finished
        self._done.set()
        pass

    async def wait(self):
        await self._done.wait()

class LookProcessor(Command):
    def __init__(self, server, command, *, force=False):
        super().__init__(server, command)
        self.force = force

    async def done(self):
        room = self.server.room
        db = self.server.db
        if self.current == self.P_BEFORE:
            await self.server.print("?? no descr")
            return
        old = room.long_descr.descr if room.long_descr else ""
        nld = "\n".join(self.lines[self.P_BEFORE])
        if self.force or not old:
            if room.long_descr:
                room.long_descr.descr = nld
            else:
                descr = db.LongDescr(descr=nld, room_id=room.id_old)
                db.add(descr)
            await self.server.print(_("Long descr updated."))
        elif old != nld:
            await self.server.print(_("Long descr differs."))
            print(f"""\
*** 
*** Descr: {room.idnn_str}
*** old:
{old}
*** new:
{nld}
***""")
        if self.exits_text:
            await self.server.process_exits_text(room, self.exits_text)

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

    loc2itl=staticmethod(loc2itl)
    itl2loc=staticmethod(itl2loc)
    itl_names=set(_itl2loc.keys())
    loc_names=set(_loc2itl.keys())

    _input_grab = None

    blocked_commands = set(("lang","kurz","ultrakurz"))

    MP_TEXT = 1
    MP_ALIAS = 2
    MP_TELNET = 3
    MP_INFO = 4

    # Prompt handling
    _prompt_evt = None
    _prompt_state = None

    keydir = {
        0: {
            21:"suedwesten",
            22:"sueden",
            23:"suedosten",
            26:"osten",
            29:"nordosten",
            28:"norden",
            27:"nordwesten",
            24:"westen",
            31:"oben",
            30:"unten",
            32:"raus",
            }
        }
    async def setup(self, db):
        self.db = db
        db.setup(self)

        self.send_command_lock = trio.Lock()
        self.me = attrdict(blink_hp=None)

        self.logger = logging.getLogger(self.cfg['name'])
        self.trigger_sender = trio.Event()
        self.exit_match = None
        self.start_rooms = deque()
        self.room = None
        self.view_room = None
        self.selected_room = None  # on the map
        self.is_new_room = False
        self.last_room = None
        self.last_dir = None  # direction that was actually used
        self.room_info = None
        self.last_room_info = None
        self.named_exit = None
        self.command = None
        self.process = None
        self.last_commands = deque()
        self.last_cmd = None  # (compound) command sent
        self.info = None
        self.cmd1_q = []
        self.cmd2_q = []

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
        self.main.start_soon(self._text_writer,rd)
        self.main.start_soon(self._send_loop)
        self.main.start_soon(self._monitor_selection)

        #await self.set_long_mode(True)
        await self.init_mud()

        self._wait_move = trio.Event()

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

        if await self.mud.GUI.angezeigt._nil:
            await self.print("<b>No GUI!</b>")
        elif not await self.mud.GUI.angezeigt:
            await self.mud.initGUI(self.cfg["name"])

        val = await self.mud.gmcp.MG
        if val:
            for x in "base info vitals maxvitals attributes".split():
                try: self.me[x] = AD(val['char'][x])
                except AttributeError: pass
            await self.gui_show_player()

            try:
                info = val["room"]["info"]
            except KeyError:
                pass
            else:
                await self.new_info(info)

        self.main.start_soon(self._sql_keepalive)

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
                    await self.print(_("Unmapped: -{room.id_old}: {room.idn_str}"),room=room)
                    room.id_mudlet = None
                    chg = True
            if chg:
                db.commit()

    async def init_mud(self):
        """
        Send mud specific commands to set the thing to something we understand
        """
        self.send_command("kurz")

    async def post_error(self):
        self.db.rollback()
        await super().post_error()

    async def _text_writer(self, rd):
        async for line in rd:
            await self.mud.print(line)

    async def print(self, msg, **kw):
        if kw:
            msg = msg.format(**kw)
        await self._text_w.send(msg)

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
        await self.send_commands(ml)

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
        for k,v in (await self.mud.getAreaTableSwap())[0].items():
            k = int(k)
            if k in self._area_id2area:
                seen.remove(k)
                continue
            # areas in the mud but not in the database
            self.logger.info(_("SYNC: add area {v} to DB").format(v=v))
            area = db.Area(id=k, name=v)
            db.add(area)
            self._area_name2area[area.name.lower()] = area
            self._area_id2area[area.id] = area
        for k in sorted(seen):
            # areas in the database but not in the mud
            v = self._area_id2area[k].name
            self.logger.info(_("SYNC: add area {v} to Mudlet").format(v=v))
            kk = (await self.mud.addAreaName(v))[0]
            if kk != k:
                # Ugh now we have to renumber it
                self.logger.warning(_("SYNC: renumber area {v}: {k} > {kk}").format(kk=kk, k=k, v=v))
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
        return short2loc(v)

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
        cmd = self.cmdfix("*", cmd, min_words=1)
        db = self.db
        n = 0
        for r in db.q(db.Room).filter(db.Room.label == cmd[0]).all():
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
            flags.append(_("Descr doesn't have exits"))
        if room.flag & db.Room.F_NO_GMCP_ID:
            flags.append(_("ignore this GMCP ID"))
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
        await self.gui_show_room_label(room)

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
        self.main.start_soon(self.send_commands, NoteProcessor(self, cmd))

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
        async with self.input_grabber() as g:
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
        await self.gui_show_room_note()


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
            if x.steps:
                txt += " +"+str(len(x.moves))
            return txt

        if len(cmd) == 0:
            for x in room._exits:
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
            await self.print(_("Cost of {exit.dir} from {room.idn_str} is {exit.cost}"), room=room,exit=x)
            return
        c = x.cost
        if c != cmd[1]:
            await self.print(_("Cost of {exit.dir} from {room.idn_str} changed from {room.cost} to {cost}"), room=room,exit=x,cost=cmd[1])
            x.cost = cmd[1]
            self.db.commit()
        else:
            await self.print(_("Cost of {exit.dir} from {room.idn_str} unchanged"), room=room,exit=x)


    @doc(_(
        """
        Cost of walking through this room
        Set the cost of passing through this room, defined as the min cost
        of leaving it
        Params: [cost [room]]
        """))
    async def alias_rp(self, cmd):
        cmd = self.cmdfix("ir", cmd, min_words=0)
        room = cmd[2] if len(cmd) > 2 else self.view_or_room
        if cmd:
            room.set_cost(cmd[0])
        await self.print(_("Cost of {room.idn_str} is {room.cost}"), room=room)



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
        "-": remove commands.
        Otherwise, add to the list of things to send.
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
        if not self.last_room:
            await self.print(_("I have no idea where you were."))
            return
        if not self.last_dir:
            await self.print(_("I have no idea how you got here."))
            return
        x = self.last_room.exit_at(self.last_dir)
        if not x.steps:
            x.steps = self.last_dir
        x.steps = cmd[0] + "\n" + x.steps
        await self.print(_("Prepended."))
        self.db.commit()

    @doc(_(
        """
        Suffix for the last move.
        Applied to the room you just came from!
        Usage: #xts ‹whatever to send›
        Put at end of the list of things to send.
        If the list was empty, the exit name is added also.
        If nothing to send is given, use the last command.
        """))
    async def alias_xts(self, cmd):
        cmd = self.cmdfix("*", cmd)
        if not self.last_room:
            await self.print(_("I have no idea where you were."))
            return
        if not self.last_dir:
            await self.print(_("I have no idea how you got here."))
            return
        x = self.last_room.exit_at(self.last_dir)
        if not x.steps:
            x.steps = self.last_dir
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
            self.named_exit = cmd[0]
            await self.print(_("Exit name '{self.named_exit}' set."), self=self)

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
        if x.steps:
            await self.print(_("This exit already has steps:"))
            await self.alias_xs(self,x.dir)
            return
        x.steps = x.dir
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
        for x in room._exits:
            await room.set_mud_exit(x.dir, x.dst if x.dst_id and x.dst.id_mudlet else True)
        await self.mud.updateMap()

    @doc(_(
        """
        Sync/update Mudlet map.
        
        Assume that the Mudlet map is out of date / has not been saved:
        write our room data to Mudlet.

        Rooms in the Mudlet map which we don't have are not touched.
        """))
    async def alias_mds(self, cmd):
        db = self.db
        s=0

        await self.print("Sync rooms")
        await self.sync_areas()
        for r in db.q(db.Room).filter(db.Room.id_mudlet != None).order_by(db.Room.id_mudlet).all():
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

        await self.print("Sync exits")
        s=0
        for r in db.q(db.Room).filter(db.Room.id_mudlet != None).order_by(db.Room.id_mudlet).all():
            s2 = r.id_mudlet // 500
            if s != s2:
                s = s2
                await self.print(_("… {room.id_mudlet}"), room=r)
                await self.mud.updateMap()
            for x in r._exits:
                await r.set_mud_exit(x.dir, x.dst if x.dst_id and x.dst.id_mudlet else True)

        await self.print("Done with map sync")
        await self.mud.updateMap()
         
    @doc(_(
        """
        Sync/update Database
        
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
        Basic sync when some mudlet IDs got deleted due to out-of-sync-ness
        """
        await self.sync_map()

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
            r = await self.new_room("unknown", offset_from=self.last_room, offset_dir=self.last_dir)
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
                r = await self.new_room("unknown", offset_from=self.last_room, offset_dir=self.last_dir)
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
        r = cmd[0] or await self.new_room("unknown")
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
        class NotInMudlet(PathChecker):
            @staticmethod
            async def check(room):
                # exits that are in Mudlet.

                if room.id_mudlet is None:
                    return SkipRoute
                if room in self.skiplist:
                    return None
                for x in room._exits:
                    if x.dst_id is None:
                        continue
                    if x.dst.id_mudlet is None:
                        return SkipSignal
                return Continue
        await self.gen_rooms(NotInMudlet())

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
        class NotInDb(PathCHecker):
            @staticmethod
            async def check(room):
                # These rooms are not in the database, so we check for unmapped
                # exits that are in Mudlet.
                mx = None
                if room in self.skiplist:
                    return SkipRoute
                for x in room._exits:
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
            await w.resume(cmd[0] if cmd else 1)
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
            await w.resume(cmd[0] if cmd else 1)
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

        checker = MappedFulltextSkipChecker(last_visit=lim, skiplist=self.skiplist)
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

        checker = MappedThingSkipChecker(thung=txt, skiplist=self.skiplist)
        await self.gen_rooms(check)

    async def clear_gen(self):
        """Cancel path generation"""
        if self.path_gen:
            await self.path_gen.cancel()
            self.path_gen = None

    @doc(_(
        """
        Cancel path generation and whatnot

        No parameters.
        """))
    async def alias_gc(self, cmd):
        await self.clear_gen()
        self.process = None

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
        rl = max(len(x) for x in exits.keys())
        for d,dst in exits.items():
            d += " "*(rl-len(d))
            if dst is None:
                await self.print(_("{d} - unknown"), d=d)
            else:
                await self.print(_("{d} = {dst.info_str}"), dst=dst, d=d)


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
        await self.print(_("pos={room.pos_x}/{room.pos_y}/{room.pos_z} layer={room.area.name}"), room=room)

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
        for x in room._r_exits:
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

    async def send_commands(self, *cmds, err_str="", real_dir=None, send_echo=True):
        """
        Send this list of commands to the MUD.
        The list may include special processing or delays.
        """
        p = SeqProcess(self, cmds, real_dir, send_echo=send_echo)
        await self.run_process(p)

    async def sync_map(self):
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
        logger.debug("AREAS:%r",area_names)

        for r in db.q(db.Room).filter(db.Room.id_old != None, db.Room.id_mudlet != None):
            todo.append(r)
        db.rollback()

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
            done.add(r.id_old)
            x = r.exits

            if r.id_old in broken: continue
            if not r.id_gmcp:
                id_gmcp = (await self.mud.getRoomHashByID(r.id_mudlet))[0]
                if id_gmcp:
                    r.id_gmcp = id_gmcp
                    db.commit()

            if False and r.area_id is None:
                ra = (await self.mud.getRoomArea(r.id_mudlet))[0]
                know_area(ra)
                r.area_id = ra


            try:
                y = await r.mud_exits
            except NoData:
                # didn't go there yet
                logger.debug("EXPLORE %s %s",r.id_str,r.name)
                explore.add(r.id_old)
                continue
            # print(r.id_old,r.id_mudlet,r.name,":".join(f"{k}={v}" for k,v in y.items()), v=v, k=k)
            for d,nr in x.items():
                if not nr: continue
                if nr.id_old in done: continue
                if nr.id_old in broken: continue
                if nr.id_mudlet: continue
                mid = y.get(d,None)
                if not mid:
                    continue
                # print(_("{nr.id_old} = {mid} {d}").format(nr=nr, d=d, mid=mid))
                try:
                    nr.set_id_mudlet(mid)
                    db.commit()
                except IntegrityError:
                    # GAAH
                    db.rollback()

                    done.add(nr.id_old)
                    broken.add(nr.id_old)
                    xr = db.r_mudlet(mid)
                    logger.warning("BAD %s %s = %s %s",nr.id_old,xr.id_old,mid,nr.name)
                    xr.set_id_mudlet(None)
                    #xr.id_gmcp = None
                    #nr.id_gmcp = None
                    broken.add(xr.id_old)
                    db.commit()
                else:
                    todo.appendleft(nr)


    @asynccontextmanager
    async def input_grabber(self):
        w,r = trio.open_memory_channel(1)
        try:
            async def send(x):
                if x == ".":
                    await w.aclose()
                    self._input_grab = None
                    return
                await w.send(x)
            self._input_grab = send
            yield r
        finally:
            with trio.move_on_after(2) as cg:
                cg.shield = True
                if self._input_grab is send:
                    self._input_grab = None
                    await w.aclose()
                    # otherwise done above

    async def called_input(self, msg):
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
        return await self._do_move(msg)

    async def _do_move(self, msg, send_echo=None):
        # return None if the input has been handled.
        # Otherwise return the same (or some other) text to be sent.
        self.named_exit = None
        ms = short2loc(msg)
        if self.room is None:
            await self.send_commands(msg)
        else:
            self.last_cmd = msg
            try:
                x = self.room.exit_at(msg)
            except KeyError:
                await self.send_commands(msg, send_echo=False if send_echo is None else send_echo)
            else:
                self.named_exit = msg
                await self.send_commands(*x.moves, send_echo=bool(x.steps) if send_echo is None else send_echo)


        if self.logfile:
            print(">>>",msg, file=self.logfile)
        return

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
        self.main.start_soon(self._fnkey, key,mod)

    async def _fnkey(self, key, mod):
        try:
            s = self.keydir[mod][key]
        except KeyError:
            await self.print(f"No shortcut {mod}/{key} found.")
        else:
            if isinstance(s,str):
                await self._do_move(s, send_echo=True)
                return
            if isinstance(s,Command):
                s = (s,)
            await self.send_commands(*s)


    # ### Basic command handling ### #


    async def called_prompt(self, msg):
        """
        Called by Mudlet when the MUD sends a Telnet GA
        signalling readiness for the next line
        """
        logger.debug("NEXT")
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
        self.process, p.upstack = p, self.process
        self.trigger_sender.set()

    async def check_more(self, msg):
        if not msg.startswith("--mehr--("):
            return False
        self.main.start_soon(self.mud.send,"\n",False)
        return True

    async def called_text(self, msg):
        """Incoming text from the MUD"""
        if await self.check_more(msg):
            return
        if self.logfile:
            print(msg, file=self.logfile)
        m = self.is_prompt(msg)
        if isinstance(m,str):
            msg = m
            self.log_text_prompt()
            self.prompt(self.MP_TEXT)
            if len(msg) == 2:
                return
            msg = msg[2:]

        msg = msg.rstrip()
        logger.debug("IN  : %s", msg)
        if self.filter_text(msg) is False:
            return
        self.log_text(msg)
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

    async def alias_dss(self, cmd):
        """
        Sender state
        """
        if not self.command:
            await self.print("No command")
        else:
            await self.print(f"Command: {self.command}")
        if not self._prompt_evt:
            await self.print("No prompt wait")
        elif self._prompt_evt.is_set():
            await self.print("Prompt event set")
        else:
            await self.print(f"Prompt event waiting {self._prompt_state}")

        for x in self.cmd1_q:
            await self.print(f"Mudlet: {x}")
        for x in self.cmd2_q:
            await self.print(f"Mapper: {x}")
        x = self.process
        while x:
            await self.print(f"Stack: {x}")
            x = x.upstack

    async def _send_loop(self):
        """
        Main send-some-commands loop.
        """
        seen = True
        while True:
            try:
                if self.info:
                    info,self.info = self.info,None
                    await self.went_to_dir(TIME_DIR, info=info, skip_if_noop=True)
                    continue

                if self.cmd1_q:
                    # Command transmitted by Mudlet.
                    cmd = self.cmd1_q.pop(0)
                    logger.debug("Prompt saw %r",cmd)
                    xmit = False
                elif self.cmd2_q:
                    # Command queued here.
                    # There should be only one, but sometimes the user is
                    # faster than the MUD.
                    cmd = self.cmd2_q.pop(0)
                    logger.debug("Prompt send %r",cmd)
                    xmit = True
                else:
                    # No command queued, so we check the stack.
                    if self.process and seen:
                        logger.debug("Prompt Next %r",self.process)
                        await self.process.next()
                        seen = False
                    else:
                        logger.debug("Prompt Wait %r",self.process)
                        await self.trigger_sender.wait()
                        self.trigger_sender = trio.Event()
                        seen = True
                    continue
                seen = True

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
                    await self.mud.send(cmd.command, cmd.send_echo)
                else:
                    cmd.send_seen = True
                await self._prompt_evt.wait()
                self._prompt_evt = None
                logger.debug("Prompt seen")
                await self._process_prompt()

            except Exception as exc:
                await self.print(f"*** internal error: {exc!r} ***")
                logger.exception("internal error: %r", exc)
                self.cmd2_q = []
                self.process = None
                if self._prompt_evt:
                    self._prompt_evt.set()
                    self._prompt_evt = None


    def match_exit(self, msg):
        """
        Match the "there is one exit" or "there are N exits" line which
        separates the room description from the things in the room.

        TODO: if there's no GMCP this may signal that you have moved.
        """
        def matched(g=None):
            if self.command:
                self.command.dir_seen(g)

        m = None
        if self.exit_match is None:
            m = EX3.search(msg)
            if not m:
                m = EX4.search(msg)
            if not m:
                m = EX5.search(msg)
            if m:
                self.exit_match = False
                matched()
                return True

            m = EX1.search(msg)
            if not m:
                m = EX2.search(msg)
            if m:
                matched(m.group(1))
                self.exit_match = True

        if self.exit_match is True:
            if not m:
                matched(msg)
            if EX9.search(msg):
                self.exit_match = False
                return True

        return self.exit_match


    def log_text(self, msg):
        if self.match_exit(msg):
            return

        if self.command:
            self.command.add(msg)

    def log_text_prompt(self):
        if self.command:
            self.command.prompt_seen()

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

    def _trigger_prompt(self):
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
            self._trigger_prompt()
            return

        if mode == self.MP_TEXT:
            if self.cmd1_q:
                # The user typed a command while we were working.
                self._trigger_prompt()
            elif self._prompt_state is None:
                self._prompt_state = True
                self.main.start_soon(self._trigger_prompt_later, 0.5)
            elif self._prompt_state is False:
                self._trigger_prompt()
            return

        if mode == self.MP_TELNET:
            if self._prompt_state is None:
                self._prompt_state = False
                self.main.start_soon(self._trigger_prompt_later, 0.5)
            elif self._prompt_state is True:
                self._trigger_prompt()
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
        if self.exit_match:
            await self.print(_("WARNING: Exit matching failed halfway!"))
        exits_seen = self.exit_match is False
        self.exit_match = None
        await self._to_text_watchers(None)

        # finish the current command
        s = self.command
        if s is None:
            # no no command yet
            return
        await s.done()

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

        moved = False
        nr = None
        if s.info:
            i_gmcp = s.info.get("id","")
            i_short = s.info.get("short","")

            # Changing info data generally indicate movement.
            nr = self.room_by_gmcp(i_gmcp)
            if nr and r.id_gmcp != nr.id_gmcp:
                moved = True
            elif i_short and self.clean_shortname(r.name) != self.clean_shortname(i_short) and not (r.flag & self.db.Room.F_MOD_SHORTNAME):
                await self.print(_("Shortname differs. #mm?"))
                # moved = True

        d = self.named_exit or short2loc(s.command)
        try:
            x = self.room.exit_at(d)
        except KeyError:
            x = None

        if x and not exits_seen:
            if x.dst and x.dst.flag & self.db.Room.F_NO_EXIT:
                exits_seen = True

        if isinstance(s, LookProcessor):
            # This command does not generate movement. Thus if it did
            # anyway the move was probably timed, except when the exit
            # already exists. Life is complicated.
            if x is None:
                d = TIME_DIR

        if exits_seen:
            if x is not None:
                # There is an exit thus we seem to have used it
                moved = True
            elif is_std_dir(d):
                # Standard directions generally indicate movement.
                # Exit may be 'created' by opening a door or similar.
                moved = True

        if nr or moved:
            # Consume the movement.
            self.named_exit = None

            await self.went_to_dir(d, info=s.info, exits_text=s.exits_text)
        self.db.commit()

    def room_by_gmcp(self, id_gmcp):
        if not id_gmcp:
            return None
        try:
            return self.db.r_hash(id_gmcp)
        except NoData:
            return None

    async def process_exits_text(self, room, txt):
        xx = room.exits
        for x in self.exits_from_line(txt):
            if x not in xx:
                await room.set_exit(x)
                await self.print(_("New exit: {exit}"), exit=x,room=room)


    async def handle_event(self, msg):
        name = msg[0].replace(".","_")
        hdl = getattr(self, "event_"+name, None)
        if hdl is not None:
            await hdl(msg)
            return
        if msg[0].startswith("gmcp.") and msg[0] != msg[1]:
            # not interesting, will show up later
            return
        if msg[0] == "sysTelnetEvent":
            msg[3] = "".join("\\x%02x"%b if b<32 or b>126 else chr(b) for b in msg[3].encode("utf8"))
        logger.debug("%r", msg)

    async def initGMCP(self):
        await self.mud.sendGMCP("""Core.Supports.Debug 20""")
        await self.mud.sendGMCP("""Core.Supports.Set [ "MG.char 1", "MG.room 1", "comm.channel 1" ] """)
    async def event_sysProtocolEnabled(self, msg):
        if msg[1] == "GMCP":
            await self.initGMCP()

    async def event_gmcp_MG_char_attributes(self, msg):
        logger.debug("AttrMG %s: %r",msg[1],msg[2])
        self.me.attributes = AD(msg[2])
        await self.gui_show_player()

    async def event_gmcp_MG_char_base(self, msg):
        logger.debug("BaseMG %s: %r",msg[1],msg[2])
        self.me.base = AD(msg[2])
        await self.gui_show_player()

    async def event_gmcp_MG_char_info(self, msg):
        logger.debug("InfoMG %s: %r",msg[1],msg[2])
        self.me.info = AD(msg[2])
        await self.gui_show_player()

    async def event_gmcp_MG_char_vitals(self, msg):
        logger.debug("VitalsMG %s: %r",msg[1],msg[2])
        self.me.vitals = AD(msg[2])
        await self.gui_show_vitals()

    async def event_gmcp_MG_char_maxvitals(self, msg):
        logger.debug("MaxVitalsMG %s: %r",msg[1],msg[2])
        self.me.maxvitals = AD(msg[2])
        await self.gui_show_vitals()


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
        await self.gui_show_room_data(room)

    async def event_sysDataSendRequest(self, msg):
        logger.debug("OUT : %s", msg[1])
        # We are sending data. Thus there won't be a prompt, thus we take
        # the prompt signal that might already be there out of the channel.
        if not self.command or self.command.send_seen:
            pass
        elif self.command.command != msg[1]:
            await self.print(_("WARNING last_command differs, stored {sc!r} vs seen {lc!r}"), sc=self.command.command,lc=msg[1])
        else:
            self.command.send_seen = True
            return

        self.cmd1_q.append(msg[1])
        self.trigger_sender.set()

    async def event_gmcp_MG_room_info(self, msg):
        if len(msg) > 2:
            info = AD(msg[2])
        else:
            info = await self.mud.gmcp.MG.room.info

        if self._prompt_evt is not None and self.command.info is None:
            # waiting for prompt, during a command
            self.command.info = info
        else:
            # not waiting, so process it from the main loop
            self.info = info
            self.trigger_sender.set()

    async def event_gmcp_comm_channel(self, msg):
        # don't do a thing
        msg = AD(msg[2])
        logger.debug("CHAN %r",msg)
        chan = msg.chan
        player = msg.player
        prefix = f"[{msg.chan}:{msg.player}] "
        txt = msg.msg.rstrip("\n")
        if txt.startswith(prefix):
            txt = txt.replace(prefix,"").replace("\n"," ").replace("  "," ").strip()
            await self.print(prefix+txt.rstrip("\n"))  # TODO color
        else:
            prefix = f"[{msg.chan}:{msg.player} "
            if txt.startswith(prefix) and txt.endswith("]"):
                txt = txt[len(prefix):-1]
            await self.print(prefix+txt.rstrip("\n")+"]")  # TODO color

    async def new_room(self, descr, id_gmcp=None, id_mudlet=None, offset_from=None,
            offset_dir=None, area=None):

        if id_mudlet:
            try:
                room = self.db.r_mudlet(id_mudlet)
            except NoData:
                pass
            else:
                if offset_from and offset_dir:
                    self.logger.error(_("Not in mudlet? but we know mudlet# {id_mudlet} at {offset_room.id_str}/{offset_dir}").format(offset_room=offset_room, offset_dir=offset_dir, id_mudlet=id_mudlet))
                else:
                    self.logger.error(_("Not in mudlet? but we know mudlet# {id_mudlet}").format(id_mudlet=id_mudlet))
                return None

        if id_gmcp:
            try:
                room = self.db.r_hash(id_gmcp)
            except NoData:
                pass
            else:
                self.logger.error(_("New room? but we know hash {hash} at {room.id_old}").format(room=room, hash=id_gmcp))
                return None
            mid = await self.mud.getRoomIDbyHash(id_gmcp)
            if mid and mid[0] and mid[0] > 0:
                if id_mudlet is None:
                    id_mudlet = mid[0]
                elif id_mudlet != mid[0]:
                    await self.print(_("Collision: mudlet#{idm} but hash#{idh} with {hash!r}"), idm=id_mudlet, idh=mid[0], hash=id_gmcp)
                    return

        if offset_from is None and id_mudlet is None:
            self.logger.warning("I don't know where to place the room!")
            await self.print(_("I don't know where to place the room!"))

        room = self.db.Room(name=descr, id_mudlet=id_mudlet)
        if id_gmcp:
            room.id_gmcp = id_gmcp
        self.db.add(room)
        self.db.commit()

        is_new = await self.maybe_assign_mudlet(room, id_mudlet)
        await self.maybe_place_room(room, offset_from,offset_dir, is_new=is_new)

        if (area is None or area.flag & self.db.Area.F_IGNORE) and offset_from is not None:
            area = offset_from.area
        if area is not None and not (area.flag & self.db.Area.F_IGNORE):
            await room.set_area(area)

        self.db.commit()
        logger.debug("ROOM NEW:%s/%s",room.id_old, room.id_mudlet)
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

    async def maybe_place_room(self, room, offset_from,offset_dir, is_new=False):
        """
        Place the room if it isn't already.
        """
        x,y,z = None,None,None
        if room.id_mudlet:
            try:
                x,y,z = await self.mud.getRoomCoordinates(room.id_mudlet)
            except ValueError:
                # Huh. Room deleted there. Get a new room then.
                room.set_id_mudlet((await self.rpc(action="newroom"))[0])

        if offset_from and (x is None or (x,y,z) == (0,0,0)):
            await self.place_room(offset_from,offset_dir,room)

        else:
            # mudlet position is kindof authoritative
            room.pos_x, room.pos_y, room.pos_z = x,y,z

    async def place_room(self, start,dir,room):
        """
        Went from start via dir to room. Move room.
        """
        # x,y,z = start.pos_x,start.pos_y,start.pos_z
        x,y,z = await self.mud.getRoomCoordinates(start.id_mudlet)
        dx,dy,dz = dir_off(dir, self.conf['dir_use_z'],
                d_x=self.conf["pos_x_delta"], d_y=self.conf["pos_y_delta"],
                d_small=self.conf["pos_small_delta"])
        x += dx; y += dy; z += dz
        room.pos_x, room.pos_y, room.pos_z = x,y,z
        await self.mud.setRoomCoordinates(room.id_mudlet, x,y,z)

    async def move_to(self, moved, src=None, dst=None):
        """
        Move from src / the current room to dst / ? in this direction

        Returns: a new room if "dst" is True, dst if that is set, the room
        in that direction otherwise, None if there's no room there (yet).
        """

    async def alias_dbg(self, cmd):
        breakpoint()
        pass

    async def new_info(self, info, moved=None):
        db=self.db
        rc = False
        src = False
        is_new_room = False

        if info == self.room_info and moved is None:
            await self.print("INFO same and not moved")
            return

        logger.debug("INFO:%r",info)
        if self.conf['debug_new_room']:
            import pdb;pdb.set_trace()

        self.last_room_info = self.room_info
        self.room_info = info

        id_gmcp = info.get("id", None)
        if id_gmcp and self.room and self.room.id_gmcp == id_gmcp:
            return

        # Case 1: we have a hash.
        room = None

        if id_gmcp:
            try:
                room = db.r_hash(id_gmcp)
            except NoData:
                pass
            else:
                await self.went_to_room(room)
            if moved is None and self.command:
                moved = self.command.command


    async def update_exit(self, room, d):
        try:
            x = self.room.exit_to(room)
        except KeyError:
            x = None
        else:
            if is_std_dir(d):
                x = None

        if x is None:
            x,_src = await self.room.set_exit(d, room, force=False)
        else:
            _src = False
        if x.dst != room:
            await self.print(_("""\
Exit {exit} points to {dst.idn_str}.
You're in {room.idn_str}.""").format(exit=x.dir,dst=x.dst,room=room))
        #src |= _src

        if self.conf['add_reverse']:
            rev = loc2rev(d)
            if rev:
                xr = room.exits.get(rev, NoData)
                if xr is NoData:
                    await self.print(_("This room doesn't have a back exit {dir}."), room=room,dir=rev)
                elif xr is not None and xr != self.room:
                    await self.print(_("Back exit {dir} already goes to {xr.idn_str}!"), xr=xr, dir=rev)
                else:
                    await room.set_exit(rev, self.room)
            else:
                try:
                    self.room.exit_to(room)
                except KeyError:
                    await self.print(_("I don't know the reverse of {dir}, way back not set"), dir=d)
                # else:
                #     some exit to where we came from exists, so we don't complain

    def clean_shortname(self, name):
        """
        Clean up the MUD's shortnames.
        """
        name = name.strip()
        name = name.rstrip(".")
        if name:
            if name[0] in "'\"" and name[0] == name[-1]:
                name = name[1:-1]
            name = name.strip()
        if not name:
            return "<namenloser Raum>"
        return name

    def clean_thing(self, name):
        """
        Clean up the MUD's thing names.
        """
        name = name.strip()
        name = name.rstrip(".")
        return name

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
                rn = self.command.lines[0][-1]
            except (AttributeError,IndexError):
                rn = "unknown"
            if room is None:
                room = await self.new_room(rn, offset_from=self.room, offset_dir=d, id_mudlet=id_mudlet, id_gmcp=id_gmcp)
                is_new = True
            else:
                room.id_mudlet = id_mudlet
                room.area = None
            await self.room.set_exit(d, room)
            db.commit()

        await self.went_to_room(room, d=d, is_new=is_new, info=info, exits_text=exits_text)

    async def went_to_room(self, room, d=None, repair=False, is_new=False, info=None, exits_text=None):
        """You went to `room` using direction `d`."""
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
        #await self.check_walk()
        nr,self._wait_move = self._wait_move, trio.Event()
        nr.set()
        await self.gui_show_room_data()

        if not self.last_room:
            return

        p = self.command
        rn = ""
        if info:
            rn = info.get("short","")
        if not rn and p.current > Command.P_BEFORE and p.lines[0]:
            rn = p.lines[0][-1]
        if rn:
            rn = self.clean_shortname(rn)
        if rn:
            room.last_shortname = rn
            if not room.name:
                room.name = rn
                db.commit()
            elif room.name == rn:
                pass
            elif self.clean_shortname(room.name) == rn:
                room.name = rn
                db.commit()
            else:
                await self.print(_("Name: old: {room.name}"), room=room,name=rn)
                await self.print(_("Name: new: {name}"), room=room,name=rn)
                await self.print(_("Use '#rs' to update it"))

        if room.long_descr:
            for t in p.lines[p.P_AFTER]:
                for tt in SPC.split(t):
                    room.has_thing(tt)
        else:
            self.main.start_soon(self.send_commands, LookProcessor(self, "schau"))
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
        self.main.start_soon(self.send_commands,LookProcessor(self, "schau", force=True))

    @doc(_(
        """
        Update room shortname
        Either the last shortname, or whatever you say here.
        """))
    async def alias_rs(self, cmd):
        db = self.db
        cmd = self.cmdfix("*", cmd)
        sn = cmd[0] if cmd else self.room.last_shortname
        if not sn:
            await self.print("No shortname known")
            return
        if not self.room:
            await self.print("No current room")
            return

        self.room.name = sn
        await self.mud.setRoomName(self.room.id_mudlet, sn)
        if cmd:
            self.room.flag |= db.Room.F_MOD_SHORTNAME
        else:
            self.room.flag &=~ db.Room.F_MOD_SHORTNAME
        db.commit()


    # ### Status GUI ### #

    async def gui_show_room_data(self, room=None):
        if room is None:
            room = self.room
        if room.id_mudlet:
            await self.mud.centerview(room.id_mudlet)
        else:
            await self.print("WARNING: you are in an unmapped room.")
        r,g,b = 30,30,30  # adapt for parallel worlds or whatever
        await self.mmud.GUI.ort_raum.echo(room.name)
        if room.area:
            await self.mmud.GUI.ort_region.echo(f"{room.area.name} [{room.id_str}]")
        else:
            await self.mmud.GUI.ort_region.echo(f"? [{room.id_str}]")
            r,g,b = 80,30,30

        await self.mmud.GUI.ort_raum.setColor(r, g, b)
        await self.mmud.GUI.ort_region.setColor(r, g, b)
        await self.gui_show_room_label(room)
        await self.gui_show_room_note(room)

    async def gui_show_room_note(self, room=None):
        if room is None:
            room = self.room
            if room is None:
                return
        if room.note:
            note = room.note.note
            try:
                lf = note.index("\n")
            except ValueError:
                pass
            else:
                note = note[:lf]
        else:
            note = "-"
        await self.mmud.GUI.raumnotizen.echo(note)

    async def gui_show_room_label(self, room):
        label = room.label or "-"
        await self.mmud.GUI.raumtype.echo(label)

    async def gui_show_player(self):
        try: name = self.me.base.name
        except AttributeError: name = "?"
        try: level = self.me.info.level
        except AttributeError: level = "?"
        await self.mmud.GUI.spieler.echo(f"{name} [{level}]")


            #R=db.q(db.Room).filter(db.Room.id_old == 1).one()
            #R.id_mudlet = 142
            #db.commit()
        #import pdb;pdb.set_trace()
        pass

    async def _gui_vitals_color(self, lp_ratio=None):
        if lp_ratio is None:
            try:
                lp_ratio = self.me.vitals.hp/self.me.maxvitals.hp
            except AttributeError:
                lp_ratio = 0.9
        if lp_ratio > 1:
            lp_ratio = 1
        await self.mmud.GUI.lp_anzeige.setColor(255 * (1 - lp_ratio), 255 * lp_ratio, 50)

    async def _gui_vitals_blink_hp(self, task_status=trio.TASK_STATUS_IGNORED):
        with trio.CancelScope() as cs:
            self.me.blink_hp = cs
            task_status.started(cs)
            try:
                await self.mmud.GUI.lp_anzeige.setColor(255, 0, 50)
                await trio.sleep(0.3)
                await self._gui_vitals_color()
            finally:
                if self.me.blink_hp == cs:
                    self.me.blink_hp = None

    async def gui_show_vitals(self):
        try:
            v = self.me.vitals
        except AttributeError:
            return
        try:
            w = self.me.maxvitals
        except AttributeError:
            await self.mmud.GUI.lp_anzeige.setValue(1,1, f"<b> {v.hp}/?</b> ")
            await self.mmud.GUI.kp_anzeige.setValue(1,1, f"<b> {v.sp}/?</b> ")
            await self.mmud.GUI.gift.echo("")
        else:
            await self.mmud.GUI.lp_anzeige.setValue(v.hp,w.max_hp, f"<b> {v.hp}/{w.max_hp}</b> ")
            await self.mmud.GUI.kp_anzeige.setValue(v.sp,w.max_sp, f"<b> {v.sp}/{w.max_sp}</b> ")
            if v.poison:
                r,g,b = 255,255-160*v.poison/w.max_poison,0
                line = f"G I F T  {v.poison}/{w.max_poison}"
            else:
                r,g,b = 30,30,30
                line = ""
            await self.mmud.GUI.gift.echo(line, "white")
            await self.mmud.GUI.gift.setColor(r, g, b)

            if not self.me.blink_hp:
                await self._gui_vitals_color(v.hp / w.max_hp)

        if "last_hp" in self.me and self.me.last_hp > v.hp:
            await self.main.start(self._gui_vitals_blink_hp)
        self.me.last_hp = v.hp

        # TODO flight

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
        """Focus on a room
        Either a room# or the selected room on the map or the current room
        """))
    async def alias_v(self, cmd):
        cmd = self.cmdfix("r", cmd)
        if cmd:
            await self.view_to(cmd[0])
        else:
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
        cmd = self.cmdfix("w", cmd)
        room = self.room
        if not room:
            await self.print(_("No active room."))
            return
        if cmd:
            w = self.db.word(cmd[0], create=True)
            self.next_word = self.room.with_word(w, create=True)
            self.main.start_soon(self.send_commands, WordProcessor(self, self.room, w, "unt "+cmd[0]))
        elif self.next_word:
            w = self.next_word.word
            self.main.start_soon(self.send_commands, WordProcessor(self, self.room, w, "unt "+w.name if w.name else "schau"))
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
            self.main.start_soon(self.send_commands, WordProcessor(self, self.room, w.word, "schau"))
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
        nw = self.room.with_word(alias.alias_for(real))
        if self.next_word.word == alias:
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
    List all known features / a feature's exits.
    """))
    async def alias_xfl(self, cmd):
        db = self.db
        cmd = self.cmdfix("w", cmd)
        if cmd:
            f = db.feature(cmd[0])
            if f is None:
                await self.print(_("Feature unknown."))
            else:
                for x in f.exits:
                    await self.print(x.info_str)

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
        async with self.input_grabber() as g:
            async for line in g:
                txt += line+"\n"

        f.enter = txt
        db.commit()
        await self.print(_("{f.name}: enter commands set"), f=f)

    @doc(_("""
    Commands for exiting
    Set/replace the commands sent when passing a feature'd exit
    """))
    async def alias_xflb(self, cmd):
        db = self.db
        cmd = self.cmdfix("w", cmd, min_words=1)
        f = db.feature(cmd[0])

        txt = ""
        if f.enter:
            await self.print(_("Old content:\n{txt}"), txt=f.enter)
        await self.print(_("Set feature-enter text. End with '.'."))
        async with self.input_grabber() as g:
            async for line in g:
                txt += line+"\n"

        f.enter = txt
        db.commit()
        await self.print(_("{f.name}: enter commands set"), f=f)

    @with_alias("xf+")
    @doc(_("""
    Set feature
    Set the exit which you just used to have this feature.
    Does not actually apply the feature.
    """))
    async def alias_xf_p(self, cmd):
        db = self.db
        cmd = self.cmdfix("w", cmd, min_words=1)
        f = db.feature(cmd[0])
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
        d = loc2rev(self.last_dir)
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

    async def _q_state(self, q):
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
    """))
    async def alias_qo(self, cmd):
        cmd = self.cmdfix("ii", cmd, min_words=1)
        if not self.quest:
            await self.print(_("No current quest."))
            return
        ns = cmd[-1]
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
    Quest Next Step
    Do the next thing in this quest
    """))
    async def alias_qn(self, cmd):
        cmd = self.cmdfix("i", cmd)
        if not self.quest:
            await self.print(_("No current quest."))
            return
        if cmd:
            self.quest.step = cmd[0]
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
                await self.print(_("Quest: Already walkting to room {room.idn_str}"), room=qs.room)
                await self.print(_("Walker: {w}"), w=w)

            elif self.path_gen and isinstance(self.path_gen.checker,RoomFinder) and self.path_gen.checker.room == qs.room:
                await self.print(_("Quest: Patience! Still finding room {room.idn_str}"), room=qs.room)
            else:
                await self.print(_("Quest: Going to room {room.idn_str}"), room=qs.room)
                await self.run_to_room(qs.room)
            return

        c = qs.command
        if c[0] != '#':
            await self._do_move(c)
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

    async def run(self, db, logfile):
        logger.debug("connected")
        self.logfile = logfile
        print(f"""
*** Start *** {datetime.now().strftime("%Y-%m-%d %H:%M")} ***
""", file=logfile)
        if logfile is not None:
            logfile.flush()

        await self.setup(db)

        try:
            info = await self.mud.gmcp.MG.room.info
        except RuntimeError:
            pass
        else:
            await self.new_info(info)

        #await self.mud.centerview()

        try:
            async with self.event_monitor("*") as h:
                async for msg in h:
                    await self.handle_event(msg['args'])
        except Exception as exc:
            logger.exception("END")
            raise
        except BaseException as exc:
            logger.exception("END")
            raise
        else:
            logger.error("END")
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
@click.option("-l","--log", type=click.File("a"), help="Log file")
@click.option("-d","--debug", is_flag=True, help="Debug output")
@click.option("-m","--migrate", count=True, help="do SQL migration? use -mm for deleting anything, -mmm for dropping tables")
async def main(config,log,debug,migrate):
    m={}
    if config is None:
        config = open("mapper.cfg","r")
    cfg = yaml.safe_load(config)
    cfg = combine_dict(cfg, DEFAULT_CFG, cls=attrdict)
    logging.basicConfig(level=logging.DEBUG if debug else getattr(logging,cfg.log['level'].upper()))

    if not log and cfg.logfile is not None:
        log = open(cfg.logfile, "a")
    with SQL(cfg) as db:
        logger.debug("checking database")
        if migrate:
            run_alembic(db, migrate)

        logger.debug("waiting for connection from Mudlet")
        async with S(cfg=cfg) as mud:
            await mud.run(db=db, logfile=log)

if __name__ == "__main__":
    main()
