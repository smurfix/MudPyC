# Semi-singleton return codes for path lookup.
# The idea is to return something with known but still somewhat malleable
# attributes.
# So, the check functions return these classes. If a wrapper needs to
# modify them, it calls the class (returning a modifyable object) with
# the attr(s) it wants to affect. If another wrapper does the same, it
# calls the object, which returns self. These objects are never copied or
# reused, thus this is safe.

class _PathSignal:
    skip = False
    # discard paths through this room

    signal = False
    # this is a result

    done = False
    # stop scanning entirely


    def __init__(self, **kw):
        self(**kw)

    def __call__(self, **kw):
        for k,v in kw.items():
            setattr(self,k,v)
        return self

class Continue(_PathSignal):
    pass

class SignalThis(_PathSignal):
    """this room matches"""
    signal = True

class SkipRoute(_PathSignal):
    """Don't create a route through this room"""
    skip = True

class SkipSignal(_PathSignal):
    """both SkipRoute and SignalThis"""
    skip = True
    signal = True

class SignalDone(_PathSignal):
    """This is it, don't bother searching further"""
    signal = True
    done = True

class AllDone(_PathSignal):
    """This is not it, but don't bother searching further"""
    done = True

# Room tags in Mudlet.
# Corresponds to possible return values of `Room.open_exits`
ENV_OK=101
ENV_STD=102
ENV_SPECIAL=103
ENV_UNMAPPED=104

