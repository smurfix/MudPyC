from .. import Driver as _Driver
from .. import ExitMatcher as _ExitMatcher

import re

class ExitMatcher(_ExitMatcher):
    M_INIT = [
        re.compile(r"^Es gibt \S+ sichtbare Ausgaenge:(.*)$"),
        re.compile(r"^Es gibt einen sichtbaren Ausgang:(.*)$"),
    ]
    M_NONE = [
        re.compile(r"^Es gibt keinen sichtbaren Ausgang.\s*"),
        re.compile(r"^Es gibt keine sichtbaren Ausgaenge.\s*"),
        re.compile(r"^Du kannst keine Ausgaenge erkennen.\s*"),
    ]
    M_JOIN_LAST = " und "

class Driver(_Driver):
    """
    Adaption for (generic) German MUDs
    """
    lang = "de"
    ExitMatcher = ExitMatcher

    NAMELESS = "<namenloser Raum>"
    LOOK = "schau"
    EXAMINE = "untersuche"

    _dir_local = tuple("norden sueden osten westen nordosten nordwesten suedosten suedwesten oben unten rein raus".split())
    _dir_short = tuple("n s o w no nw so sw ob u re r".split())

    keymap = {
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
    

    # everything else should be generic


