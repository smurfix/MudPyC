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

CFG_HELP=attrdict(
        use_mg_area=_("Use the MUD's area name"),
        force_area=_("Modify existing rooms' area when visiting them"),
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
def short2loc(x):

    return x

class S(Server):

    loc2itl=staticmethod(loc2itl)
    itl2loc=staticmethod(itl2loc)
    itl_names=set(_itl2loc.keys())
    loc_names=set(_loc2itl.keys())

    _input_grab = None

    async def setup(self, db):
        self.db = db
        db.setup(self)

        self.send_command_lock = trio.Lock()
        self.me = attrdict(blink_hp=None)

        self.logger = logging.getLogger(self.cfg['name'])
        self.sent = deque()
        self.start_rooms = deque()
        self.room = None
        self.last_room = None
        self.last_dir = None
        self.room_info = None
        self.last_room_info = None
        self.named_exit = None

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
        await self.set_long_mode(True)

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

        self.main.start_soon(self.sql_keepalive)

    async def set_long_mode(self, long_mode):
        if long_mode:
            ml = "lang"
        elif long_mode is False:
            ml = "kurz"
        else:
            ml = "ultrakurz"
        await self.send_command(ml)

    async def sql_keepalive(self):
        while True:
            await trio.sleep(600)
            self.db.db.execute("select 1")

    async def set_conf(self, k, v):
        if k not in self.conf:
            raise KeyError(k)
        self.conf[k] = v
        await self.mud.py.ext_conf._set(self.conf)

    async def sync_areas(self):
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

    def do_register_aliases(self):
        super().do_register_aliases()

        self.alias.at("m").helptext = _("Mapping")
        self.alias.at("mu").helptext = _("Find unmapped rooms/exits")
        self.alias.at("g").helptext = _("Walking, paths")
        self.alias.at("mc").helptext = _("Map Colors")
        self.alias.at("r").helptext = _("Rooms")
        self.alias.at("v").helptext = _("View Map")
        self.alias.at("cf").helptext = _("Change boolean settings")
        self.alias.at("co").helptext = _("Room and name positioning")
        self.alias.at("rl").helptext = _("Room's long description")
    
    def _cmdfix_r(self,v):
        v = int(v)
        if not v:
            return None
        if v < 0:
            return self.db.r_old(-v)
        else:
            return self.db.r_mudlet(v)

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
        id_mudlet = room.id_mudlet
        db.delete(room)
        db.commit()
        if id_mudlet:
            await self.mud.deleteRoom(id_mudlet)
            await self.mud.updateMap()

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
                await self.mud.print(_("Label of {room.idn_str} was {room.label}").format(room=self.room))
                self.room.label = cmd
            else:
                await self.mud.print(_("Label of {room.idn_str} not changed").format(room=self.room))
        self.db.commit()
        await self.show_room_label(self.room)

    @doc(_(
        """Show/change the room's note.

        The note is multi-line.
        No arguments: show the note.
        '-': delete the note
        '.': add to the note until a single dot is entered.
        '>': add the output sent by the previous command.
        Anything else is added to the note.
        """))
    async def alias_rn(self, cmd):
        cmd = self.cmdfix("*", cmd)
        if not self.room:
            await self.mud.print(_("No active room."))
            return
        if not cmd:
            if self.room.note:
                await self.mud.print(_("Note of {room.idn_str}:\n{room.note}").format(room=self.room))
            else:
                await self.mud.print(_("No note for {room.idn_str}.").format(room=self.room))
        else:
            cmd = cmd[0]
            if cmd == "-":
                if self.room.note:
                    await self.mud.print(_("Note of {room.idn_str} was:\n{room.note}").format(room=self.room))
                    self.room.note = None
                    self.db.commit()
            else:
                if cmd == ".":
                    cmd = ""
                    await self.mud.print(_("Extending note. End with '.'."))
                    async with self.input_grabber() as g:
                        async for line in g:
                            cmd += line+"\n"

                if self.room.note:
                    self.room.note += "\n" + cmd
                    await self.mud.print(_("Note of {room.idn_str} extended.").format(room=self.room))
                else:
                    self.room.note = cmd
                    await self.mud.print(_("Note of {room.idn_str} created.").format(room=self.room))
        self.db.commit()
        await self.show_room_note()



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

        cmd = cmd[0]
        area = await self.get_named_area(cmd, False)
        if self.room.info_area == area:
            pass
        elif self.room.orig_area != area:
            self.room.orig_area = self.room.area
        await self.room.set_area(area)
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

    @with_alias("x-")
    @doc(_(
        """Remove an exit from a / the current room
        Usage: #x- ‹exit› ‹room›"""))
    async def alias_x_m(self, cmd):
        cmd = self.cmdfix("wr",cmd, min_words=1)
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

    @with_alias("x+")
    @doc(_(
        """Add an exit from this room to some other / an unknown room.
        Usage: #x+ ‹exit› ‹room›
        """))
    async def alias_x_p(self, cmd):
        cmd = self.cmdfix("wr",cmd)
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

        cmd = self.cmdfix("w",cmd)
        if len(cmd) == 0:
            for x in self.room._exits:
                await self.mud.print(x.info_str)
        else:
            try:
                x = self.room.exit_at(cmd[0])
            except KeyError:
                await self.mud.print(_("Exit unknown."))
                return
            await self.mud.print(x.info_str)
            if x.steps:
                for m in x.moves:
                    await self.mud.print("… "+m)

    @doc(_(
        """
        Commands for an exit.
        Usage: #xc ‹exit› ‹whatever to send›
        "-": remove commands.
        Otherwise, add to the list of things to send.
        """))
    async def alias_xc(self, cmd):
        cmd = self.cmdfix("w*", cmd)
        if len(cmd) < 2:
            await self.mud.print(_("Missing arguments. You want '#xs'."))
        else:
            try:
                x = self.room.exit_at(cmd[0])
            except KeyError:
                await self.mud.print(_("Exit unknown."))
                return
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
        Prepare a named exit
        Usage: you have an interesting and/or existing exit but don't know
        which command triggers it.
        So you say "#xn pseudo_direction", then try any number of things,
        and when you do move the exit will be created with the name you
        give here and with the command you used to get there.

        "#xn" without a name will clear this.
        """))
    async def alias_xn(self, cmd):
        cmd = self.cmdfix("w", cmd)
        if not cmd:
            self.named_exit = None
            await self.mud.print(_("Exit name cleared."))
        else:
            self.named_exit = cmd[0]
            await self.mud.print(_("Exit name '{self.named_exit}' set.").format(self=self))

    @doc(_(
        """
        Rename the exit just taken.
        The exit just taken, usually a command like "enter house",
        is renamed to whatever you say here. The exit is aliased to the old
        name. Thus "#xr house" will rename that exit to "house" and save
        "enter house" as the command to use.
        """))
    async def alias_xt(self, cmd):
        cmd = self.cmdfix("w",cmd)
        if not cmd:
            await self.mud.print(_("Usage: #xt new_name"))
            return
        cmd = cmd[0]

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
        Fix last move
        You went to another room instead.
        Mention a room# to use that room, or zero to create a new room.
        Unlike #ms this command adds an exit, either explicitly (second
        word) or using the one you used.
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
        x = self.last_room.exit_at(self.last_dir)
        x.dst = r
        self.db.commit()
        self.went_to_room(r, repair=True)

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
            self.went_to_room(r)
        else:
            await self.mud.print(_("MAP: I have no idea where you are."))
            return
        await self.went_to_room(r, repair=True)

    @doc(_(
        """
        Set previous location
        You came from  here.
        Explicitly use zero to create a new room.
        """))
    async def alias_ml(self, cmd):
        cmd = self.cmdfix("rw", cmd)
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
        cmd = self.cmdfix("rwr", cmd, min_words=3)
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

        No parameters.
        """))
    async def alias_mud(self, cmd):
        async def check(room):
            # exits that are in Mudlet.
            mx = None
            if room in self.skiplist:
                return SkipRoute
            for x in room._exits:
                if x.dst_id is None:
                    continue
                if x.dst.id_mudlet is None:
                    return SkipSignal
            return None
        await self.gen_rooms(check)
        ...

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
                    mx = await room.mud_exits
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
                    await self.mud.print(_("#gu {d} : {r.idn_str} ({lh})").format(r=r,lh=len(h),d=d+1))
            return res

        await self.clear_gen()

        if room is None:
            room = self.walker.last_room if self.walker else self.room
        try:
            async with PathGenerator(self, self.room, _check, **({"n_results":n_results} if n_results else {})) as gen:
                self.path_gen = gen
                while await gen.wait_stalled():
                    if n_results == 1:
                        await self.clear_walker()
                        self.walker = Walker(self, self.path_gen.results[0][1])
                        await self.clear_gen()
                        return
                    await self.mud.print(_("Maybe more results: #gn"))
                if self.path_gen is gen:
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
    async def alias_gu(self, cmd):
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
        r = self.start_rooms[cmd-1]
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
        self.start_rooms.appendleft(cmd)
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
    async def alias_gl(self, cmd):
        cmd = self.cmdfix("w",cmd)
        if not cmd:
            await self.mud.print(_("Usage: #gt Kneipe / Kirche / Laden"))
            return
        cmd = cmd[0].lower()

        async def check(r):
            if not r.id_mudlet:
                return SkipRoute
            i = await self.mud.getRoomUserData(r.id_mudlet, "type")
            if i and i[0] and i[0].lower() == cmd:
                return SkipSignal
            if r in self.skiplist:
                return SkipRoute
        await self.gen_rooms(check)


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
        cmd = cmd[0].lower()

        async def check(r):
            if not r.id_mudlet:
                return SkipRoute
            n = r.longdescr
            if n and n.lower().contains(cmd):
                return SkipSignal
            n = r.note
            if n and n.lower().contains(cmd):
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

    async def alias_gd(self, cmd):
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
        """Info for selected rooms (on the map)"""))
    async def alias_rs(self, cmd):
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
            if r in self.skiplist:
                return SkipRoute
            if not r.id_mudlet:
                return SkipRoute
            return None

        self.start_rooms.appendleft(self.room)
        if len(self.start_rooms) > 10:
            self.start_rooms.pop()

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
            area = db.Area(id=aid, name=name)
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
                if isinstance(d,str):
                    logger.debug("sender sends nothing")
                    if not d:
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
        area_names = {int(k):v for k,v in (await mud.mud.getAreaTableSwap())[0].items()}
        area_known = set()
        area_rev = {}
        for k,v in area_names.items:
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
            if not r.hash_mg:
                hash_mg = (await mud.mud.getRoomHashByID(r.id_mudlet))[0]
                if hash_mg:
                    r.hash_mg = hash_mg
                    db.commit()

            if r.area_id is None:
                ra = (await mud.mud.getRoomArea(r.id_mudlet))[0]
                know_area(ra)
                r.area_id = ra


            try:
                y = await r.mud_exits(mud)
            except NoData:
                # didn't go there yet
                logger.debug("EXPLORE %s %s",r.id_str,r.name)
                explore.add(r.id_old)
                continue
            # print(r.id_old,r.id_mudlet,r.name,":".join(f"{k}={v}" for k,v in y.items()), v=v, k=k)
            for d,nr_id in x.items():
                nr_id = x[d]
                if not nr_id: continue
                if nr_id in done: continue
                if nr_id in broken: continue
                nr = db.r_old(nr_id)
                if nr is None:
                    # missing data?
                    await r.set_exit(d,None)
                    logger.debug("EXPLORE %s %s",r.id_str,r.name)
                    await self.update_room_color(r)
                    continue
                if nr.id_mudlet: continue
                mid = y.get(d,None)
                if not mid:
                    more.add((r.id_old,d))
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
                    xr.hash_mg = None
                    nr.hash_mg = None
                    broken.add(xr.id_old)
                    db.commit()
                else:
                    todo.appendleft(nr)

        logger.debug("%r",more)

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
            if self._input_grab is send:
                self._input_grab = None
                await w.aclose()
                # otherwise done above

    async def called_input(self, msg):
        if self._input_grab:
            await self._input_grab(msg)
            return None
        if not self.room:
            return msg
        if msg.startswith("#"):
            return None
        if msg.startswith("lua "):
            return None
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
        """Store the current long description"""))
    @with_alias("rl!")
    async def alias_rl_b(self, cmd):
        ...

    def called_prompt(self, msg):
        logger.debug("NEXT")
        self.maybe_close_descr()
        try:
            self._prompt_s.send_nowait(None)
        except trio.WouldBlock:
            pass

    async def alias_gp(self,cmd):
        try:
            self._prompt_s.send_nowait(None)
        except trio.WouldBlock:
            pass

    async def send_command(self, cmd):
        """Send a single line."""
        await self._prompt_r.receive()
        await self.mud.send(cmd)

    async def called_text(self, msg):
        if msg.startswith("> "):
            await self.prompt()
            if len(msg) == 2:
                return
            msg = msg[2:]

        if msg.startswith("Der GameDriver teilt Dir mit:"):
            return

