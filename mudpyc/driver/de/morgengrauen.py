import trio

from . import Driver as _Driver
from mudpyc.util import AD
from mudpyc.mapper.sql import NoData

import logging
logger = logging.getLogger(__name__)

class Driver(_Driver):
    """
    """
    name = "Morgengrauen"

    lua_setup = [
"""
-- thanks to krrrcks for the original I cloned this from

GUI = GUI or {}
GUI.angezeigt = GUI.angezeigt or false
GUI.lp_anzeige_blinkt = GUI.lp_anzeige_blinkt or false

function initGUI()

  -- already set up?
  if GUI.mainbox then return end

  -- Textfenster begrenzen
  setBorderTop(0)
  setBorderBottom(90) -- bisschen Platz fuer Statuszeile
  setBorderLeft(0)
  setBorderRight(0)

  -- Statuszeile malen. Layout wie folgt:
  -- Zeile 1: spieler (Name, Stufe), gift, trenner_1, vorsicht (Vorsicht, Fluchtrichtung)
  -- Zeile 2: ort_raum (Region, Raumnummer, Para), ort_region (Ort kurz)
  -- Zeile 3: lp_titel, lp_anzeige (Lebenspunkte-Anzeige), kp_titel, kp_anzeige (KP-Anzeige), trenner_2
  local HBox = Geyser.HBox:new({
      name="HBox",
      x=0, y=-90,
      width="100%", height=70,
  })
  GUI.mainbox = HBox
  
  local VBox = Geyser.VBox:new({
      name="VBox", width="100%", h_policy=Geyser.Fixed
  }, HBox)

  
  Zeile = Geyser.HBox:new({name="HBox_line4"}, VBox)
  GUI.raumnotizen = Geyser.Label:new({name = "raumnotizen"}, Zeile)
  GUI.raumtype = Geyser.Label:new({name = "raumtype"}, Zeile)
  
  -- Zeile 1
  Zeile = Geyser.HBox:new({name="HBox_line1"}, VBox)
  GUI.spieler = Geyser.Label:new({name = "spieler"}, Zeile)
  GUI.gift = Geyser.Label:new({name = "gift"}, Zeile)
  GUI.trenner_1 = Geyser.Label:new({name = "trenner_1"}, Zeile)
  GUI.vorsicht = Geyser.Label:new({name = "vorsicht"}, Zeile)

  -- Zeile 2  
  Zeile = Geyser.HBox:new({name="HBox_line2"}, VBox)
  GUI.ort_raum = Geyser.Label:new({name = "ort_raum"}, Zeile)
  GUI.ort_region = Geyser.Label:new({name = "ort_region"}, Zeile)

  -- Zeile 3
  Zeile = Geyser.HBox:new({name="HBox_line3"}, VBox)

  GUI.lp_titel = Geyser.Label:new({name = "lp_titel"}, Zeile)
  GUI.lp_titel:echo("Lebenspunkte:")

  GUI.lp_anzeige = Geyser.Gauge:new({name = "lp_anzeige"}, Zeile)
  GUI.lp_anzeige:setColor(0, 255, 50) 
  GUI.trenner_3 = Geyser.Label:new({name = "trenner_3"}, Zeile)
  GUI.kp_titel = Geyser.Label:new({name = "kp_titel"}, Zeile)
  GUI.kp_titel:echo("Konzentration:")

  GUI.kp_anzeige = Geyser.Gauge:new({name = "kp_anzeige"}, Zeile)
  GUI.kp_anzeige:setColor(0, 50, 250)

  GUI.trenner_2 = Geyser.Label:new({name = "trenner_2"}, Zeile)
end
"""
    ]

    blink_hp: trio.CancelScope = None

    async def show_player(self):   
        s = self.server
        try: name = s.me.base.name
        except AttributeError: name = "?"
        try: level = s.me.info.level
        except AttributeError: level = "?"
        await s.mmud.GUI.spieler.echo(f"{name} [{level}]")

    async def _gui_vitals_color(self, lp_ratio=None, **kw):
        s = self.server
        if lp_ratio is None:
            try:
                lp_ratio = s.me.vitals.hp/self.me.maxvitals.max_hp
            except AttributeError:
                lp_ratio = 0.9
        if lp_ratio > 1:
            lp_ratio = 1
        await s.mmud.GUI.lp_anzeige.setColor(255 * (1 - lp_ratio), 255 * lp_ratio, 50, **kw)

    async def _gui_vitals_blink_hp(self, task_status=trio.TASK_STATUS_IGNORED):
        s = self.server
        with trio.CancelScope() as cs:
            self.blink_hp = cs
            task_status.started(cs)
            try:
                await s.mmud.GUI.lp_anzeige.setColor(255, 0, 50)
                await trio.sleep(0.5)
                await self._gui_vitals_color()
            finally:
                if self.blink_hp == cs:
                    self.blink_hp = None

    async def show_room_label(self, room):
        s = self.server
        label = room.label or "-"
        await s.mmud.GUI.raumtype.echo(label, noreply=True)

    async def show_room_data(self, room=None):
        s = self.server
        if room is None:
            room = s.room
        if room.id_mudlet:
            await s.mud.centerview(room.id_mudlet)
        else:
            await s.print("WARNING: you are in an unmapped room.")
        r,g,b = 30,30,30  # adapt for parallel worlds or whatever
        await s.mmud.GUI.ort_raum.echo(room.name)
        if room.area:
            await s.mmud.GUI.ort_region.echo(f"{room.area.name} [{room.id_str}]")
        else:
            await s.mmud.GUI.ort_region.echo(f"? [{room.id_str}]")
            r,g,b = 80,30,30

        await s.mmud.GUI.ort_raum.setColor(r, g, b)
        await s.mmud.GUI.ort_region.setColor(r, g, b)
        await self.show_room_label(room)
        await self.show_room_note(room)

    async def show_room_note(self, room=None):
        s = self.server
        if room is None:
            room = s.room
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
        await s.mmud.GUI.raumnotizen.echo(note)

    async def show_vitals(self):
        s = self.server
        try:
            v = s.me.vitals
        except AttributeError:
            return
        try:
            w = s.me.maxvitals
        except AttributeError:
            await s.mmud.GUI.lp_anzeige.setValue(1,1, f"<b> {v.hp}/?</b> ", noreply=True)
            await s.mmud.GUI.kp_anzeige.setValue(1,1, f"<b> {v.sp}/?</b> ", noreply=True)
            await s.mmud.GUI.gift.echo("", noreply=True)
        else:
            await s.mmud.GUI.lp_anzeige.setValue(v.hp,w.max_hp, f"<b> {v.hp}/{w.max_hp}</b> ", noreply=True)
            await s.mmud.GUI.kp_anzeige.setValue(v.sp,w.max_sp, f"<b> {v.sp}/{w.max_sp}</b> ", noreply=True)
            if v.poison:
                r,g,b = 255,255-160*v.poison/w.max_poison,0
                line = f"G I F T  {v.poison}/{w.max_poison}"
            else:
                r,g,b = 30,30,30
                line = ""
            await s.mmud.GUI.gift.echo(line, "white", noreply=True)
            await s.mmud.GUI.gift.setColor(r, g, b, noreply=True)

            if not self.blink_hp:
                await self._gui_vitals_color(v.hp / w.max_hp, noreply=True)

        if "last_hp" in s.me and s.me.last_hp > v.hp:
            await s.main.start(self._gui_vitals_blink_hp)
        s.me.last_hp = v.hp

        # TODO flight


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

    def init_mud(self):
        yield from super().init_mud()
        yield "kurz"
        yield "telnegs aus"
        yield "report senden"

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
        await self.show_player()

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
        await self.show_player()

    async def event_gmcp_MG_char_base(self, msg):
        s = self.server
        logger.debug("BaseMG %s: %r",msg[1],msg[2])
        s.me.base = AD(msg[2])
        await self.show_player()

    async def event_gmcp_MG_char_info(self, msg):
        s = self.server
        logger.debug("InfoMG %s: %r",msg[1],msg[2])
        s.me.info = AD(msg[2])
        await self.show_player()

    async def event_gmcp_MG_char_vitals(self, msg):
        s = self.server
        logger.debug("VitalsMG %s: %r",msg[1],msg[2])
        s.me.vitals = AD(msg[2])
        await self.show_vitals()

    async def event_gmcp_MG_char_maxvitals(self, msg):
        s = self.server
        logger.debug("MaxVitalsMG %s: %r",msg[1],msg[2])
        s.me.maxvitals = AD(msg[2])
        await self.show_vitals()

    async def event_gmcp_MG_room_info(self, msg):   
        s = self.server
        if len(msg) > 2:
            info = AD(msg[2])
        else:
            info = await s.mud.gmcp.MG.room.info

        c = s.current_command(no_info=True)
        await c.set_info(info)
        s.maybe_trigger_sender()
       
