from .. import Driver as _Driver

class Driver(_Driver):
    """
    Adaption for (generic) German MUDs
    """
    lang = "de"

    _dir_local = tuple("norden sueden osten westen nordosten nordwesten suedosten suedwesten oben unten rein raus".split())
    _dir_short = tuple("n s o w no nw so sw ob u re r".split())

    # everything else should be generic