#       if exit_pat.match(msg):
#           self.maybe_close_descr()
#       elif self.long_descr is None:
#           self.long_lines.apend(msg)

        logger.debug("IN  : %s", msg)

    async def prompt(self):
        pass  # use only in MUDs that don't send GA

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
        await self.show_room_data(room)

    async def event_sysDataSendRequest(self, msg):
        logger.debug("OUT : %s", msg[1])
        # We are sending data. Thus there won't be a prompt, thus we take
        # the prompt signal that might already be there out of the channel.
        try:
            self._prompt_r.receive_nowait()
        except trio.WouldBlock:
            pass
        self.sent.appendleft(msg[1])
        if len(self.sent) > 10:
            self.sent.pop()

    async def event_gmcp_MG_room_info(self, msg):
        if len(msg) > 2:
            info = AD(msg[2])
        else:
            info = await mud.mud.gmcp.MG.room.info
        await self.new_info(info)

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

    async def new_room(self, descr, hash=None, id_mudlet=None, offset_from=None,
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
        if hash:
            try:
                room = self.db.r_hash(hash)
            except NoData:
                pass
            else:
                self.logger.error(_("New room? but we know hash {hash} at {room.id_old}").format(room=room, hash=hash))
                return None
            mid = await self.mud.getRoomIDbyHash(hash)
            if mid and mid[0] and mid[0] > 0:
                if id_mudlet is None:
                    id_mudlet = mid[0]
                elif id_mudlet != mid[0]:
                    await self.mud.print(_("Collision: mudlet#{idm} but hash#{idh} with {hash!r}").format(idm=id_mudlet, idh=mid[0], hash=hash))
                    return

        if offset_from is None and id_mudlet is None:
            self.logger.warning("I don't know where to place the room!")
            await self.mud.print(_("I don't know where to place the room!"))

        room = self.db.Room(name=descr, hash_mg=hash, id_mudlet=id_mudlet)
        self.db.add(room)
        self.db.commit()

        await self.maybe_assign_mudlet(room, id_mudlet)
        await self.maybe_place_room(room, offset_from,offset_dir, is_new=True)

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

    async def new_info(self, info, moved=None):
        db=self.db
        rc = False
        src = False

        if info == self.room_info:
            return

        logger.debug("INFO:%r",info)
        if self.conf['debug_new_room']:
            import pdb;pdb.set_trace()

        self.last_room_info = self.room_info
        self.room_info = info

        r_hash = info.get("id", None)
        if r_hash and self.room and self.room.hash_mg == r_hash:
            return

        # Case 1: we have a hash.
        room = None

        if r_hash:
            try:
                room = db.r_hash(r_hash)
            except NoData:
                pass
            if moved is None and self.sent:
                moved = self.sent[0]

        # check directions from our old room
        if self.named_exit:
            real_move = moved
            moved = self.named_exit
            self.named_exit = None
        else:
            real_move = None
            moved = short2loc(moved)

        if not room: # case 2: non-hashed or new room
            if not self.room:
                await self.mud.print(_("MAP: I have no idea where you are."))

            if not r_hash: # maybe moved
                if moved is None:
                    await self.mud.print(_("MAP: If you moved, say '#mn'."))
                    return
            
            room = self.room.exits.get(moved, None) if self.room else None
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
                room = await self.new_room(info["short"], hash=r_hash, id_mudlet=id_mudlet, offset_from=self.room if self.room else None, offset_dir=moved)
            elif not room.id_mudlet:
                room.set_id_mudlet(id_mudlet)
            # id_old must always be set, it's a primary key

        if not room.hash_mg:
            room.hash_mg = r_hash

        is_new = await self.maybe_assign_mudlet(room)
        await self.maybe_place_room(room, self.room,moved, is_new=is_new)

        for x in info["exits"]:
            rc = (await room.set_exit(x, True))[1] or rc
        if self.room and room.id_old != self.room.id_old:
            x,_src = await self.room.set_exit(moved, room)
            src |= _src

            if real_move and not x.steps:
                x.steps = real_move

            if self.conf['add_reverse']:
                rev = loc2rev(moved)
                if rev:
                    xr = room.exits.get(rev, None)
                    if xr is None:
                        # rc = (await room.set_exit(rev, self.room))[1] or rc
                        await self.mud.print(_("An exit {dir} doesn't exist in {room.idn_str}!").format(room=room,dir=rev))
                    elif xr != self.room:
                        await self.mud.print(_("The exit {dir} already goes to {xr.idn_str}!").format(xr=xr, dir=rev))
                else:
                    try:
                        self.room.exit_to(room)
                    except KeyError:
                        await self.mud.print(_("I don't know the reverse of {dir}, way back not set").format(dir=moved))
                    # else: some exit to where we came from exists, so we don't complain

        short = info.get("short","").rstrip(".")
        if short and room.id_mudlet:
            await self.mud.setRoomName(room.id_mudlet, short)

        dom = info.get("domain","")
        area = (await self.get_named_area(dom, create=None)) if dom else self.room.area if self.room else (await self.get_named_area("Default", create=None))
        if room.area is None or self.conf['force_area']:
            if room.orig_area is None:
                room.orig_area = area
            if self.conf['use_mg_area'] or not self.room:
                await room.set_area(area)
            else:
                await room.set_area(self.room.area)
        room.info_area = area
        db.commit()
        logger.debug("ROOM:%s",room.info_str)
        if rc and room.id_mudlet:
            await self.update_room_color(room)
        if src and self.room.id_mudlet:
            await self.update_room_color(self.room)

        await self.went_to_room(room, moved)

    async def went_to_room(self, room, d=None, repair=False):
        """You went to `room` using direction `d`."""
        if not self.room or room.id_old != self.room.id_old:
            if not repair:
                self.last_room = self.room
            if d:
                self.last_dir = d
            self.room = room

        await self.mud.print(room.info_str)
        if room.id_mudlet is not None:
            room.pos_x,room.pos_y,room.pos_z = await self.mud.getRoomCoordinates(room.id_mudlet)
            self.db.commit()
        #await self.check_walk()
        nr,self._wait_move = self._wait_move, trio.Event()
        nr.set()
        await self.show_room_data()

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
        await self.send_commands("lang","schau")
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
        dx *= self.conf['label_shift_x']
        dy *= self.conf['label_shift_y']
        msg = await self.mud.getMapSelection()
        if not msg or not msg[0]:
            rooms = await self.mud.getPlayerRoom()
        else:
            rooms = msg[0]['rooms']
        for r in rooms:
            x,y = await self.mud.getRoomNameOffset(r)
            x += dx; y += dy
            await self.mud.setRoomNameOffset(r, x, y)
        await self.mud.updateMap()

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

        #await mud.mud.centerview()

        try:
            async with self.event_monitor("*") as h:
                async for msg in h:
                    await self.handle_event(msg['args'])
        except Exception as exc:
            raise
        except BaseException as exc:
            logger.exception("END")
            raise
        finally:
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
    logging.basicConfig(level=getattr(logging,cfg.log['level'].upper()))

    with SQL(cfg) as db:
        logger.debug("waiting for connection from Mudlet")
        async with S(cfg=cfg) as mud:
            await mud.run(db=db)

main()
