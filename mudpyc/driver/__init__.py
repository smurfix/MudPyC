# generic driver

import re
import weakref

from typing import NewType, Optional, Dict, Set, Tuple, Final, Union, List

import logging
logger = logging.getLogger(__name__)

from mudpyc.util import AD

LocalDir = NewType("LocalDir", str)
ShortDir = NewType("ShortDir", str)
IntlDir = NewType("IntlDir", str)


class ExitMatcher:
    """
    Helper which assembles exits.
    """
    M_INIT = [
        re.compile(r"^There are \S+ visible exits:(.*)$"),
        re.compile(r"^There is one visible exit:(.*)$"),
    ]
    M_NONE = [
        re.compile(r"^There is no visible exit.\s*"),
    ]

    M_JOIN_SEP = ", "
    M_JOIN_LAST = " and "
    M_END = re.compile(r"\.\s*$")

    finish_cb = None
    def set_finish_cb(self, cb):
        self.finish_cb = cb
        if self.last:
            cb()

    @classmethod
    def first_line(cls, msg, colors=None):
        """
        Returns an ExitMatcher if `msg` is the first line describing exits.
        """
        for r in cls.M_INIT:
            m = r.match(msg)
            if m:
                self = cls()
                self.line = m.group(1)
                if cls.M_END.search(msg):
                    self.finish()
                return self
        for r in cls.M_NONE:
            m = r.match(msg)
            if m:
                self = cls()
                self.finish()
                return self
        return None

    def finish(self) -> None:
        if self.last:
            raise RuntimeError("Finished twice")
        if self.line:
            self.exits = self.exits_from_line(self.line)
        self.last = True
        if self.finish_cb:
            self.finish_cb()

    def prompt(self) -> bool:
        """
        return False if the prompt arrived halfway in an Exit block.
        """
        if not self.last:
            self.finish()
            return False
        return True

    def match_line(self, msg, colors=None) -> bool:
        """
        Check if this line also contains exits.
        """
        if self.last:
            return False
        self.line += " " + msg
        if self.M_END.search(msg):
            self.finish()
        return True
        
    def exits_from_line(self, line) -> List[str]:     
        """
        Split the exit text into exit names
        """
        line = line.strip()
        line = line.rstrip(".")
        ui = line.find(self.M_JOIN_LAST)
        if ui < 0:
            return [ line ]
        else:
            res = line[:ui].split(self.M_JOIN_SEP)
            res.append(line[ui+len(self.M_JOIN_LAST):])
            return res

    def __init__(self):
        self.exits = []
        self.line = ""
        self.last = False

