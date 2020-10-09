from ._lpmud import Driver as _Driver
from mudpyc.util import AD
from mudpyc.mapper.sql import NoData

import logging
logger = logging.getLogger(__name__)

class Driver(_Driver):
    """
    """
    name = "Midnight Sun 2"

    def gmcp_setup_data(self):
        """
        Iterates whatever GMCP settings the current MUD should use.

        Usage: call sendGMCP on whatever you get.
        """
        yield "Core.Supports.Debug", 20
        yield "Core.Supports.Set", [ "Char 1", "MG.room 1", "comm.channel 1" ]

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
       

# {
#   Char = {
#     Inv = {
#       action = "add",
#       short = "A shimmering orb",
#       kept = "0",
#       id = "219703",
#       used = "0"
#     },
#     Info = {
#       stats = {
#         str = "muscular",
#         con = "thin",
#         int = "feebleminded",
#         cha = "abhorrent",
#         dex = "stiff",
#         wis = "gullible"
#       },
#       name = "Smurfey",
#       lordroom = "",
#       level = "2",
#       title = "Novice",
#       guild = "Adventurer"
#     },
#     Wimpy = {
#       setting = "low",
#       direction = "0",
#       percentage = "18"
#     },
#     Align = {
#       value = -43
#     },
#     Vitals = {
#       Burden = "96"
#     },
#     Opponent = {
#       HP = 0,
#       short = ""
#     }
#   },
#   Map = {
#     Room = {
#       id = "5833",
#       props = {
#         "castle"
#       },
#       short = "Tower of Light",
#       verb = "up",
#       exits = {
#         up = 6306,
#         south = 6302,
#         west = 6294,
#         down = 5832,
#         east = 6296,
#         north = 6298
#       },
#       area = "Manetheren"
#     }
#   },
#   Channels = {
#     Message = [[
# \u001b[0m\u001b[0m\u001b[37m\u001b[40m[chat] Rev: so the Blacklist lost two main characters from 
# season 7, I wonder
#        how they are going to kill them off in season 8
# \u001b[0m\u001b[0m\u001b[37m\u001b[40m]],
#     Channel = "chat"
#   }
# }
