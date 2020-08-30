#!/usr/bin/python3

from mudlet import Server, Alias, with_alias, run_in_task
import trio
from mudlet.util import ValueEvent, attrdict, combine_dict
from functools import partial
import logging
import asyncclick as click
import yaml
import json
from contextlib import asynccontextmanager, contextmanager
from collections import deque
from weakref import ref
from inspect import iscoroutine
import re

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from .sql import SQL, NoData
from .const import SignalThis, SkipRoute, SkipSignal
from .const import ENV_OK,ENV_STD,ENV_SPECIAL,ENV_UNMAPPED
from .walking import Walker, PathGenerator
from ..util import doc

import logging
logger = logging.getLogger(__name__)

def AD(x):
    return combine_dict(x, cls=attrdict, force=True)

DEFAULT_CFG=attrdict(
        name="Morgengrauen",  # so far
        sql=attrdict(
            url='mysql://user:pass@server.example.com/morgengrauen'
            ),
        settings=attrdict(
            use_mg_area = False,
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
EX9 = re.compile(r"\.\s*$")
SPC = re.compile(r"\s{3,}")
WORD = re.compile(r"(\w+)")
DASH = re.compile(r"- +")

RF_no_exit = (1<<0)  # the room doesn't show an Exits line

CFG_HELP=attrdict(
        use_mg_area=_("Use the MUD's area name"),
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

class Processor:
    """
    Class to collect one output to the MUD and whatever input you get in
    response.
    """
    P_BEFORE = 0
    P_AFTER = 1
    P_LATE = 2

    got_info = None
    # None: not yet
    # False: prompt seen

    def __init__(self, server, command):
        self.server = server
        self.command = command
        self.lines = ([],[],[])
        self.exits_text = None
        # before reporting exits or whatever
        # after reporting exits but before the prompt
        # after the prompt
        self.current = self.P_BEFORE

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
        pass

class LookProcessor(Processor):
    def __init__(self, server, command, *, force=False):
        super().__init__(server, command)
        self.force = force

    async def done(self):
        room = self.server.room
        db = self.server.db
        if self.current == self.P_BEFORE:
            await self.server.mud.print("?? no descr")
            return
        old = room.long_descr
        nld = "\n".join(self.lines[self.P_BEFORE])
        if self.force or not old:
            room.long_descr = nld
            await self.server.mud.print(_("Long descr updated."))
        elif old != nld:
            await self.server.mud.print(_("Long descr differs."))
            print(f"""\
*** 
*** Descr: {room.idnn_str}
*** old:
{old}
*** new:
{nld}
***""")
        if self.exits_text:
            await self.server.process_exits_text(self.exits_text)

        for t in self.lines[self.P_AFTER]:
            for tt in SPC.split(t):
                room.has_thing(tt)

        db.commit()

class WordProcessor(Processor):
    """
    A processor that takes your result and adds it to the room's search
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
            await self.server.mud.print(_("No data?."))
            return
        # TODO check for NPC status in last line
        await self.server.add_words(" ".join(l), room=self.room)
        if self.server.room == self.room:
            if self.word:
                if self.server.next_word and self.server.next_word.word == self.word:
                    await self.server.next_search(mark=True)
            else:
                await self.server.next_search(mark=False, again=False)


class NoteProcessor(Processor):
    async def done(self):
        await self.server.add_processor_note(self)


class S(Server):

    loc2itl=staticmethod(loc2itl)
    itl2loc=staticmethod(itl2loc)
    itl_names=set(_itl2loc.keys())
    loc_names=set(_loc2itl.keys())

    _input_grab = None

    blocked_commands = set(("lang","kurz","ultrakurz"))

    async def setup(self, db):
        self.db = db
        db.setup(self)

        self.send_command_lock = trio.Lock()
        self.me = attrdict(blink_hp=None)

        self.logger = logging.getLogger(self.cfg['name'])
        self.sent = deque()
        self.sent_new = None
        self.sent_prompt = None
        self.exit_match = None
        self.start_rooms = deque()
        self.room = None
        self.is_new_room = False
        self.last_room = None
        self.last_dir = None
        self.room_info = None
        self.last_room_info = None
        self.last_shortname = None
        self.named_exit = None
        self.last_command = None
        self.next_word = None  # word to search for

        self.long_mode = True  # long
        self.long_lines = []
        self.long_descr = None

        #self.player_room = None
        self.walker = None
        self.path_gen = None
        self._prompt_s, self._prompt_r = trio.open_memory_channel(1)
        # assume that there's a prompt
        self._prompt_s.send_nowait(None)
        self.skiplist = set()
        self.last_saved_skiplist = None
        self._text_monitors = set()

        self._text_w,rd = trio.open_memory_channel(1000)
        self.main.start_soon(self._text_writer,rd)

        #await self.set_long_mode(True)
        await self.init_mud()

        self._wait_move = trio.Event()

        self._area_name2area = {}
        self._area_id2area = {}

        await self.initGMCP()
        await self.mud.setCustomEnvColor(ENV_OK, 0,255,0, 255)
        await self.mud.setCustomEnvColor(ENV_STD, 0,180,180, 255)
        await self.mud.setCustomEnvColor(ENV_SPECIAL, 65,65,255, 255)
        await self.mud.setCustomEnvColor(ENV_UNMAPPED,255,225,0, 255)

        await self.sync_areas()

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
            await self.mud.print("<b>No GUI!</b>")
        elif not await self.mud.GUI.angezeigt:
            await self.mud.initGUI(self.cfg["name"])

        val = await self.mud.gmcp.MG
        if val:
            for x in "base info vitals maxvitals attributes".split():
                try: self.me[x] = AD(val['char'][x])
                except AttributeError: pass
            await self.gui_player()

            try:
                info = val["room"]["info"]
            except KeyError:
                pass
            else:
                await self.new_info(info)

        self.main.start_soon(self._sql_keepalive)

    async def init_mud(self):
        """
        Send mud specific commands to set the thing to something we understand
        """
        await self.send_commands("kurz")

    async def _text_writer(self, rd):
        async for line in rd:
            await self.mud.print(line)

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

        self.alias.at("m").helptext = _("Mapping")
        self.alias.at("mu").helptext = _("Find unmapped rooms/exits")
        self.alias.at("g").helptext = _("Walking, paths")
        self.alias.at("mc").helptext = _("Map Colors")
        self.alias.at("md").helptext = _("Mudlet functions")
        self.alias.at("r").helptext = _("Rooms")
        self.alias.at("v").helptext = _("View Map")
        self.alias.at("cf").helptext = _("Change boolean settings")
        self.alias.at("co").helptext = _("Room and name positioning")
    
    def _cmdfix_r(self,v):
        """
        cmdfix add-on that interprets room names.
        Negative=ours, positive=Mudlet's
        """
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
                await self.mud.print(_("Config item '{cmd}' unknown.").format(cmd=cmd))
            else:
                await self.mud.print(_("{cmd} = {v}.").format(v=v, cmd=cmd))

        else:
            for k,vt in DEFAULT_CFG.settings.items():
                # we do it this way because dicts are sorted
                v = self.conf[k]
                if isinstance(vt,bool):
                    v=_("ON") if v else _("off")
                await self.mud.print(f"{str(v):>5} = {CFG_HELP[k]}")

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
        await self._conf_flip("use_mg_area")

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
            await self.mud.print(_("You can't delete the room you're in."))
            return
        if self.last_room == room:
            await self.mud.print(_("You can't delete the room you just came from."))
            return
        await self._delete_room(room)

    async def _delete_room(self, room):
        id_mudlet = room.id_mudlet
        self.db.delete(room)
        self.db.commit()
        if id_mudlet:
            await self.mud.deleteRoom(id_mudlet)
            await self.mud.updateMap()

    @doc(_(
        """Show room flags
        """))
    async def alias_rf(self, cmd):
        cmd = self.cmdfix("r", cmd)
        if cmd:
            room = cmd[0]
        else:
            room = self.room
        flags = []
        if not room.flag:
            await self.mud.print(_("No flag set."))
            return
        if room.flag & RF_no_exit:
            flags.append(_("Descr doesn't have exits"))
        await self.mud.print(_("Flags: {flags}").format(flags=" ".join(flags)))

    @doc(_(
        """Flag: no-exit-line
        """))
    async def alias_rfe(self, cmd):
        cmd = self.cmdfix("r", cmd)
        if cmd:
            room = cmd[0]
        else:
            room = self.room
        flags = []
        if room.flag & RF_no_exit:
            room.flag &=~ RF_no_exit
            await self.mud.print(_("Room normal: has Exits line."))
        else:
            room.flag |= RF_no_exit
            await self.mud.print(_("Room special: has no Exits line."))
        self.db.commit()

    @doc(_(
        """Show/change the current room's label.
        No arguments: show the label.
        '-': delete the label
        Anything else is set as label (single word only).
        """))
    async def alias_rt(self, cmd):
        cmd = self.cmdfix("w", cmd)
        if not self.room:
            await self.mud.print(_("No active room."))
            return
        if not cmd:
            if self.room.label:
                await self.mud.print(_("Label of {room.idn_str}: {room.label}").format(room=self.room))
            else:
                await self.mud.print(_("No label for {room.idn_str}.").format(room=self.room))
        else:
            cmd = cmd[0]
            if cmd == "-":
                if self.room.label:
                    await self.mud.print(_("Label of {room.idn_str} was {room.label}").format(room=self.room))
                    self.room.label = None
                    self.db.commit()
            elif self.room.label != cmd:
                if self.room.label:
                    await self.mud.print(_("Label of {room.idn_str} was {room.label}").format(room=self.room))
                else:
                    await self.mud.print(_("Label of {room.idn_str} set").format(room=self.room))
                self.room.label = cmd
            else:
                await self.mud.print(_("Label of {room.idn_str} not changed").format(room=self.room))
        self.db.commit()
        await self.show_room_label(self.room)

    async def add_processor_note(self, proc):
        """
        Take the output+input from a processor and add it to the text
        """
        txt = _("> {cmd}").format(room=self.room, cmd=proc.command)
        txt += "\n"+"\n".join(proc.lines[proc.P_BEFORE])
        await self._add_room_note(txt)

    @doc(_(
        """Show the room's note.
        
        With a number, add the n'th last command and its output to the note.
        With some text, send that command and add its output to the note.
        """))
    async def alias_rn(self, cmd):
        if cmd:
            try:
                cmd = int(cmd)
            except ValueError:
                self.main.start_soon(self.send_commands, NoteProcessor(self, cmd))
            else:
                await self.add_processor_note(self.sent[cmd-1])
        else:
            if not self.room:
                await self.mud.print(_("No active room."))
                return
            if self.room.note:
                await self.mud.print(_("Note of {room.idn_str}:\n{room.note}").format(room=self.room))
            else:
                await self.mud.print(_("No note for {room.idn_str}.").format(room=self.room))

    @with_alias("rn-")
    @doc(_(
        """Delete the room's notes."""))
    async def alias_rn_m(self, cmd):
        if not self.room:
            await self.mud.print(_("No active room."))
            return
        if self.room.note:
            await self.mud.print(_("Note of {room.idn_str} was:\n{room.note}").format(room=self.room))
            self.room.note = None
            self.db.commit()
            await self.show_room_note()

    @with_alias("rn.")
    @doc(_(
        """Add text to the room's note."""))
    async def alias_rn_dot(self, cmd):
        if not self.room:
            await self.mud.print(_("No active room."))
            return
        txt = ""
        await self.mud.print(_("Extending note. End with '.'."))
        async with self.input_grabber() as g:
            async for line in g:
                txt += line+"\n"
        await self._add_room_note(txt)

    @with_alias("rn+")
    @doc(_(
        """Add a line to the room's note."""))
    async def alias_rn_plus(self, cmd):
        if not self.room:
            await self.mud.print(_("No active room."))
            return
        if not cmd:
            await self.mud.print(_("No text."))
            return
        await self._add_room_note(cmd)

    async def _add_room_note(self, txt, prefix=False):
        if self.room.note:
            self.room.note = txt + "\n" + self.room.note
            await self.mud.print(_("Note of {room.idn_str} extended.").format(room=self.room))
        else:
            self.room.note = txt
            await self.mud.print(_("Note of {room.idn_str} created.").format(room=self.room))
        self.db.commit()
        await self.show_room_note()


    @with_alias("rn*")
    @doc(_(
        """Prepend a line to the room's note."""))
    async def alias_rn_star(self, cmd):
        if not self.room:
            await self.mud.print(_("No active room."))
            return
        if not cmd:
            await self.mud.print(_("No text."))
            return
        await self._add_room_note(cmd, prefix=True)


    @doc(_(
        """Show/change the room's area/domain."""))
    async def alias_ra(self, cmd):
        cmd = self.cmdfix("w", cmd)
        if not self.room:
            await self.mud.print(_("No active room."))
            return
        if not cmd:
            if self.room.area:
                await self.mud.print(self.room.area.name)
            else:
                await self.mud.print(_("No area/domain set."))
            return

        area = await self.get_named_area(cmd[0], False)
        if self.room.info_area == area:
            pass
        elif self.room.orig_area != area:
            self.room.orig_area = self.room.area
        await self.room.set_area(area, force=True)
        self.db.commit()

    @with_alias("ra!")
    @doc(_(
        """Set the room to its 'other' area, or to a new named one"""))
    async def alias_ra_b(self, cmd):
        cmd = self.cmdfix("w", cmd)
        if cmd:
            cmd = cmd[0]
            area = await self.get_named_area(cmd, True)
        else:
            if self.room.orig_area and self.room.orig_area != self.room.area:
                area = self.room.orig_area
            elif self.room.info_area and self.room.info_area != self.room.area:
                area = self.room.info_area
            else:
                await self.mud.print(_("No alternate area/domain known."))
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
        await self.mud.print(_("Setting '{name}' {set}.").format(name=name, set=_('set') if self.conf[name] else _('cleared')))
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
            await self.mud.print(_("Setting '{name}' to {v}.").format(v=v, name=name))
            await self._save_conf(name)
        else:
            v = self.conf[name]
            await self.mud.print(_("Setting '{name}' is now {v}.").format(v=v, name=name))

    async def _conf_int(self, name, cmd):
        if cmd:
            v = int(cmd)
            self.conf[name] = v
            await self.mud.print(_("Setting '{name}' to {v}.").format(v=v, name=name))
            await self._save_conf(name)
        else:
            v = self.conf[name]
            await self.mud.print(_("Setting '{name}' is now {v}.").format(v=v, name=name))

    async def _save_conf(self, name):
        v = json.dumps(self.conf[name])
        await self.mud.setMapUserData("conf."+name, v)

    @with_alias("x=")
    @doc(_(
        """Remove an exit from a / the current room
        Usage: #x= ‹exit› ‹room›"""))
    async def alias_x_m(self, cmd):
        cmd = self.cmdfix("xr",cmd, min_words=1)
        room = self.room if len(cmd) < 2 else cmd[1]
        cmd = cmd[0]
        try:
            x = room.exit_at(cmd)
        except KeyError:
            await self.mud.print(_("Exit unknown."))
            return
        if (await room.set_exit(cmd.strip(), None))[1]:
            await self.update_room_color(room)
        await self.mud.updateMap()
        await self.mud.print(_("Removed."))

    @with_alias("x-")
    @doc(_(
        """Set an exit from a / the current room to 'unknown'
        Usage: #x- ‹exit› ‹room›"""))
    async def alias_x_m(self, cmd):
        cmd = self.cmdfix("xr",cmd, min_words=1)
        room = self.room if len(cmd) < 2 else cmd[1]
        cmd = cmd[0]
        try:
            x = room.exit_at(cmd)
        except KeyError:
            await self.mud.print(_("Exit unknown."))
            return
        if (await room.set_exit(cmd.strip(), False))[1]:
            await self.update_room_color(room)
        await self.mud.updateMap()
        await self.mud.print(_("Set to 'unknown'."))

    @with_alias("x+")
    @doc(_(
        """Add an exit from this room to some other / an unknown room.
        Usage: #x+ ‹exit› ‹room›
        """))
    async def alias_x_p(self, cmd):
        cmd = self.cmdfix("xr",cmd)
        if len(cmd) == 2:
            d,r = cmd
        else:
            d = cmd[0]
            r = True
        if (await self.room.set_exit(d, r))[1]:
            await self.update_room_color(self.room)
        await self.mud.updateMap()

    @doc(_(
        """Show exit details
        Either of a single exit (including steps if present), or all of them."""))
    async def alias_xs(self,cmd):
        cmd = self.cmdfix("x",cmd)
        def get_txt(x):
            txt = x.dir
            if x.cost != 1:
                txt += f" /{x.cost}"
            txt += ": "
            if x.dst:
                txt += x.dst.idnn_str
                if x.steps:
                    txt += " +"+str(len(x.moves))
            else:
                txt += _("‹unknown›")
            return txt

        if len(cmd) == 0:
            for x in self.room._exits:
                txt = get_txt(x)
                await self.mud.print(txt)
        else:
            try:
                x = self.room.exit_at(cmd[0])
            except KeyError:
                await self.mud.print(_("Exit unknown."))
                return
            await self.mud.print(get_txt(x))
            if x.steps:
                for m in x.moves:
                    await self.mud.print("… "+m)

    @doc(_(
        """
        Cost of walking through an exit
        Set the cost of using an exit.
        Params: exit cost [room]
        """))
    async def alias_xp(self, cmd):
        cmd = self.cmdfix("xir", cmd, min_words=2)
        if len(cmd) > 2:
            room = cmd[2]
        else:
            room = self.room
        x = room.exit_at(cmd[0])
        c = x.cost
        if c != cmd[1]:
            await self.mud.print(_("Cost of {exit.dir} from {room.idn_str} changed from {room.cost} to {cost}").format(room=room,exit=x,cost=cmd[1]))
            x.cost = cmd[1]
            self.db.commit()
        else:
            await self.mud.print(_("Cost of {exit.dir} from {room.idn_str} unchanged").format(room=room,exit=x))


    @doc(_(
        """
        Commands for an exit.
        Usage: #xc ‹exit› ‹whatever to send›
        "-": remove commands.
        Otherwise, add to the list of things to send.
        """))
    async def alias_xc(self, cmd):
        cmd = self.cmdfix("x*", cmd)
        if len(cmd) < 2:
            await self.mud.print(_("Missing arguments. You want '#xs'."))
        else:
            x = (await self.room.set_exit(cmd[0]))[0]
            if cmd[1] == "-":
                if x.steps:
                    await self.mud.print(_("Steps:"))
                    for m in x.moves:
                        await self.mud.print("… "+m)
                    x.steps = None
                    await self.mud.print(_("Deleted."))
                else:
                    await self.mud.print(_("This exit doesn't have specials."))
            else:
                x.steps = ((x.steps + "\n") if x.steps else "") + cmd[1]
                await self.mud.print(_("Added."))
            self.db.commit()

    @doc(_(
        """
        Prefix for the last move.
        Applied to the room you just came from!
        Usage: #xtp ‹whatever to send›
        Put in front of the list of things to send.
        If the list was empty, the exit name is added also.
        You can't 
        """))
    async def alias_xtp(self, cmd):
        cmd = self.cmdfix("*", cmd, min_words=1)
        if not self.last_room:
            await self.mud.print(_("I have no idea where you were."))
            return
        if not self.last_dir:
            await self.mud.print(_("I have no idea how you got here."))
            return
        x = self.last_room.exit_at(self.last_dir)
        if not x.steps:
            x.steps = self.last_dir
        x.steps = cmd[0] + "\n" + x.steps
        await self.mud.print(_("Prepended."))
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
            await self.mud.print(_("I have no idea where you were."))
            return
        if not self.last_dir:
            await self.mud.print(_("I have no idea how you got here."))
            return
        x = self.last_room.exit_at(self.last_dir)
        if not x.steps:
            x.steps = self.last_dir
        x.steps += "\n" + (cmd[0] if cmd else self.sent[0].command)
        await self.mud.print(_("Appended."))
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
            await self.mud.print(_("Exit name cleared."))
        else:
            self.named_exit = cmd[0]
            await self.mud.print(_("Exit name '{self.named_exit}' set.").format(self=self))

    @doc(_(
        """
        Timed exit
        Usage: you're in a room but know that you'll be swept to another
        one shortly.
        Use '#xn' to clear this.
        """))
    async def alias_xnt(self, cmd):
        cmd = self.cmdfix("", cmd)
        self.named_exit = ":time:"
        await self.mud.print(_("Timed exit prepared."))
    
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
            await self.mud.print(_("I don't know where I am or where I came from."))
            return

        try:
            x = self.last_room.exit_at(cmd)
        except KeyError:
            pass
        else:
            await self.mud.print(_("This exit already exists:"))
            await self.alias_xs(self,x.dir)
            return

        try:
            x = self.last_room.exit_to(self.room)
        except KeyError:
            await self.mud.print(_("{self.last_room.idn_str} doesn't have an exit to {self.room.idn_str}?").format(self=self))
            return
        if x.steps:
            await self.mud.print(_("This exit already has steps:"))
            await self.alias_xs(self,x.dir)
            return
        x.steps = x.dir
        x.dir = cmd
        self.db.commit()

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

        await self.mud.print("Start map sync")
        for r in db.q(db.Room).filter(db.Room.id_mudlet != None).all():
            print(r.idnn_str)
            s2 = r.id_mudlet // 2500
            if s != s2:
                s = s2
                await self.mud.print(_("… {room.id_mudlet}").format(room=r))
            m = await self.mud.getRoomCoordinates(r.id_mudlet)
            if m:
                x,y,z = await self.mud.getRoomCoordinates(r.id_mudlet, r.pos_x, r.pos_y, r.pos_z)
                if x or y:
                    r.pos_x,r.pos_y,r.pos_z = x,y,z
                x,y = await self.mud.getRoomNameOffset(r.id_mudlet)
                r.label_x,r.label_y = x,y
            else:
                await self.mud.addRoom(r.id_mudlet)
                await self.mud.setRoomCoordinates(r.id_mudlet, r.pos_x, r.pos_y, r.pos_z)
                # await self.mud.setRoomNameOffset(r.id_mudlet, r.label_x,r.label_y)

            if r.area_id:
                await self.mud.setRoomArea(r.id_mudlet, r.area_id)
            for x in r._exits:
                await r.set_mud_exit(x.dir, x.dst if x.dst_id and x.dst.id_mudlet else True)

            db.commit()


        await self.mud.print("Done with map sync")
        await self.mud.updateMap()
         
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
        p = self.mud.print
        cmd = self.cmdfix("i", cmd)
        if cmd:
            cmd = cmd[0]-1
        else:
            cmd = 0
        s = self.sent[cmd]
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
            await self.mud.print(_("I have no idea where you were."))
            return

        cmd = self.cmdfix("r", cmd)
        if len(cmd)>1:
            d = cmd[1]
        elif self.last_dir is not None:
            d=self.last_dir
        else:
            await self.mud.print(_("I have no idea how you got here."))
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
        elif self.room:
            await self.mud.centerview(self.room.id_mudlet)
        else:
            await self.mud.print(_("MAP: I have no idea where you are."))
            return
        await self.went_to_room(r, repair=True)

    @doc(_(
        """
        I moved.
        Use with nonstandard directions. (Standard directions should be automatic.)
        Usage: #mm ‹room› ‹dir›
        Leave room empty for new / known room. Direction defaults to last command.
        """))
    async def alias_mm(self, cmd):
        cmd = self.cmdfix("rx", cmd)

        d = cmd[1] if len(cmd) > 1 else self.sent[0].command
        if cmd and cmd[0]:
            await self.room.set_exit
            await self.went_to_room(cmd[0], d)
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
                await self.mud.print(_("I have no idea where you were."))
                return
            await self.mud.print(_("You went {d} from {last.idn_str}.").format(last=self.last_room,d=self.last_dir,room=self.room))
            return
        r = cmd[0] or await self.new_room("unknown")
        self.last_room = r
        if not cmd[0]:
            await self.mud.print(_("{last.idn_str} created.").format(last=self.last_room,d=self.last_dir,room=self.room))
        if len(cmd) > 1:
            self.last_dir = cmd[1]
            if (await r.set_exit(cmd[1],self.room or True))[1]:
                await self.update_room_color(r)
                if self.room:
                    await self.update_room_color(self.room)
        self.db.commit()
        if self.last_dir:
            await self.mud.print(_("You came from {last.idn_str} and went {d}.").format(last=self.last_room,d=self.last_dir))
        else:
            await self.mud.print(_("You came from {last.idn_str}.").format(last=self.last_room))



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
                await self.mud.print(_("I have no idea where you were."))
                return
        if this is None:
            this = False
        if (await prev.set_exit(d,this))[1]:
            await self.update_room_color(prev)
        self.db.commit()
        await self.mud.updateMap()
        await self.mud.print(_("Exit set."))

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
        d = ":time:"
        if prev is None:
            prev = self.last_room
            if prev is None:
                await self.mud.print(_("I have no idea where you were."))
                return
        if this is None:
            this = False
        if (await prev.set_exit(d,this))[1]:
            await self.update_room_color(prev)
        self.db.commit()
        await self.mud.updateMap()
        await self.mud.print(_("Timed exit set."))

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
            return None
        await self.gen_rooms(check)

    @doc(_(
        """
        Rooms without long descr

        Find routes to those rooms. Use #gg to use one.

        No parameters. Use the skip list to block ways to dangerous rooms.
        """))
    async def alias_mul(self, cmd):
        async def check(room):
            if room in self.skiplist:
                return SkipRoute
            if not room.long_descr:
                return SkipSignal
            return None
        await self.gen_rooms(check)

    @doc(_(
        """Mudlet rooms not in the database

        Find routes to those rooms. Use #gg to use one.

        No parameters."""))
    async def alias_mum(self, cmd):
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
            return None
        await self.gen_rooms(check)

    async def gen_rooms(self, checkfn, room=None, n_results=None):
        """
        Generate a room list. The check function is called with
        the room.

        checkfn may return any of the relevant control objects in
        mudlet.const, True for SkipSignal, or False for SkipRoute.

        If n_results is 1, walk the first path immediately.

        """
        # If a prev generator is running, kill it
        async def _check(d,r,h):
            res = await checkfn(r)
            if res is True:
                res = SkipSignal
            elif res is False:
                res = SkipRoute
            if res is SignalThis or res is SkipSignal:
                if n_results == 1:
                    await self.mud.print(_("{r.idnn_str} ({lh} steps)").format(r=r,lh=len(h),d=d+1))
                else:
                    await self.mud.print(_("#gg {d} : {r.idn_str} ({lh})").format(r=r,lh=len(h),d=d+1))
            return res

        await self.clear_gen()

        if room is None:
            room = self.walker.last_room if self.walker else self.room
        try:
            async with PathGenerator(self, self.room, _check, **({"n_results":n_results} if n_results else {})) as gen:
                self.path_gen = gen
                while await gen.wait_stalled():
                    await self.mud.print(_("Maybe more results: #gn"))
                if self.path_gen is gen:
                    if n_results == 1:
                        await self.clear_walk()
                        if not gen.results:
                            await self.mud.print(_("Cannot find a way there!"))
                        elif len(gen.results[0]) > 1:
                            self.walker = Walker(self, gen.results[0][1])
                        else:
                            await self.mud.print(_("Already there!"))
                        await self.clear_gen()
                        return
                    await self.mud.print(_("No more results."))
                # otherwise we've been cancelled
        finally:
            self.path_gen = None

    async def found_path(self, n, room, h):
        await self.mud.print(_("#gg {n} :d{lh} f{room.info_str}").format(room=room, n=n, lh=len(h)))

    def gen_next(self):
        evt, self._gen_next = self._gen_next, trio.Event()
        evt.set()

    @doc(_(
        """
        Path generator / walker status
        """))
    async def alias_gi(self, cmd):
        if self.walker:
            await self.mud.print("Walk: "+str(self.walker))
        else:
            await self.mud.print(_("No active walk."))
        if self.path_gen:
            await self.mud.print("Path: "+str(self.path_gen))
        else:
            await self.mud.print(_("No active path generator."))

    @doc(_(
        """
        Resume walking / generate more paths

        Parameter:
        If more paths, their number, default 3.
        If resume walking, skip this many rooms, default zero.
        """))
    async def alias_gn(self, cmd):
        cmd = self.cmdfix("i", cmd)
        if cmd and cmd[0] >= (0 if self.walker else 1):
            cmd = cmd[0]
        else:
            cmd = 0 if self.walker else 3
        if self.walker:
            self.walker.resume(cmd)
        elif self.path_gen:
            self.path_gen.make_more_results(cmd)
        else:
            await self.mud.print(_("No path generator is active."))

    @doc(_(
        """
        Use the first / a specific path which the generator produced
        No parameters: use the first result
        Otherwise: use the n'th result
        """))
    async def alias_gg(self, cmd):
        if self.path_gen is None:
            await self.mud.print(_("No route search active"))
            return
        if self.path_gen.results is None:
            if self.path_gen.is_running():
                await self.mud.print(_("No route search results yet"))
            else:
                await self.mud.print(_("No route search results. Sorry."))
            return
        cmd = self.cmdfix("i",cmd)
        if not cmd:
            self.walker = Walker(self, self.path_gen.results[0][1])
        elif cmd[0] <= len(self.path_gen.results):
            self.walker = Walker(self, self.path_gen.results[cmd[0]-1][1])
        else:
            await self.mud.print(_("I only have {lgr} results.").format(lgr=len(self.path_gen.results)))
            return

        self.add_start_room(self.room)
        await self.clear_gen()

    @doc(_(
        """
        Show details for generated paths
        No parameters: short details for all results
        Otherwise: complete list for all results
        """))
    async def alias_gv(self, cmd):
        if self.path_gen is None:
            await self.mud.print(_("No route search active"))
            return
        cmd = self.cmdfix("i",cmd)
        if cmd:
            dest, res = self.path_gen.results[cmd[0]-1]
            await self.mud.print(dest.info_str)
            prev = None
            for rid in res:
                room = self.db.r_old(rid)
                if prev is None:
                    d = _("Start")
                else:
                    d = prev.exit_to(room).dir
                prev = room
                await self.mud.print(f"{d}: {room.idnn_str}")
        else:
            i = 0
            if not self.path_gen.results:
                if self.path_gen.is_running():
                    await self.mud.print(_("No route search results yet"))
                else:
                    await self.mud.print(_("No route search results. Sorry."))
                return
            if self.room is None or self.room.id_old != self.path_gen.results[0][1][0]:
                room = self.db.r_old(self.path_gen.results[0][1][0])
                await self.mud.print(_("Start at {room.idnn_str}:"))
            for dest,res in self.path_gen.results:
                i += 1
                res = res[1:]
                if len(res) > 7:
                    res = res[:2]+[None]+res[-2:]
                res = (self.db.r_old(r).idn_str if r else "…" for r in res)
                await self.mud.print(f"{i}: {dest.idnn_str}")
                await self.mud.print("   "+" ".join(res))

    @doc(_(
        """
        Return to previous room
        No parameter: list the last ten rooms you started a speedwalk from.
        Otherwise, go to the N'th room in the list."""))
    async def alias_gr(self, cmd):
        cmd = self.cmdfix("i", cmd)
        if not cmd:
            if self.start_rooms:
                for n,r in enumerate(self.start_rooms):
                    await self.mud.print(_("{n}: {r.idn_str}").format(r=r, n=n+1))
            else:
                await self.mud.print(_("No rooms yet remembered."))
            return
        r = self.start_rooms[cmd[0]-1]
        await self.run_to_room(r)


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
            await self.mud.print(_("Room {room.id_str} is already on the list.").format(room=room))
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
        res = []
        rl = 0
        maxlen = 100
        if not self.skiplist:
            await self.mud.print(_("Skip list is empty."))
            return
        for r in self.skiplist:
            rn = r.idn_str
            if rl+len(rn) >= maxlen:
                await self.mud.print(" ".join(res))
                res = []
                rl = 0
            res.append(rn)
            rl += len(rn)+1
        if res:
            await self.mud.print(" ".join(res))

    @with_alias("gs+")
    @doc(_(
        """
        Add room the skiplist.
        No room given: use the current room.
        """))
    async def alias_gs_p(self,cmd):
        db = self.db
        cmd = self.cmdfix("r", cmd)
        room = cmd[0] if cmd else self.room
        if not room:
            await self.mud.print(_("No current room known"))
            return
        if room in self.skiplist:
            await self.mud.print(_("Room {room.id_str} already is on the list.").format(room=room))
            return
        self.skiplist.add(room)
        await self.mud.print(_("Room {room.idn_str} added.").format(room=room))
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
        if not cmd:
            cmd = self.room
            if not cmd:
                await self.mud.print(_("No current room known"))
                return
        room = cmd[0]
        try:
            self.skiplist.remove(room)
            db.commit()
        except KeyError:
            await self.mud.print(_("Room {room.id_str} is not on the list.").format(room=room))
        else:
            await self.mud.print(_("Room {room.id_str} removed.").format(room=room))

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
                sk = db.r_skiplist(cmd)
            except NoData:
                await self.mud.print(_("List {cmd} doesn't exist"))
            else:
                db.delete(sk)
                db.commit()

        else:
            self.skiplist = set()
            await self.mud.print(_("List cleared."))


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
                await self.mud.print(_("{sk.name}: {lsk} rooms").format(sk=sk, lsk=len(sk.rooms)))
            if not seen:
                await self.mud.print(_("No skiplists found"))
            return

        cmd = cmd[0]
        self.last_saved_skiplist = cmd
        sk = db.r_skiplist(cmd, create=True)
        for room in self.skiplist:
            sk.rooms.append(room)
        db.commit()
        await self.mud.print(_("skiplist '{cmd}' contains {lsk} rooms.").format(cmd=cmd, lsk=len(sk.rooms)))

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
                await self.mud.print(_("No last-saved skiplist."))
                return
        try:
            sk = db.r_skiplist(cmd)
            onr = len(sk.rooms)
            for room in sk.rooms:
                self.skiplist.add(room)
        except NoData:
            await self.mud.print(_("skiplist '{cmd}' not found.").format(cmd=cmd))
            return
        nr = len(self.skiplist)
        await self.mud.print(_("skiplist '{cmd}' merged, now {nr} rooms (+{nnr}).").format(nr=nr, cmd=cmd, nnr=nr-onr))



    @doc(_(
        """Go to labeled room
        Find the closest room(s) with that label.
        Routes will not go through rooms on the current skiplist,
        but they may end at a room that is.
        """))
    async def alias_gt(self, cmd):
        cmd = self.cmdfix("w",cmd)
        if not cmd:
            await self.mud.print(_("Usage: #gt Kneipe / Kirche / Laden"))
            return
        cmd = cmd[0].lower()

        async def check(r):
            if not r.id_mudlet:
                return SkipRoute
            if r.label and r.label.lower() == cmd:
                return SkipSignal
            i = await self.mud.getRoomUserData(r.id_mudlet, "type")
            if i and i[0] and i[0].lower() == cmd:
                return SkipSignal
            if r in self.skiplist:
                return SkipRoute
        await self.gen_rooms(check)


    @doc(_(
        """Go to a not-recently-visited room
        Find the closest room not visited recently.
        Routes will not go through rooms on the current skiplist,
        but they may end at a room that is.
        Adding a number uses that as the minimum counter.
        """))
    async def alias_mv(self, cmd):
        cmd = self.cmdfix("i",cmd)
        lim = cmd[0] if cmd else 1

        async def check(r):
            if not r.id_mudlet:
                return SkipRoute
            if r.last_visit is None or r.last_visit < lim:
                return SkipSignal
            if r in self.skiplist:
                return SkipRoute
        await self.gen_rooms(check, n_results=1)


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
            await self.mud.print(_("Usage: #gt Kneipe / Kirche / Laden"))
            return
        txt = cmd[0].lower()

        async def check(r):
            if not r.id_mudlet:
                return SkipRoute
            n = r.long_descr
            if n and txt in n.lower():
                return SkipSignal
            n = r.note
            if n and txt in n.lower():
                return SkipSignal
            if r in self.skiplist:
                return SkipRoute
        await self.gen_rooms(check)

    @doc(_(
        """Cancel path generation."""))
    async def clear_gen(self):
        if self.path_gen:
            await self.path_gen.cancel()
            self.path_gen = None

    @doc(_(
        """Cancel walking."""))
    async def clear_walk(self):
        if self.walker:
            await self.walker.cancel()
            self.walker = None

    @doc(_(
        """
        Cancel walking and/or path generation

        No parameters.
        """))
    async def alias_gc(self, cmd):
        await self.clear_gen()
        await self.clear_walk()

    @doc(_(
        """
        Exits (short)
        Print a one-line list of exits of the current / a given room"""))
    async def alias_x(self, cmd):
        cmd = self.cmdfix("i",cmd)
        if not cmd:
            room = self.room
        else:
            room = self.db.r_mudlet(cmd[0])
        await self.mud.print(room.exit_str)

    @doc(_(
        """
        Exits (long)
        Print a multi-line list of exits of the current / a given room"""))
    async def alias_xx(self, cmd):
        cmd = self.cmdfix("r",cmd)
        if not cmd:
            room = self.room
        else:
            room = cmd[0]
        exits = room.exits
        rl = max(len(x) for x in exits.keys())
        for d,dst in exits.items():
            d += " "*(rl-len(d))
            if dst is None:
                await self.mud.print(_("{d} - unknown").format(d=d))
            else:
                await self.mud.print(_("{d} = {dst.info_str}").format(dst=dst, d=d))


    @doc(_(
        """Detail info for current room / a specific room"""))
    async def alias_ri(self, cmd):
        cmd = self.cmdfix("r",cmd)
        room = (cmd[0] if cmd else None) or self.room
        if not room:
            await self.mud.print(_("No current room known!"))
            return
        await self.mud.print(room.info_str)
        if room.note:
            await self.mud.print(room.note)

    @doc(_(
        """Show rooms that have exits to this one"""))
    async def alias_rq(self, cmd):
        cmd = self.cmdfix("r",cmd)
        room = (cmd[0] if cmd else None) or self.room
        if not room:
            await self.mud.print(_("No current room known!"))
            return
        for x in room._r_exits:
            await self.mud.print(x.src.info_str)

    @doc(_(
        """Info for selected room(s) on the map"""))
    async def alias_rm(self, cmd):
        sel = await self.mud.getMapSelection()
        if not sel or not sel[0]:
            await self.mud.print(_("No room selected."))
            return
        sel = sel[0]
        for r in sel["rooms"]:
            room = self.db.r_mudlet(r)
            await self.mud.print(room.info_str)

    @with_alias("g#")
    @doc(_(
        """
        Walk to a mapped room.
        This overrides any other walk code
        Parameter: the room's ID.
        """))
    async def alias_g_h(self, cmd):
        cmd = self.cmdfix("r", cmd, min_words=1)
        dest = cmd[0].id_old
        await self.run_to_room(dest)

    async def run_to_room(self, room):
        """Run from the current room to the mentioned room."""
        if self.room is None:
            await self.mud.print(_("No current room known!"))
            return

        if isinstance(room,int):
            room = self.db.r_old(room)
        await self.clear_gen()
        await self.clear_walk()

        async def check(r):
            # our room
            mx = None
            if r == room:
                return SkipSignal
            # if r in self.skiplist:
            #     return SkipRoute
            if not r.id_mudlet:
                return SkipRoute
            return None

        self.add_start_room(self.room)
        await self.gen_rooms(check, n_results=1)

    @doc(_(
        """Recalculate colors
        A given room, or all of them"""))
    async def alias_mcr(self, cmd):
        db = self.db
        cmd = self.cmdfix("r", cmd)
        if len(cmd):
            room = self.room if cmd[0] is None else cmd[0]
            if room.id_mudlet:
                await self.update_room_color(room)
            return
        id_old = db.q(func.max(db.Room.id_old)).scalar()
        while id_old:
            try:
                room = db.r_old(id_old)
            except NoData:
                pass
            else:
                if room.id_mudlet:
                    await self.update_room_color(room)
            id_old -= 1
        await self.mud.updateMap()

    async def get_named_area(self, name, create=False):
        try:
            area = self._area_name2area[name.lower()]
            if create is True:
                raise ValueError(_("Area '{name}' already exists").format(name=name))
        except KeyError:
            # ask the MUD to make a new one
            if create is False:
                raise ValueError(_("Area '{name} does not exist").format(name=name))
            aid = await self.mud.addAreaName(name)
            area = self.db.Area(id=aid, name=name)
            db.add(area)
            db.commit()
        return area

    async def send_commands(self, *cmds, err_str=""):
        """
        Send this list of commands to the MUD.
        The list may include special processing or delays.
        """
        logger.debug("Locking sender for %r",cmds)
        async with self.send_command_lock:
            logger.debug("Locked sender")
            for d in cmds:
                if isinstance(d,Processor):
                    logger.debug("sender sends %r",d.command)
                    await self.send_command(d)
                elif isinstance(d,str):
                    if not d:
                        logger.debug("sender sends nothing")
                        continue
                    logger.debug("sender sends %r",d)
                    await self.send_command(d)
                elif isinstance(d,(int,float)): 
                    logger.debug("sender sleeps for %s",d)
                    await trio.sleep(d)         
                elif callable(d):
                    logger.debug("sender calls %r",d)
                    res = d()    
                    if iscoroutine(res):
                        logger.debug("sender waits for %r",d)
                        await res
                else:
                    logger.error("Dunno what to do with %r %s",d,err_str)
                    await self.mud.print(_("Dunno what to do with {d !r} {err_str}").format(err_str=err_str, d=d))
            logger.debug("Done sender")

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
            self.main.start_soon(self.mud.print,_("You really shouldn't send {msg!r}.").format(msg=msg))
            return None

        if not self.room:
            return msg
        if msg.startswith("#"):
            return None
        if msg.startswith("lua "):
            return None
        self.named_exit = None
        ms = short2loc(msg)
        try:
            x = self.room.exit_at(ms)
        except KeyError:
            return msg
        if x.steps:
            self.named_exit = msg
            self.main.start_soon(self.send_commands, *x.moves)
        else:
            return msg

    def maybe_close_descr(self):
        if self.long_descr is None:
            self.long_descr, self.long_lines = self.long_lines, []
    
    @doc(_(
        """
        Stored long description
        Print the current long descr from the database
        """))
    async def alias_rl(self, cmd):
        cmd = self.cmdfix("r", cmd)
        if cmd:
            room = cmd[0]
        else:
            room = self.room
        if room.long_descr:
            await self.mud.print(_("{room.info_str}:\n{room.long_descr}").format(room=room))
        else:
            await self.mud.print(_("No text known for {room.idnn_str}").format(room=room))
        if room.note:
            await self.mud.print(_("* Note:\n{room.note}").format(room=room))


    async def called_prompt(self, msg):
        """
        Called by Mudlet when the MUD sends a Telnet GA
        signalling readiness for the next line
        """
        logger.debug("NEXT")
        self.maybe_close_descr()
        await self.prompt()

    @doc(_(
        """Go-ahead, send next
        Use this command if the engine fails to recognize
        the MUD's prompt / Telnet GA message"""))
    async def alias_ga(self,cmd):
        await self.prompt()

    async def send_command(self, cmd, cls=Processor):
        """Send a single line to the MUD.
        If necessary, waits until a prompt / GA is received.
        """
        await self._prompt_r.receive()
        self.did_send(cmd, cls=cls)
        if isinstance(cmd,Processor):
            cmd = cmd.command
        self.last_command = cmd
        await self.mud.send(cmd)

    async def called_text(self, msg):
        """Incoming text from the MUD"""
        m = self.is_prompt(msg)
        if isinstance(m,str):
            msg = m
            await self.prompt()
            self.log_text_prompt()
            if len(msg) == 2:
                return
            msg = msg[2:]

#       if exit_pat.match(msg):
#           self.maybe_close_descr()
#       elif self.long_descr is None:
#           self.long_lines.apend(msg)

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

    def did_send(self, msg, cls=Processor):
        if not isinstance(msg, Processor):
            msg = cls(self, command=msg)
        self.sent.appendleft(msg)
        if len(self.sent) > 10:
            self.sent.pop()
        self.sent_new = True
        self.sent_prompt = True
        self.exit_match = None

    def match_exit(self, msg):
        """
        Match the "there is one exit" or "there are N exits" line which
        separates the room description from the things in the room.

        TODO: if there's no GMCP this may signal that you have moved.
        """
        def matched(g=None):
            if self.sent:
                self.sent[0].dir_seen(g)

        m = None
        if self.exit_match is None:
            m = EX3.search(msg)
            if not m:
                m = EX4.search(msg)
            if m:
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

        if self.sent:
            self.sent[0].add(msg)

    def log_text_prompt(self):
        if self.sent:
            self.sent[0].prompt_seen()

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

    async def prompt(self):
        self.last_command = None
        if self.sent_prompt:
            self.sent_prompt = False
        else:
            return

        if self.exit_match:
            self.main.start_soon(self.mud.print,_("WARNING: Exit matching failed halfway!"))
            self.exit_match = None
        try:
            self._prompt_s.send_nowait(None)
        except trio.WouldBlock:
            pass
        else:
            self.main.start_soon(self.sent[0].done)
            await self._to_text_watchers(None)

        # figure out whether we should run the "moved" code
        if not self.room:
            return
        if not self.sent:
            return
        if self.sent[0].got_info:
            # assume that the info-processing code already did that
            return
        self.main.start_soon(self._moved)

    async def _moved(self):
        # assume that we moved if there is an exits text,
        # or the destination's move-without-that flag is set
        s = self.sent[0]
        if s.got_info is not None:
            return
        s.got_info = False
        d = self.named_exit or s.command
        flag_no_exit = False
        if not s.exits_text:
            # check for flag
            try:
                x = self.room.exit_at(d)
            except KeyError:
                pass
            else:
                if x.dst and x.dst.flag & RF_no_exit:
                    flag_no_exit = True

        if flag_no_exit or s.exits_text:
            if not is_std_dir(d):
                try:
                    self.room.exit_at(d)
                except KeyError:
                    return
            await self.went_to_dir(d)
            if self.is_new_room:
                await self.process_exits_text(s.exits_text)
            # otherwise leave alone
            self.db.commit()


    async def process_exits_text(self, txt):
        xx = self.room.exits
        for x in self.exits_from_line(txt):
            if x not in xx:
                await self.room.set_exit(x)
                await self.mud.print(_("New exit: {exit}").format(exit=x,room=self.room))

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
        await self.gui_player()

    async def event_gmcp_MG_char_base(self, msg):
        logger.debug("BaseMG %s: %r",msg[1],msg[2])
        self.me.base = AD(msg[2])
        await self.gui_player()

    async def event_gmcp_MG_char_info(self, msg):
        logger.debug("InfoMG %s: %r",msg[1],msg[2])
        self.me.info = AD(msg[2])
        await self.gui_player()

    async def event_gmcp_MG_char_vitals(self, msg):
        logger.debug("VitalsMG %s: %r",msg[1],msg[2])
        self.me.vitals = AD(msg[2])
        await self.gui_vitals()

    async def event_gmcp_MG_char_maxvitals(self, msg):
        logger.debug("MaxVitalsMG %s: %r",msg[1],msg[2])
        self.me.maxvitals = AD(msg[2])
        await self.gui_vitals()

    # ### vitals ### #

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

    async def gui_vitals(self):
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


    async def event_sysWindowResizeEvent(self, msg):
        pass

    async def event_sysManualLocationSetEvent(self, msg):
        try:
            room = self.db.r_mudlet(msg[1])
        except NoData:
            if self.room and not self.room.id_mudlet:
                self.room.set_id_mudlet(msg[1])
                self.db.commit()
                await self.mud.print(_("MAP: Room ID is now {room.id_str}.").format(room=self.room))
            else:
                await self.mud.print(_("MAP: I do not know room ?/{id}.").format(id=msg[0]))
            room = None
        self.room = room
        self.next_word = None
        await self.show_room_data(room)

    async def event_sysDataSendRequest(self, msg):
        logger.debug("OUT : %s", msg[1])
        # We are sending data. Thus there won't be a prompt, thus we take
        # the prompt signal that might already be there out of the channel.
        if not self.last_command:
            pass
        elif self.last_command != msg[1]:
            await self.mud.print(_("WARNING last_command differs, stored {sc!r} vs seen {lc!r}").format(sc=self.last_command,lc=msg[1]))
            self.last_command = None
            return
        else:
            self.last_command = None
            return

        try:
            self._prompt_r.receive_nowait()
        except trio.WouldBlock:
            pass
        self.did_send(msg[1])

    async def event_gmcp_MG_room_info(self, msg):
        if len(msg) > 2:
            info = AD(msg[2])
        else:
            info = await self.mud.gmcp.MG.room.info
        if self.sent_new:
            moved = self.sent[0].command
            self.sent_new = False
        else:
            moved = None
        if self.sent:
            s = self.sent[0]
            if s.got_info is None:
                self.sent[0].got_info = info
            else:
                self.main.start_soon(self.mud.print,_("INFO arrived late.").format(msg=msg))
                # check for correct room
                if self.room is not None:
                    if (self.room.id_gmcp or "") != (s.got_info.get("id","") if s.got_info else ""):
                        self.main.start_soon(self.mud.print,_("INFO room ID conflict.").format(msg=msg))
                    else:
                        self.main.start_soon(self.mud.print,_("INFO arrived late.").format(msg=msg))
                        return
                else:
                    return
        await self.new_info(info, moved=moved)

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
            await self.mud.print(prefix+txt.rstrip("\n"))  # TODO color
        else:
            prefix = f"[{msg.chan}:{msg.player} "
            if txt.startswith(prefix) and txt.endswith("]"):
                txt = txt[len(prefix):-1]
            await self.mud.print(prefix+txt.rstrip("\n")+"]")  # TODO color

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
                    await self.mud.print(_("Collision: mudlet#{idm} but hash#{idh} with {hash!r}").format(idm=id_mudlet, idh=mid[0], hash=id_gmcp))
                    return

        if offset_from is None and id_mudlet is None:
            self.logger.warning("I don't know where to place the room!")
            await self.mud.print(_("I don't know where to place the room!"))

        room = self.db.Room(name=descr, id_gmcp=id_gmcp, id_mudlet=id_mudlet)
        self.db.add(room)
        self.db.commit()

        is_new = await self.maybe_assign_mudlet(room, id_mudlet)
        await self.maybe_place_room(room, offset_from,offset_dir, is_new=is_new)

        if area is None and offset_from is not None:
            area = offset_from.area
        if area is not None:
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
            await self.mud.print(_("Mudlet IDs inconsistent! old {room.idn_str}, new {idm}").format(room=room, idm=id_mudlet))
        if not room.id_mudlet:
            room.set_id_mudlet((await self.rpc(action="newroom"))[0])
            return True

    async def maybe_place_room(self, room, offset_from,offset_dir, is_new=False):
        """
        Place the room if it isn't already.
        """
        x,y,z = None,None,None
        if room.id_mudlet and not is_new:
            try:
                x,y,z = await self.mud.getRoomCoordinates(room.id_mudlet)
            except ValueError:
                # Huh. Room deleted there. Get a new room then.
                room.set_id_mudlet((await self.rpc(action="newroom"))[0])
            else:
                if x==0 and y==0 and z==0:
                    x=1

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
            await self.mud.print("INFO same and not moved")
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
            if moved is None and self.sent:
                moved = self.sent[0].command

        # check directions from our old room
        if self.named_exit:
            real_move = moved
            moved = short2loc(self.named_exit)
            self.named_exit = None
        else:
            real_move = None
            moved = short2loc(moved)

        if not room: # case 2: non-hashed or new room
            if not self.room:
                await self.mud.print(_("MAP: I have no idea where you are."))

            if not id_gmcp: # maybe moved
                if moved is None:
                    await self.mud.print(_("MAP: If you moved, say '#mn'."))
                    return
            
            room = self.room.exits.get(moved, None) if self.room else None
            id_mudlet = await self.mud.getRoomIDbyHash(id_gmcp)

            if id_mudlet and id_mudlet[0] > 0 and not self.conf['mudlet_gmcp']:
                # Ignore and delete this.
                id_mudlet = id_mudlet[0]
                await self.mud.setRoomIDbyHash(id_mudlet,"")
                id_mudlet = None

            if id_mudlet and id_mudlet[0] > 0:
                id_mudlet = id_mudlet[0]
            else:
                id_mudlet = (await self.room.mud_exits).get(moved, None) if self.room else None
            room2 = None
            if id_mudlet is not None:
                try:
                    room2 = self.db.r_mudlet(id_mudlet)
                except NoData:
                    room2 = None
            else:
                room2 = None
            if not room:
                room = room2
            elif room2 and room2.id_old != room.id_old:
                if self.room:
                    self.logger.warning("Conflict! From %s we went %s, old=%s mud=%s", self.room.id_str, moved, room.id_str, room2.id_str)
                await self.mud.print(_("MAP: Conflict! {room.id_str} vs. {room2.id_str}").format(room2=room2, room=room))
                self.room = None
                return
            if not room:
                room = await self.new_room(self.clean_shortname(info["short"]), id_gmcp=id_gmcp or None, id_mudlet=id_mudlet, offset_from=self.room if self.room else None, offset_dir=moved)
                is_new_room = True
            elif not room.id_mudlet:
                room.set_id_mudlet(id_mudlet)
            # id_old must always be set, it's a primary key

        if id_gmcp and not room.id_gmcp:
            room.id_gmcp = id_gmcp

        is_new = await self.maybe_assign_mudlet(room)
        await self.maybe_place_room(room, self.room,moved, is_new=is_new)

        for x in info["exits"]:
            rc = (await room.set_exit(x, True))[1] or rc
        if self.room and room.id_old != self.room.id_old:
            x = None
            # The idea is that if there already is an exit to this room, if
            # the current command is nonstandard then presumably it's a
            # manual component of the way in question, thus we leave it
            # alone.
            try:
                x = self.room.exit_to(room)
            except KeyError:
                pass
            else:
                if is_std_dir(moved):
                    x = None

            if x is None:
                x,_src = await self.room.set_exit(moved, room, force=False)
            else:
                _src = False
            if x.dst != room:
                await self.mud.print(_("""\
Exit {exit} points to {dst.idn_str}.
You're in {room.idn_str}.""").format(exit=x.dir,dst=x.dst,room=room))
            src |= _src

            if real_move and not x.steps:
                x.steps = real_move

            if self.conf['add_reverse']:
                rev = loc2rev(moved)
                if rev:
                    xr = room.exits.get(rev, NoData)
                    if xr is NoData:
                        await self.mud.print(_("An exit {dir} doesn't exist in {room.idn_str}!").format(room=room,dir=rev))
                    elif xr is not None and xr != self.room:
                        await self.mud.print(_("The exit {dir} already goes to {xr.idn_str}!").format(xr=xr, dir=rev))
                    else:
                        await room.set_exit(rev, self.room)
                else:
                    try:
                        self.room.exit_to(room)
                    except KeyError:
                        await self.mud.print(_("I don't know the reverse of {dir}, way back not set").format(dir=moved))
                    # else:
                    #     some exit to where we came from exists, so we don't complain

        short = self.clean_shortname(info.get("short",""))
        if short and room.id_mudlet and (is_new or is_new_room):
            await self.mud.setRoomName(room.id_mudlet, short)

        dom = info.get("domain","")
        area = (await self.get_named_area(dom, create=None)) if dom else self.room.area if self.room else (await self.get_named_area("Default", create=None))

        if room.area is None or self.conf['force_area']:
            if room.orig_area is None:
                room.orig_area = area
            if self.conf['use_mg_area'] or not self.room:
                await room.set_area(area, force=True)
            else:
                await room.set_area(self.room.area, force=True)
        room.info_area = area
        db.commit()
        logger.debug("ROOM:%s",room.info_str)
        if rc and room.id_mudlet:
            await self.update_room_color(room)
        if src and self.room.id_mudlet:
            await self.update_room_color(self.room)

        await self.went_to_room(room, moved, is_new=is_new_room)

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

    async def went_to_dir(self, d):
        if not self.room:
            await self.mud.print("No current room")
            return

        x,_ = await self.room.set_exit(d)
        is_new = False
        room = x.dst
        if room is None:
            try:
                rn = self.sent[0].lines[0][-1]
            except IndexError:
                rn = "unknown"
            room = await self.new_room(rn, offset_from=self.room, offset_dir=d)
            await self.room.set_exit(d, room)
            is_new = True
            self.db.commit()

        await self.went_to_room(room, d=d, is_new=is_new)

    async def went_to_room(self, room, d=None, repair=False, is_new=False):
        """You went to `room` using direction `d`."""
        db = self.db

        is_new = (await self.maybe_assign_mudlet(room)) or is_new
        await self.maybe_place_room(room, self.room,d, is_new=is_new)
        if room.area is None:
            await room.set_area(self.room.area)

        if not self.room or room.id_old != self.room.id_old:
            if not repair:
                self.last_room = self.room
            if d:
                self.last_dir = d
            self.room = room
            self.is_new_room = is_new
            self.next_word = None

        await self.mud.print(room.info_str)
        if room.id_mudlet is not None:
            room.pos_x,room.pos_y,room.pos_z = await self.mud.getRoomCoordinates(room.id_mudlet)
            db.commit()
        #await self.check_walk()
        nr,self._wait_move = self._wait_move, trio.Event()
        nr.set()
        await self.show_room_data()

        if not self.last_room:
            return

        try:
            p = self.sent[0]
            rn = ""
            if self.room_info:
                rn = self.room_info.get("short","")
            if not rn and p.current > Processor.P_BEFORE and p.lines[0]:
                rn = p.lines[0][-1]
            if rn:
                rn = self.clean_shortname(rn)
            if rn:
                self.last_shortname = rn
                if not room.name:
                    room.name = rn
                    db.commit()
                elif room.name == rn:
                    pass
                elif self.clean_shortname(room.name) == rn:
                    room.name = rn
                    db.commit()
                else:
                    await self.mud.print(_("Name: old: {room.name}").format(room=room,name=rn))
                    await self.mud.print(_("Name: new: {name}").format(room=room,name=rn))
                    await self.mud.print(_("Use '#rs' to update it"))

            if room.long_descr:
                for t in p.lines[p.P_AFTER]:
                    for tt in SPC.split(t):
                        room.has_thing(tt)
            else:
                self.main.start_soon(self.send_commands, LookProcessor(self, "schau"))
            room.visited()
            db.commit()


        except IndexError:
            if self.sent:
                raise
            pass

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
        if not cmd:
            cmd = self.last_shortname
            if not cmd:
                await self.mud.print("No shortname known")
                return
        if not self.room:
            await self.mud.print("No current room")
            return

        self.room.name = self.clean_shortname(cmd)
        await self.mud.setRoomName(self.room.id_mudlet, cmd)
        self.db.commit()

    async def show_room_data(self, room=None):
        if room is None:
            room = self.room
        if room.id_mudlet:
            await self.mud.centerview(room.id_mudlet)
        else:
            await self.mud.print("WARNING: you are in an unmapped room.")
        r,g,b = 30,30,30  # adapt for parallel worlds or whatever
        await self.mmud.GUI.ort_raum.echo(room.name)
        if room.area:
            await self.mmud.GUI.ort_region.echo(f"{room.area.name} [{room.id_str}]")
        else:
            await self.mmud.GUI.ort_region.echo(f"? [{room.id_str}]")
            r,g,b = 80,30,30

        await self.mmud.GUI.ort_raum.setColor(r, g, b)
        await self.mmud.GUI.ort_region.setColor(r, g, b)
        await self.show_room_label(room)
        await self.show_room_note(room)

    async def show_room_note(self, room=None):
        if room is None:
            room = self.room
            if room is None:
                return
        if room.note:
            note = room.note
            lf = note.find("\n")
            if lf > 1: note = note[:lf]
        else:
            note = "-"
        await self.mmud.GUI.raumnotizen.echo(note)

    async def show_room_label(self, room):
        label = room.label or "-"
        await self.mmud.GUI.raumtype.echo(label)

    async def gui_player(self):
        try: name = self.me.base.name
        except AttributeError: name = "?"
        try: level = self.me.info.level
        except AttributeError: level = "?"
        await self.mmud.GUI.spieler.echo(f"{name} [{level}]")


    async def walk_done(self, success:bool=True):
        if success:
            self.walker = None

    async def wait_moved(self):
        await self._wait_move.wait()



            #R=db.q(db.Room).filter(db.Room.id_old == 1).one()
            #R.id_mudlet = 142
            #db.commit()
        #import pdb;pdb.set_trace()
        pass
#
#        if mapfile:
#            m=yaml.safe_load(mapfile)
#            from ppr import pprint
#
#            m=m["ROOM"]
#            import pdb;pdb.set_trace()
#            for i,room in enumerate(m):
#                if "RSHORT" not in room: continue
#                sql = "SELECT `name` FROM `rooms` WHERE `id_old`=%s"
#                await cursor.execute(sql, (i,))
#                result = await cursor.fetchone()
#                if result is not None:
#                    continue
#                ex=[]
#                for k,v in room["EXITS"].items():
#                    v=int(v)
#                    if v:
#                        ex.append(_("{k}={v}").format(v=v, k=k))
#                    else:
#                        ex.append(_("{k}").format(k=k))
#
#                sql = "INSERT INTO `rooms` set id_old=%s,name=%s,long_descr=%s, note=%s, exits=%s,label=%s"
#                await cursor.execute(sql, (i,room["RSHORT"],"\n".join(room.get("RLONG",())),
#                    "\n".join(room.get("RNOTE",())),":".join(ex),
#                    ":".join(room.get("LABEL",{}).keys())))
#                await conn.commit()

    async def hello2(self, prompt,e):
        await trio.sleep(2)
        # from here you could again call mudlet
        e.set("Hello, "+prompt)

    def called_hello(self, prompt):
        e = ValueEvent()
        # do not call into Mudlet from here, you will deadlock
        self.main.start_soon(self.hello2,prompt,e)
        return e

    async def called_view_go(self, d):
        vr = self.view_room or self.room
        self.view_room = vr = vr.exits[d].dst
        if vr.id_mudlet:
            await self.mud.centerview(vr.id_mudlet)
        else:
            await self.mud.print(_("No mapped room to {d}").format(d=d))

    @doc(_(
        """Shift the view to the room in this direction"""))
    async def alias_vg(self, cmd):
        await self.called_view_go(cmd)

    @with_alias("v#")
    @doc(_(
        """Shift the view to this room"""))
    async def alias_v_h(self, cmd):
        await self.called_view_goto(int(cmd))

    async def called_view_reset(self):
        """Shift the view to the player's"""
        self.view_room = self.room
        await self.mud.centerview(self.room.id_mudlet)

    @doc(_(
        """Shift the view to the player's"""))
    async def alias_vr(self, cmd):
        await self.called_view_reset()

    async def called_view_goto(self, d):
        """Shift the view to this room"""
        vr = self.db.r_mudlet(d)
        self.view_room = vd
        await self.mud.centerview(vr.id_mudlet)

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

    # ### scan ### #

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
        print("\n\nADD "+txt+"\n")
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
            await self.mud.print(_("No active room."))
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
    Clear search
    Reset discovery so that all known words are scanned again.
    Starts with a global "look".
    """))
    async def alias_sc(self, cmd):
        cmd = self.cmdfix("", cmd)
        room = self.room
        if not room:
            await self.mud.print(_("No active room."))
            return
        self.room.reset_words()


    async def next_search(self, mark=False, again=True):
        if not self.room:
            await self.mud.print(_("No active room."))
            return

        if mark:
            if not self.next_word:
                await self.mud.print(_("No current word."))
                return
            if not self.next_word.flag:
                self.next_word.flag = 1
                self.db.commit()

        self.next_word = w = self.room.next_word()
        if w is not None:
            if w.word.name:
                await self.mud.print(_("Next search: {word}").format(word=w.word.name))
            else:
                await self.mud.print(_("Next search: ‹look›"))
            return

        w = self.room.with_word("", create=True)
        if w.flag:
            await self.mud.print(_("No (more) words."))
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
                await self.mud.print(_("No current word."))
                return
            alias,real = self.next_word.word,cmd[0]
        else:
            alias,real = db.word(cmd[0]), cmd[1]
        nw = self.room.with_word(alias.alias_for(real))
        if self.next_word.word == alias:
            self.next_word = nw
        db.commit()

        nw = self.next_word
        if nw.flag:
            await self.next_search()
        else:
            await self.mud.print(_("Next search: {word}").format(word=nw.word.name))
            

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
            await self.mud.print(_("Already ignored."))
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
            await self.mud.print(_("Already ignored."))
            return
        w.flag = db.WF_SKIP
        if wr is not None:
            # db.delete(wr)
            wr.flag = db.WF_SKIP
        db.commit()
        if not cmd:
            await self.next_search()

    # ### main code ### #

    async def run(self, db):
        logger.debug("connected")
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
#       async with self.events("*") as h:
#           async for msg in h:
#               print(msg)
#       async with self.events("gmcp.MG.room.info") as h:
#           async for msg in h:
#               info = await self.mud.gmcp.MG.room.info
#               print("ROOM",info)

@click.command()
@click.option("-c","--config", type=click.File("r"), help="Config file")
async def main(config):
    m={}
    if config is None:
        config = open("mapper.cfg","r")
    cfg = yaml.safe_load(config)
    cfg = combine_dict(cfg, DEFAULT_CFG, cls=attrdict)
    #logging.basicConfig(level=getattr(logging,cfg.log['level'].upper()))

    with SQL(cfg) as db:
        logger.debug("waiting for connection from Mudlet")
        async with S(cfg=cfg) as mud:
            await mud.run(db=db)

if __name__ == "__main__":
    main()