class Driver:
    name = "Generic driver"

    lang:str = None # should be overridden

    NAMELESS = "<nameless room>"
    LOOK = "look"
    EXAMINE = "examine"

    ExitMatcher = ExitMatcher

    # Used by `init_std_dirs` but Mudlet-specific so not modifyable
    _dir_intl: Final[Tuple[IntlDir, ...]] = \
            tuple("north south east west northeast northwest southeast southwest up down in out".split())

    # These data are used by `init_std_dirs` and may be overridden
    _dir_local: Tuple[LocalDir, ...] = _dir_intl
    _dir_short: Tuple[ShortDir, ...] = \
            tuple("n s e w ne nw se sw u d i o".split())
    _rev_pairs: Tuple[Tuple[LocalDir,LocalDir], ...] = \
            (("north","south"),("east","west"),("up","down"),("in","out"))

    # Reverse a direction. Filled by `init_std_dirs`.
    # May contain non-standard directions.
    _loc2rev: Dict[LocalDir,LocalDir] = None

    # Mappings between _dir_intl and _dir_local, filled in by `init_std_dirs`
    _loc2intl: Dict[LocalDir,IntlDir] = None
    _intl2loc: Dict[IntlDir,LocalDir] = None
    _short2loc: Dict[ShortDir,LocalDir] = None
    # cache
    _std_dirs: Set[Union[LocalDir,ShortDir]] = None
    _intl_dirs: Set[IntlDir] = None

    keymap = {
        0: {
            21:"southwest",
            22:"south",
            23:"southeast",
            26:"east",
            29:"northeast",
            28:"north",
            27:"northwest",
            24:"west",
            31:"up",     # minus
            30:"down",   # plus
            32:"leave",  # asterisk
            34:"enter",  # slash
            }
        }
    
    # WARNING you need to write your Lua setup code in such a way that it
    # can easily be re-executed without clearing or changing the current
    # state.
    lua_setup = ["""\
function initGUI()
    print("Note: you don't have any MUD specific Mudlet code")
end
"""]

    def init_mud(self):
        if False:
            yield ""

    async def show_room_label(self, room):
        """
        Update the current room's label in the GUI
        """
        pass

    async def show_room_note(self, room=None):
        """
        Update the current room's notes in the GUI
        """
        pass

    async def show_room_data(self, room=None):
        """
        Update the current room's data in the GUI.

        This should call `show_room_note` and `show_room_label` with the
        new room.
        """
        pass

    async def show_player(self):
        """
        Display the player's name and other attrs that don't change often
        """
        pass

    async def show_vitals(self):
        """
        Display the player's vitals (health etc)
        """
        pass

    def is_mudlet_dir(self, d: IntlDir) -> bool:
        """
        Return a flag whether this is a standard Mudlet direction.

        TODO: backend specific, except that we don't have multiple backends yet.
        """
        return d in self._intl_dirs

    def short2loc(self, d: str) -> LocalDir:
        """
        Translate a short direction, as understood by your MUD, to
        the equivalent long-hand version. E.g. "n" ⟶ "north".

        Short directions are *not* recognized by any other method in this class.

        Any unknown direction is returned unmodified.
        """
        return self._short2loc.get(d,d)

    def intl2loc(self, d: IntlDir) -> LocalDir:
        """
        Translate an international direction, as understood by Mudlet, to
        the name understood by the MUD you're talking to.

        This is the inverse of `intl2loc`.

        Any unknown direction is returned unmodified.
        """
        if not isinstance(d,str):
            import pdb;pdb.set_trace()
        return self._intl2loc.get(d,d)

    def loc2intl(self, d: LocalDir) -> IntlDir:
        """
        Translate a direction as understood by the MUD to the name required
        for Mudlet's 12 standardized directions.

        This is the inverse of `intl2loc`.

        Any unknown direction is returned unmodified.
        """
        return self._loc2intl.get(d,d)

    def loc2rev(self, d: LocalDir) -> Optional[LocalDir]:
        """
        Reverse a direction. Typically uses `_loc2rev` but might recognize
        other and/or non-reversible exits, i.e. "enter ‹anything›" might
        map to an unspecific "leave".

        Returns `None` if not reversible.
        """
        return self._loc2rev.get(d, None)

    def is_std_dir(self, d: LocalDir) -> bool:
        """
        Returns `True` if the direction is commonly understood by the MUD
        as some sort of movement.
        """
        return d in self._std_dirs

    def init_std_dirs(self):
        """
        Setup method to configure the standard directions.

        This method builds _std_dirs from _dir_local.
        """
        self._std_dirs = set(self._dir_local)

    def offset_delta(self, d: LocalDir):
        """
        When walking in direction X this function returns a 5-tuple in which
        direction the room is. Each item is either -1, 0, or 1. At least
        one item is != 0.

        dx: east/west
        dy: north/south
        dz: up/down
        ds: in/out
        dt: something else (never -1)
        """
        return self.offset_delta_intl(self.loc2intl(d), self.is_std_dir(d))

    def offset_delta_intl(self, d: IntlDir, is_std=False):
        """
        Default implementation for offset_delta

        This accommodates directions like "eastup" if the mud treats that
        as a normalized direction, thus prevents "enter pickup" being
        treated as something that involves going "up".
        """
        dx,dy,dz,ds,dt = 0,0,0,0,0
        if not is_std:
            dt = 1
        elif d == "in":
            ds = 1
        elif d == "out":
            ds = -1
        else:
            if "north" in d:
                dy = 1
            elif "south" in d:
                dy = -1
            if "east" in d:
                dx = 1
            elif "west" in d:
                dx = -1
            if "up" in d:
                dz = 1
            elif "down" in d:
                dz = -1
            if (dx,dy,dz) == (0,0,0):
                dt = 1
        return dx,dy,dz,ds,dt



    def init_short2loc(self):
        """
        Setup method to configure the standard directions.

        This method builds _short2loc from _dir_local and _dir_short.
        """
        self._short2loc = { s:l for s,l in zip(self._dir_short,self._dir_local) }

    def init_intl(self):
        """
        Setup method to configure local/international translation.

        This method builds _loc2intl and _intl2loc from _dir_local and _dir_intl.
        """
        self._loc2intl = {}
        self._intl2loc = {}
        for k,v in zip(self._dir_local, self._dir_intl):
            self._loc2intl[k] = v
            self._intl2loc[v] = k
        self._intl_dirs = set(self._dir_intl)

    def init_reversal(self):
        """
        Setup method to configure direction reversal.

        This method builds _loc2rev from _dir_local and _rev_pairs.

        Must be called after `init_intl`.
        """
        rp = self._rev_pairs
        if self._rev_pairs == Driver._rev_pairs and self._dir_local != self._dir_intl:
            # translated names but untranslated pairs
            rp = ((self.intl2loc(a),self.intl2loc(b)) for a,b in rp)
        self._loc2rev = {}
        for x,y in rp:
            self._loc2rev[x] = y
            self._loc2rev[y] = x

    def gmcp_setup_data(self):
        """
        Iterates whatever GMCP settings the current MUD should use.

        Usage: call sendGMCP on whatever you get.
        """
        yield "Core.Supports.Set", [ "Char 1", "Char.Skills 1", "Char.Items 1", "Room 1", "IRE.Rift 1", "IRE.Composer 1", "External.Discord 1", "Client.Media 1" ]

    def match_exits(self, msg, colors=None):
        """
        Returns an exits-matching object if this is the first line describing exits.
        """
        return self.ExitMatcher.first_line(msg, colors)

    def __init__(self, server):
        self._server = weakref.ref(server)
        self.init_std_dirs()
        self.init_short2loc()
        self.init_intl()
        self.init_reversal()
    
    @property
    def server(self):
        return self._server()

    async def gmcp_initial(self, gmcp):
        # extract "standard" GMCP data
        raise RuntimeError("TODO")

    async def event_gmcp_comm_channel(self, msg):
        s = self.server
        msg = AD(msg[2])  
        logger.debug("CHAN %r",msg)
        chan = msg.chan
        player = msg.player
        prefix = f"[{msg.chan}:{msg.player}] "
        txt = msg.msg.rstrip("\n")
        if txt.startswith(prefix):
            txt = txt.replace(prefix,"").replace("\n"," ").replace("  "," ").strip()
            txt = prefix+txt.rstrip("\n")
        else:
            prefix = f"[{msg.chan}:{msg.player} "
            if txt.startswith(prefix) and txt.endswith("]"):
                txt = txt[len(prefix):-1]
            txt = prefix+txt.rstrip("\n")+"]"
        await s.print(txt)  # TODO color
        await s.log_text(txt)

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
            return self.NAMELESS
        return name

