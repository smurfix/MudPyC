class SignalThis:
    """this room matches"""
    pass

class SkipRoute:
    """Don't create a route through this room"""
    pass

class SkipSignal:
    """both SkipRoute and SignalThis"""
    pass

# corresponds to possible return values of `Room.open_exits`
ENV_OK=101
ENV_STD=102
ENV_SPECIAL=103
ENV_UNMAPPED=104

