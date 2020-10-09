# Wayfinder

import trio
from inspect import iscoroutine

import logging
logger = logging.getLogger(__name__)

from .const import SignalThis, SkipRoute, SkipSignal, SignalDone, Continue


class PathChecker:
    """
    Abstract base class for path checks
    """
    def __init__(self, reporter=None):
        self.reporter = reporter

    async def check(self, room):
        """
        Simple check function.
        Override me.
        """
        return None

    async def check_full(self, n, r, p):
        """
        Full check code which you might override.
        n: results so far
        r: current room
        p: path to the current room
        """
        res = await self.check(r)
        if res is None:
            res = Continue
        if self.reporter:
            await self.reporter(n,r,p,res)
        return res

class CachedPathChecker(PathChecker):
    """
    Abstract base class for path checks which processes cache entries
    """
    pass


class RoomFinder(CachedPathChecker):
    def __init__(self, room, **kw):
        self.room = room.id_old
        super().__init__(**kw)

    async def check(self, room):
        if self.room == room.id_old:
            # there can be only one
            return SignalDone

class LabelChecker(CachedPathChecker):
    def __init__(self, label, **kw):
        self.label = label
        super().__init__(**kw)

    async def check(self, room):
        if self.label == room.label:
            return SignalThis

class FulltextChecker(PathChecker):
    def __init__(self, txt, **kw):
        self.txt = txt.lower()
        super().__init__(**kw)

    async def check(self, room):
        n = room.long_descr
        if n and self.txt in n.descr.lower():
            return SkipSignal
        n = room.note
        if n and self.txt in n.note.lower():
            return SkipSignal

class VisitChecker(PathChecker):
    def __init__(self, last_visit, **kw):
        self.last_visit = last_visit
        super().__init__(**kw)

    async def check(self, room):
        if room.last_visit is None or room.last_visit < self.last_visit:
            return SkipSignal

class ThingChecker(PathChecker):
    def __init__(self, thing, **kw):
        self.thing = thing
        super().__init__(**kw)

    async def check(self, room):
        for t in room.things:
            if self.thing in t.name.lower():
                return SignalThis

class SkipFound:
    """
    A mix-in that doesn't create paths through results
    """
    async def check(self, room):
        res = await super().check(room)
        if res is None:
            res = Continue
        if res.signal:
            res = res(skip=True)
        return res


class PathGenerator:
    _scope: trio.CancelScope = None

    def __init__(self, server, start_room, checker:PathChecker, n_results = 3):
        assert start_room is not None
        self.s = server
        self.checker = checker
        self.start_room = start_room

        self.results = []  # (destination,path)
        self.n_results = n_results
        self._n_results = trio.Semaphore(n_results)
        self._stall_wait = trio.Event()

    def __repr__(self):
        return "PG‹%s›" % (" ".join("%s=%s" % (k,v) for k,v in self._repr().items()),)

    def _repr(self):
        res = {}
        res["run"] = "Y" if self.is_running else "N"
        if self.is_stalled:
            res["stall"] = "Y"
        res["sem"] = self._n_results.value
        res["semw"] = self._n_results.statistics().tasks_waiting
        res["start"] = self.start_room.idn_str
        return res

    def is_running(self):
        return self._scope is not None

    def is_stalled(self):
        """True if the generator needs a call to make_more_results to continue"""
        return self._stall_wait.is_set()

    async def wait_stalled(self):
        """True if newly stalled. Use this to signal printing a request for more."""
        if self._scope is None:
            return False
        if self._stall_wait.is_set():
            self._stall_wait = trio.Event()
        await self._stall_wait.wait()

        return self._scope is not None

    def make_more_results(self, n):
        """Call to increase the number of results."""
        while n:
            self._n_results.release()
            n -= 1

    async def __aenter__(self):
        await self.s.main.start(self._build)
        return self

    async def __aexit__(self, *tb):
        await self.cancel()

    async def cancel(self):
        if self._scope:
            self._scope.cancel()
            self._scope = None
        if self._stall_wait:
            self._stall_wait.set()

    async def _build(self, *, task_status=trio.TASK_STATUS_IGNORED):
        """The task that actually generates the results."""
        try:
            with trio.CancelScope() as self._scope:
                task_status.started()

                first = True
                seq = self.start_room.reachable.__aiter__()
                p = None
                while True:
                    await trio.sleep(0)
                    if first and p:
                        p = p(skip=False)
                        first = False
                    try:
                        h = await seq.asend(p)
                    except StopAsyncIteration:
                        return
                    r = h[-1]
                    p = self.checker.check_full(len(self.results), r,h)
                    if iscoroutine(p):
                        p = await p
                    if p.signal:
                        self.results.append((r,h))
                        if self.n_results == 1 or p.done:
                            await self.cancel()
                            return
                    if p.done:
                        await seq.aclose()
                        return

                    if p.signal:
                        # This dance suspends the searcher if it's waiting for
                        # new slots.
                        # The main code calls `wait_stalled`, which returns
                        # True when this code gets suspended (as soon as we
                        # set `stall_wait`) so that it can tell the user.
                        try:
                            self._n_results.acquire_nowait()
                        except trio.WouldBlock:
                            self._stall_wait.set()
                            await self._n_results.acquire()
        finally:
            self._scope = None


