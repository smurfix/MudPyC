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

from sqlalchemy.exc import IntegrityError

from .sql import SQL, NoData
from .const import SignalThis, SkipRoute, SkipSignal
from .const import ENV_OK,ENV_STD,ENV_SPECIAL,ENV_UNMAPPED
from .walking import Walker, PathGenerator

import gettext
_ = gettext.gettext

import logging
logger = logging.getLogger(__name__)

P_WAIT = 0
P_SEND = 1
P_WAITNEXT = 2
P_NEXT = 3

DEFAULT_CFG=attrdict(
        sql=attrdict(
            url='mysql://user:pass@server.example.com/morgengrauen'
            ),
        settings=attrdict(
            use_mg_area = False,
            force_area = False,
            add_reverse = True,
            dir_use_z = True,
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
    """Calculate an (un)likely offset for placing a new room.
    """
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

    async def setup(self, db):
        self.db = db
        db.setup(self)

        self.logger = logging.getLogger(self.cfg['name'])
        self.sent = deque()
        self.start_rooms = deque()
        self.room = None
        self.last_room = None
        self.last_dir = None
        self.room_info = None
        self.last_room_info = None
        self.named_exit = None

        #self.player_room = None
        self.walker = None
        self.path_gen = None
        self.prompted = P_NEXT # we hope
        self._prompt_s, self._prompt_r = trio.open_memory_channel(1)
        # assume that there's a prompt
        self._prompt_s.send_nowait(None)
        self.skiplist = set()
        self.last_saved_skiplist = None

        self._wait_move = trio.Event()

        self._area_name2area = {}
        self._area_id2area = {}

        await self.mud.sendGMCP("""Core.Supports.Debug 20""")
        await self.mud.sendGMCP("""Core.Supports.Add [ "MG.char 1", "MG.room 1", "comm.channel 1" ] """)
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

        self.alias.at("m").helptext = "Mapping"
        self.alias.at("mu").helptext = "Find unmapped rooms/exits"
        self.alias.at("g").helptext = "Walking, paths"
        self.alias.at("mc").helptext = "Map Colors"
        self.alias.at("r").helptext = "Rooms"
        self.alias.at("v").helptext = "View Map"
        self.alias.at("cf").helptext = "Change boolean settings"
        self.alias.at("co").helptext = "Room and name positioning"
    
    def _cmdfix_r(self,v):
        return self.db.r_mudlet(int(v))

    async def alias_c(self, cmd):
        """Configuration.
        Shows config data. No parameters.
        """
        if cmd:
            try:
                v = self.conf[cmd]
            except KeyError:
                await self.mud.print(_("Config item '{cmd}' unknown.").format(cmd=cmd))
            else:
                await self.mud.print(_("{cmd} = {v}.").format(v=v, cmd=cmd))

        else:
            for k,v in self.conf.items():
                await self.mud.print(_("{k} = {v}.").format(v=v, k=k))

    async def alias_cff(self, cmd):
        """Change setting for forcing newly visited rooms' areas
        to what 'use_mg_area' says."""
        await self._conf_flip("force_area")

    async def alias_cfm(self, cmd):
        """Use the MUD's area name?
        If False, use the last-visited room or 'Default'.
        """
        await self._conf_flip("use_mg_area")

    async def alias_ra(self, cmd):
        """Show/change the room's area/domain.
        """
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
    async def alias_ra_b(self, cmd):
        """Set the room to its 'other' area, or to a new named one"""
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

    async def alias_cfr(self, cmd):
        """When creating an exit, also link back?"""
        await self._conf_flip("add_reverse")

    async def alias_cfz(self, cmd):
        """Can new rooms be created in Z direction?"""
        await self._conf_flip("dir_use_z")

    async def _conf_flip(self, name):
        self.conf[name] = not self.conf[name]
        await self.mud.print(_("Setting '{name}' {set}.").format(name=name, set=_('set') if self.conf[name] else _('cleared')))
        await self._save_conf(name)

    async def alias_cox(self, cmd):
        """X shift for moving room labels (Ctrl-left/right)"""
        await self._conf_float("label_shift_x", cmd)

    async def alias_coy(self, cmd):
        """Y shift for moving room labels (Ctrl-Shift-left/right)"""
        await self._conf_float("label_shift_x", cmd)

    async def alias_cop(self, cmd):
        """X offset for placing rooms"""
        await self._conf_int("pos_x_delta", cmd)

    async def alias_coq(self, cmd):
        """Y offset for placing rooms"""
        await self._conf_int("pos_x_delta", cmd)

    async def alias_cor(self, cmd):
        """Diagonal offset for placing rooms (Z, nonstandard exits)"""
        await self._conf_int("pos_small_delta", cmd)

    async def _conf_float(self, name, cmd):
        if cmd:
            v = float(cmd)
            self.conf[name] = v
            await self.mud.print(_("Setting '{name}' to {v}.").format(v=v, name=name))
            await self._save_conf(name)
        else:
            v = self.conf[name]
            await self.mud.print(_("Setting '{name}' is {v}.").format(v=v, name=name))

    async def _conf_int(self, name, cmd):
        if cmd:
            v = int(cmd)
            self.conf[name] = v
            await self.mud.print(_("Setting '{name}' to {v}.").format(v=v, name=name))
            await self._save_conf(name)
        else:
            v = self.conf[name]
            await self.mud.print(_("Setting '{name}' is {v}.").format(v=v, name=name))

    async def _save_conf(self, name):
        v = json.dumps(self.conf[name])
        await self.mud.setMapUserData("conf."+name, v)

    @with_alias("x-")
    async def alias_x_m(self, cmd):
        """Remove an exit from a room"""
        cmd = self.cmdfix("w",cmd)
        if not cmd:
            await self.mud.print(_("Usage: #x- exitname"))
            return
        cmd = cmd[0]
        try:
            x = self.room.exit_at(cmd)
        except KeyError:
            await self.mud.print(_("Exit unknown."))
            return
        await self.room.set_exit(cmd.strip(), None)
        await self.mud.updateMap()
        await self.mud.print(_("Removed."))

    @with_alias("x+")
    async def alias_x_p(self, cmd):
        """Add an exit to a room."""
        cmd = self.cmdfix("wr",cmd)
        if len(cmd) == 2:
            d,r = cmd
        else:
            d = cmd[0]
            r = True
        await self.room.set_exit(d, r)
        await self.mud.updateMap()

    async def alias_xs(self,cmd):
        """Show an exit's details"""

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

    async def alias_xc(self, cmd):
        """Commands for an exit.
        Usage: #xc ‹exit› ‹whatever to send›
        "-": remove commands.
        Otherwise, add to the list of things to send.
        """
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

    async def alias_xn(self, cmd):
        """Prepare a named exit
        Usage: you have an interesting and/or existing exit but don't know
        which command triggers it.
        So you say "#xn pseudo_direction", then try any number of things,
        and when you do move the exit will be created with the name you
        give here and with the command you used to get there.

        "#xn" without a name will clear this.
        """
        cmd = self.cmdfix("w", cmd)
        if not cmd:
            self.named_exit = None
            await self.mud.print(_("Exit name cleared."))
        else:
            self.named_exit = cmd[0]
            await self.mud.print(_("Exit name '{self.named_exit}' set.").format(self=self))

    async def alias_xt(self, cmd):
        """Rename the exit just taken.
        The exit just taken, usually a command like "enter house",
        is renamed to whatever you say here. The exit is aliased to the old
        name. Thus "#xr house" will rename that exit to "house" and save
        "enter house" as the command to use.
        """
        cmd = self.cmdfix("w",cmd)
        if not cmd:
            await self.mud.print(_("Usage: #xt new_name"))
            return
        cmd = cmd[0]

        if not self.room or not self.last_room:
            await self.mud.print(_("I don't know where I am or where I came from."))
            return

        try:
            self.last_room.exit_at(cmd)
        except KeyError:
            pass
        else:
            await self.mud.print(_("This exit already exists:"))
            await self.alias_xs(self,x.dir)
            return

        try:
            x = self.last_room.exit_to(self.room)
        except KeyError:
            await self.mud.print(_("Room {self.last_room.id_str} doesn't have an exit to {self.room.id_str}?").format(self=self))
            return
        if x.steps:
            await self.mud.print(_("This exit already has steps:"))
            await self.alias_xs(self,x.dir)
            return
        x.steps = x.dir
        x.dir = cmd
        self.db.commit()

    async def alias_mn(self, cmd):
        """Fix last move
        You went to another room instead.
        Mention a room# to use that room, or leave empty to create a new room.
        """
        if not self.last_room:
            await self.mud.print(_("I have no idea where you were."))
            return
        if not self.last_room:
            await self.mud.print(_("I have no idea how you got here."))
            return
        cmd = self.cmdfix("r", cmd)
        if cmd:
            r = cmd[0]
        else:
            r = await self.new_room("unknown", offset_from=self.last_room, offset_dir=self.last_dir)
        x = self.last_room.exit_at(self.last_dir)
        x.dst = r
        self.db.comit()
        self.went_to_room(r, fix=True)

    async def alias_mud(self, cmd):
        """Database rooms not in Mudlet

        Find routes to those rooms. Use #gg to use one.

        No parameters.
        """
        async def check(room):
            # exits that are in Mudlet.
            mx = None
            for x in room.exits:
                if x.dst_id is not None:
                    continue
                if x.dst.id_mudlet is None:
                    return SignalThis
            return None
        self.gen_rooms(check)
        ...

    async def alias_mum(self, cmd):
        "Mudlet rooms not in the database"
        async def check(room):
            # These rooms are not in the database, so we check for unmapped
            # exits that are in Mudlet.
            mx = None
            for x in room.exits:
                if x.dst_id is not None:
                    continue
                if mx is None:
                    mx = await room.mud_exits
                if mx.get(x.dir, None) is not None:
                    return SignalThis
            return None
        self.gen_rooms(check)

    async def gen_rooms(self, checkfn, room=None):
        """
        Generate a room list. The check function is called with
        the room.

        checkfn may return any of the relevant control objects in
        mudlet.const, True for SkipSignal, or False for SkipRoute.

        """
        # If a prev generator is running, kill it
        async def _check(d,r,h):
            if r in self.skiplist:
                return None
            res = await checkfn(r)
            if res is True:
                res = SkipSignal
            elif res is False:
                res = SkipRoute
            if res is SignalThis or res is SkipSignal:
                await self.mud.print(_("#gu {d+1} : {r.id_str} {r.name} ({len(h)})").format(r=r))
            return res

        await self.clear_gen()

        if room is None:
            room = self.walker.last_room if self.walker else self.room
        try:
            async with PathGenerator(self, self.room, _check) as gen:
                self.path_gen = gen
                while await gen.wait_stalled():
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

    async def alias_gi(self, cmd):
        """
        Path generator / walker status
        """
        if self.walker:
            await self.mud.print("Walk: "+str(self.walker))
        else:
            await self.mud.print(_("No active walk."))
        if self.path_gen:
            await self.mud.print("Path: "+str(self.path_gen))
        else:
            await self.mud.print(_("No active path generator."))

    async def alias_gn(self, cmd):
        """
        Ask the path generator for another room or three.

        No parameters.
        """
        if self.path_gen:
            self.path_gen.make_more_results(3)
        else:
            await self.mud.print(_("No path generator is active."))

    async def alias_gu(self, cmd):
        """Use the first / a specific path which the generator produced
        No parameters: use the first result
        Otherwise: use the n'th result
        """
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
        elif cmd < len(self.path_gen.results):
            self.walker = Walker(self, self.path_gen.results[cmd[0]-1][1])
        else:
            await self.mud.print(_("I only have {lgr} results.").format(lgr=len(self.path_gen.results)))
            return

        self.clear_gen()

    async def alias_gr(self, cmd):
        """Return to prev room
        List the last ten rooms you started a speedwalk from"""
        cmd = self.cmdfix("i", cmd)
        if not cmd:
            if self.start_rooms:
                for n,r in enumerate(self.start_rooms):
                    await self.mud.print(_("{n}: {r.id_str} {r.name}").format(r=r, n=n+1))
            else:
                await self.mud.print(_("No rooms yet remembered."))
            return
        r = self.start_rooms[i-1]
        await self.run_to_room(r)


    @with_alias("gr+")
    async def alias_gr_p(self, cmd):
        """Add a room to the #gr list
        Remember the current (or a numbered) room for later walking-back-to
        """
        cmd = self.cmdfix("r", cmd)
        if cmd:
            cmd = cmd[0]
        else:
            cmd = self.room
        if cmd in self.start_rooms:
            await self.mud.print(_("Room {cmd.id_str} is already on the list.").format(cmd=cmd))
            return
        self.start_rooms.appendleft(cmd)
        if len(self.start_rooms) > 10:
            self.start_rooms.pop()


    async def alias_gs(self,cmd):
        """skiplist for searches.
        Rooms on the list are skipped while searching.
        """
        res = []
        rl = 0
        maxlen = 100
        if not self.skiplist:
            await self.mud.print(_("Skip list is empty."))
            return
        for r in self.skiplist:
            rn = _("{r.id_str}:{r.name}").format(r=r)
            if rl+len(rn) >= maxlen:
                await self.mud.print(" ".join(res))
                res = []
                rl = 0
            res.append(rn)
            rl += len(rn)+1
        if res:
            await self.mud.print(" ".join(res))

    @with_alias("gs+")
    async def alias_gs_p(self,cmd):
        """Add room the skiplist.
        No room given: use the current room.
        """
        db = self.db
        cmd = self.cmdfix("r", cmd)
        if not cmd:
            cmd = self.room
            if not cmd:
                await self.mud.print(_("No current room known"))
                return
        cmd = cmd[0]
        if cmd in self.skiplist:
            await self.mud.print(_("Room {cmd.id_str} already is on the list."))
            return
        self.skiplist.add(cmd)
        await self.mud.print(_("Room {cmd.id_str} added."))
        db.commit()

    @with_alias("gs-")
    async def alias_gs_m(self,cmd):
        """Remove room from the skiplist.
        No room given: use the current room.
        """
        db = self.db
        cmd = self.cmdfix("r", cmd)
        if not cmd:
            cmd = self.room
            if not cmd:
                await self.mud.print(_("No current room known"))
                return
        cmd = cmd[0]
        try:
            self.skiplist.remove(cmd)
            db.commit()
        except KeyError:
            await self.mud.print(_("Room {cmd.id_str} is not on the list."))
        else:
            await self.mud.print(_("Room {cmd.id_str} removed."))

    @with_alias("gs=")
    async def alias_gs_eq(self,cmd):
        """Forget a / clear the current skiplist.
        If a name is given, delete the named list, else clear the current
        in-memory list"""
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


    async def alias_gss(self,cmd):
        """Suspend the skiplist (by name)
        Stored lists are merged when one with the same name exists.
        No name given: list stored skiplists.
        """
        db = self.db
        cmd = self.cmdfix("w", cmd)
        if not cmd:
            seen = False
            for sk in db.q(db.Skiplist).all():
                seen = True
                await self.mud.print(_("{sk.name}: {lsk)} rooms").format(sk=sk, lsk=len(sk.rooms)))
            if not seen:
                await self.mud.print(_("No skiplists found"))
            return

        cmd = cmd[0]
        self.last_saved_skiplist = cmd
        sk = db.r_skiplist(cmd, create=True)
        for room in self.skiplist:
            sk.rooms.append(room)
        await self.mud.print(_("skiplist '{cmd}' contains {lsk} rooms.").format(cmd=cmd, lsk=len(sk.rooms)))
        db.commit()

    async def alias_gsr(self,cmd):
        """Restore the named skiplist by merging with the current list.
        No name given: merge/restore the last-saved list.
        """
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



    async def alias_gt(self, cmd):
        """Go to typed room"""
        cmd = self.cmdfix("w",cmd)
        if not cmd:
            await self.mud.print(_("Usage: #gt Kneipe / Kirche / Laden"))
            return
        cmd = cmd[0]

        async def check(r):
            if not r.id_mudlet:
                return SkipRoute
            i = await self.mud.getRoomUserData(r.id_mudlet, "type")
            if i and i[0] and i[0] == cmd:
                return SkipSignal
        await self.gen_rooms(check)

    async def clear_gen(self):
        """Cancel path generation."""
        if self.path_gen:
            self.path_gen.cancel()
            self.path_gen = None

    async def clear_walk(self):
        """Cancel walking."""
        if self.walker:
            self.walker.cancel()
            self.walker = None

    async def alias_gc(self, cmd):
        """
        Cancel walking and/or path generation

        No parameters.
        """
        self.clear_gen()
        self.clear_walk()

    async def alias_x(self, cmd):
        """Exits
        Print a one-line list of exits of the current / a given room"""
        cmd = self.cmdfix("i",cmd)
        if not cmd:
            room = self.room
        else:
            room = self.db.r_mudlet(cmd[0])
        await self.mud.print(room.exit_str)

    async def alias_xx(self, cmd):
        """Exits
        Print a multi-line list of exits of the current / a given room"""
        cmd = self.cmdfix("i",cmd)
        if not cmd:
            room = self.room
        else:
            room = self.db.r_mudlet(cmd[0])
        exits = room.exits
        rl = max(len(x) for x in exits.keys())
        for d,dst in exits:
            d += " "*(rl-len(d))
            if dst is None:
                await self.mud.print(_("{d} - unknown").format(d=d))
            else:
                await self.mud.print(_("{d} = {dst.info_str}").format(dst=dst, d=d))


    async def alias_rc(self, cmd):
        """Current Room"""
        await self.mud.print(self.room.info_str)

    async def alias_rs(self, cmd):
        """Selected Rooms"""
        sel = await self.mud.getMapSelection()
        if not sel or not sel[0]:
            await self.mud.print(_("No room selected."))
            return
        for r in sel["rooms"]:
            room = self.db.r_mudlet(r)
            await self.mud.print(room.info_str)

    @with_alias("g#")
    async def alias_g_h(self, cmd):
        """Walk to a mapped room.
        This overrides any other walk code
        Parameter: the room's ID.
        """
        dest = self.db.r_mudlet(int(cmd)).id_old

        await self.run_to_room(dest)

    async def run_to_room(self, room):
        """Run from the current room to the mentioned room."""
        if self.room is None:
            await self.mud.print(_("No current room known!"))
            return

        if not isinstance(room,int):
            room = room.id_old
        await self.clear_gen()
        await self.clear_walk()

        self.start_rooms.appendleft(self.room)
        if len(self.start_rooms) > 10:
            self.start_rooms.pop()

        async for h in self.room.reachable:
            r = h[-1]
            if r == dest:
                self.walker = Walker(self, h)
                break

    async def alias_mcr(self, cmd):
        """Recalculate colors"""
        db = self.db
        id_old = db.q(func.max(db.Room.id_old)).scalar()
        while id_old:
            try:
                room = db.r_old(id_old)
            except NoData:
                pass
            else:
                if room.id_mudlet:
                    await self.mud.setRoomEnv(room.id_mudlet, ENV_OK+room.open_exits)
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
        for d in cmds:
            if isinstance(d,str):
                if not d:
                    continue
                await self.send_command(d)
            elif isinstance(d,(int,float)): 
                await trio.sleep(d)         
            elif callable(d):
                res = d()    
                if iscoroutine(res):
                    await res
            else:
                logger.error("Dunno what to do with %r %s",d,err_str)
                await self.mud.print(_("Dunno what to do with {d !r} {err_str}").format(err_str=err_str, d=d))

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
        print(area_names)

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
                print("EXPLORE",r.id_old,r.name)
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
                    r.set_exit(d,None)
                    print("EXPLORE",r.id_old,r.name)
                    continue
                if nr.id_mudlet: continue
                mid = y.get(d,None)
                if not mid:
                    more.add((r.id_old,d))
                    continue
                # print(_("{nr.id_old} = {mid} {d}").format(nr=nr, d=d, mid=mid))
                try:
                    nr.id_mudlet = mid
                    db.commit()
                except IntegrityError:
                    # GAAH
                    db.rollback()

                    done.add(nr.id_old)
                    broken.add(nr.id_old)
                    xr = db.r_mudlet(mid)
                    print("BAD",nr.id_old,xr.id_old,"=",mid,nr.name)
                    xr.id_mudlet = None
                    xr.hash_mg = None
                    nr.hash_mg = None
                    broken.add(xr.id_old)
                    db.commit()
                else:
                    todo.appendleft(nr)

        print(more)

    @run_in_task
    async def called_input(self, msg):
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
            await self.send_commands(*x.moves)
        else:
            return msg

    def called_prompt(self, msg):
        print(_("NEXT"))
        self.prompted = P_NEXT
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

        print("IN  :", msg)

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
        print(msg)

    async def event_sysWindowResizeEvent(self, msg):
        pass

    async def event_sysManualLocationSetEvent(self, msg):
        room = self.db.r_mudlet(msg[0])
        if room is None:
            await self.mud.print(_("MAP: I do not know this room."))
        else:
            self.room = room

    async def event_sysDataSendRequest(self, msg):
        print("OUT :", msg[1])
        # We are sending data. Thus there won't be a prompt, thus we take
        # the prompt signal that might already be there out of the channel.
        try:
            self._prompt_r.receive_nowait()
        except trio.WouldBlock:
            pass
        self.prompted = P_WAITNEXT
        self.sent.appendleft(msg[1])
        if len(self.sent) > 10:
            self.sent.pop()

    async def event_gmcp_MG_room_info(self, msg):
        if len(msg) > 2:
            info = msg[2]
        else:
            info = await mud.mud.gmcp.MG.room.info
        await self.new_info(info)

    async def event_gmcp_comm_channel(self, msg):
        # don't do a thing
        msg = msg[2]
        chan = msg['chan']
        player = msg['player']
        prefix = "[{chan}:{player}] ".format(chan=chan, player=player)
        txt = msg["msg"]
        if txt.startswith(prefix):
            txt = msg["msg"].replace(prefix,"").replace("\n"," ").replace("  "," ").strip()
            print(_("CHAN:{chan} : {player}\n    :{txt}").format(txt=txt, player=player, chan=chan))
        else:
            prefix = "[{chan}:{player} ".format(chan=chan, player=player)
            if txt.startswith(prefix) and txt.endswith("]"):
                txt = txt[len(prefix):-1]
            print(_("CHAN:{chan} : {player} {txt}").format(txt=txt, player=player, chan=chan))

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
        if offset_from is None and id_mudlet is None:
            self.logger.warning("I don't know where to place the room!")
            return None

        x,y,z = None,None,None
        if id_mudlet:
            x,y,z = await self.mud.getRoomCoordinates(id_mudlet)
        else:
            id_mudlet = (await self.rpc(action="newroom"))[0]

        room = self.db.Room(name=descr, hash_mg=hash, id_mudlet=id_mudlet)
        self.db.add(room)
        self.db.commit()
        if x is None:
            await self.place_room(offset_from,offset_dir,room)

        else:
            room.pos_x, room.pos_y, room.pos_z = x,y,z
        if area is None and offset_from is not None:
            area = offset_from.area
        if area is not None:
            await room.set_area(area)

        self.db.commit()
        print("ROOM NEW:",room.id_old, room.id_mudlet)
        return room

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

        if info == self.room_info:
            return

        print(info)
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
            else:
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
                return

            if r_hash: # yes definitely a new room, thus we moved
                if moved is None and self.sent:
                    moved = self.sent[0]
            else: # maybe moved
                if moved is None:
                    await self.mud.print(_("MAP: If you moved, say '#mn'."))
                    return
            
            room = self.room.exits.get(moved, None)
            id_mudlet = (await self.room.mud_exits).get(moved, None)
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
                self.logger.warning("Conflict! From %s we went %s, old=%s mud=%s", self.room.id_str, moved, room.id_str, room2.id_str)
                await self.mud.print(_("MAP: Conflict! {room.id_str} vs. {room2.id_str}").format(room2=room2, room=room))
                self.room = None
                return
            if not room:
                room = await self.new_room(info["short"], id_mudlet=id_mudlet, offset_from=self.room, offset_dir=moved)
            elif not room.id_mudlet:
                room.id_mudlet = id_mudlet
            # id_old must always be set, it's a primary key

        if not room.hash_mg:
            room.hash_mg = r_hash
        if room.id_mudlet is None:
            room.id_mudlet = (await self.rpc(action="newroom"))[0]

        for x in info["exits"]:
            await room.set_exit(x, True)
        if self.room and room.id_old != self.room.id_old:
            if room.pos_x is None:
                await self.place_room(self.room, moved, room)

            x = await self.room.set_exit(moved, room)
            if real_move and not x.steps:
                x.steps = real_move

            if self.conf['add_reverse']:
                rev = loc2rev(moved)
                if rev:
                    await room.set_exit(rev, self.room)
                else:
                    await self.mud.print(_("Way back unknown, not set"))

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
        print(room.info_str)

        await self.went_to_room(room, moved)

    async def went_to_room(self, room, d=None, fix=False):
        if not self.room or room.id_old != self.room.id_old:
            if not fix:
                self.last_room = self.room
            if d:
                self.last_dir = d
            self.room = room

        await self.mud.print(room.info_str)
        await self.mud.centerview(room.id_mudlet)
        if room.id_mudlet is not None:
            room.pos_x,room.pos_y,room.pos_z = await self.mud.getRoomCoordinates(room.id_mudlet)
            self.db.commit()
        #await self.check_walk()
        nr,self._wait_move = self._wait_move, trio.Event()
        nr.set()

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

    async def alias_vg(self, cmd):
        """Shift the view to the room in this direction"""
        await self.called_view_go(cmd)

    @with_alias("v#")
    async def alias_v_h(self, cmd):
        """Shift the view to this room"""
        await self.called_view_goto(int(cmd))

    async def called_view_reset(self):
        """Shift the view to the player's"""
        self.view_room = self.room
        await self.mud.centerview(self.room.id_mudlet)

    async def alias_vr(self, cmd):
        """Shift the view to the player's"""
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
        print(_("connected"))
        await self.setup(db)

        info = await self.mud.gmcp.MG.room.info
        print(info)
        await self.new_info(info)

        #await mud.mud.centerview()

        async with self.event_monitor("*") as h:
            async for msg in h:
                await self.handle_event(msg['args'])
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
        print(_("waiting for connection from Mudlet"))
        async with S(cfg=cfg) as mud:
            await mud.run(db=db)

main()