#   
#   {
#     Char = {
#       Status = {
#         guild = "tanjian",
#         level = 27
#       },
#       Vitals = {
#         maxmp = 186,
#         maxhp = 202,
#         hp = 202,
#         mp = 186
#       },
#       StatusVars = {
#         guild = "Gilde",
#         level = "Spielerstufe"
#       }
#     },
#     comm = {
#       channel = {
#         msg = [[
#   [<MasteR>:Darya verlaesst als Letzte die Ebene 'Elementare', worauf diese sich
#             in einem Blitz oktarinen Lichts aufloest.]
#   ]],
#         chan = "<MasteR>",
#         player = "Darya"
#       }
#     },
#     MG = {
#       char = {
#         attributes = {
#           dex = 19,
#           con = 20,
#           int = 18,
#           str = 17
#         },
#         info = {
#           level = 27,
#           guild_title = ", Lehrlingsanwaerter des Tau-Tau",
#           guild_level = 3
#         },
#         wimpy = {
#           wimpy_dir = 0,
#           wimpy = 20
#         },
#         vitals = {
#           poison = 0,
#           hp = 202,
#           sp = 186
#         },
#         infoVars = {
#           level = "Spielerstufe",
#           guild_title = "Gildentitel",
#           guild_level = "Gildenstufe"
#         },
#         base = {
#           wizlevel = 0,
#           race = "Mensch",
#           guild = "tanjian",
#           name = "Smurf",
#           presay = "",
#           title = ", Lehrlingsanwaerter des Tau-Tau"
#         },
#         maxvitals = {
#           max_poison = 10,
#           max_hp = 202,
#           max_sp = 186
#         }
#       },
#       room = {
#         info = {
#           short = "Hauptpostamt.",
#           id = "17c293ab6875fcb64c96d1ec6c3aa4b3",
#           domain = "Ebene",
#           exits = {
#             "sueden"
#           }
#         }
#       }
#     }
#   }
