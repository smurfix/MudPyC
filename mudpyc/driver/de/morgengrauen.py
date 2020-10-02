from . import Driver as _Driver
from mudpyc.util import AD

import logging
logger = logging.getLogger(__name__)

class Driver(_Driver):
    """
    """
    name = "Morgengrauen"

    def init_std_dirs(self): 
        """
        Im Morgengrauen kann man stellenweise nach nordostunten gehen.
        """
        super().init_std_dirs()
        for a in "nord","sued","":
            for b in "ost","west","":
                for c in "ob","unt","":
                    self._std_dirs.add(a+b+c+"en")
        self._std_dirs.remove("en")  # :-)

    def init_reversal(self):
        """
        nordostunten … suedwestoben.
        """
        self._loc2rev = {}
        r=(("nord","sued"),("ost","west"),("oben","unten"))
        for x in self._std_dirs:
            y=x
            for a,b in r:
                if a in y:
                    y = y.replace(a,b)
                else:
                    y = y.replace(b,a)

            if x!=y and y in self._std_dirs:
                self._loc2rev[x] = y
                self._loc2rev[y] = x

    def gmcp_setup_data(self):
        """
        Iterates whatever GMCP settings the current MUD should use.

        Usage: call sendGMCP on whatever you get.
        """
        yield "Core.Supports.Debug", 20
        yield "Core.Supports.Set", [ "MG.char 1", "MG.room 1", "comm.channel 1" ]

    async def gmcp_initial(self, gmcp):
        s = self.server
        val = gmcp.get("MG", None)
        if not val:
            return False
        for x in "base info vitals maxvitals attributes".split():
            try: s.me[x] = AD(val['char'][x])
            except KeyError: pass
        await s.gui_show_player()

        try:
            id_gmcp = val["room"]["info"]["id"]
        except KeyError:
            pass
        else:
            if id_gmcp:
                try:
                    room = s.db.r_hash(id_gmcp)
                except NoData:
                    pass
                else:
                    await s.went_to_room(room)
        return True
