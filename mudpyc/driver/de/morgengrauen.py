from . import Driver as _Driver
from mudpyc.util import AD
from mudpyc.mapper.sql import NoData

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
                    room = await s.new_room(descr=val["room"]["info"]["short"], id_gmcp=id_gmcp)
                await s.went_to_room(room)
        return True

    async def event_gmcp_MG_char_attributes(self, msg):
        s = self.server
        logger.debug("AttrMG %s: %r",msg[1],msg[2])
        s.me.attributes = AD(msg[2])   
        await s.gui_show_player()

    async def event_gmcp_MG_char_base(self, msg):
        s = self.server
        logger.debug("BaseMG %s: %r",msg[1],msg[2])
        s.me.base = AD(msg[2])
        await s.gui_show_player()

    async def event_gmcp_MG_char_info(self, msg):
        s = self.server
        logger.debug("InfoMG %s: %r",msg[1],msg[2])
        s.me.info = AD(msg[2])
        await s.gui_show_player()

    async def event_gmcp_MG_char_vitals(self, msg):
        s = self.server
        logger.debug("VitalsMG %s: %r",msg[1],msg[2])
        s.me.vitals = AD(msg[2])
        await s.gui_show_vitals()

    async def event_gmcp_MG_char_maxvitals(self, msg):
        s = self.server
        logger.debug("MaxVitalsMG %s: %r",msg[1],msg[2])
        s.me.maxvitals = AD(msg[2])
        await s.gui_show_vitals()

    async def event_gmcp_MG_room_info(self, msg):   
        s = self.server
        if len(msg) > 2:
            info = AD(msg[2])
        else:
            info = await s.mud.gmcp.MG.room.info

        c = s.current_command(no_info=True)
        await c.set_info(info)
        s.maybe_trigger_sender()
       

