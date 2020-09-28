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
        self.txt = txt
        super().__init__(**kw)

    async def check(self, room):
        n = room.long_descr
        if n and self.txt in n.lower():
            return SkipSignal
        n = room.note
        if n and self.txt in n.lower():
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
        self.thing = last_visit
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


#class Walker:
#    """
#    This class implements the actual movement through the MUD.
#    """
#    first_room = None
#    last_room = None
#
#    prev_room_id = None
#    _rec = None
#
#    def __init__(self, server, way):
#        self.s = server
#        self.way = way
#
#        self._done = trio.Event()
#        self._resumed = trio.Event()
#        self.prev_room = None
#        self.state = "new"
#
#        if way:
#            self.first_room = self.s.db.r_old(way[0])
#            self.last_room = self.s.db.r_old(way[-1])
#            self.s.main.start_soon(self.walk)
#        else:
#            self.state = "done"
#            self._done.set()
#
#    def __str__(self):
#        if self._rec:
#            return repr(self)
#        self._rec = True
#        res = _("Walk from {self.first_room.id_str}").format(self=self)
#        if self.first_room != self.next_room_id and self.next_room_id != self.last_room.id_old:
#            next_room = self.s.db.r_old(self.next_room_id)
#            res += _(" via {next_room.id_str}").format(next_room=next_room)
#        res += _(" to {self.last_room.id_str} ({self.state})").format(self=self)
#        self._rec = False
#        return res
#
#    @property
#    def in_room(self):
#        """Returns the room were currently in, from the server"""
#        r = self.s.room
#        if not r:
#            return None
#        return r.id_old
#
#    @property
#    def next_room_id(self):
#        """The room we want to enter next."""
#        return self.way[0]
#
#    async def walk(self):
#        """Start the walker."""
#        if self.s.walker is not self:
#            if self.s.walker is not None:
#                await self.s.walker.cancel()
#            self.s.walker = self
#        try:
#            with trio.CancelScope() as self.scope:
#                await self._walk()
#        finally:
#            self.state = "ended"
#            self._done.set()
#            if self.s.walker is self:
#                self.s.walker = None
#
#    async def _walk(self):
#        """The actual walker."""
#        await self.s.set_long_mode(False)
#        while self.way:
#            logged = False
#            resumed = False
#            while self.way and self.in_room != self.way[0]:
#                nr = self.s.db.r_old(self.next_room_id)
#                self.state = _("Wait for entering room {nr.id_str}:{nr.name}").format(nr=nr)
#                with trio.move_on_after(99 if logged else 5) as cs:
#                    await self.s.wait_moved()
#                if self.s.walker is not self:
#                    self.state = "Wait for reactivation"
#                    await self._resumed.wait()
#                    # way may have changed
#                    resumed = True
#                elif cs.cancel_called:
#                    if not logged:
#                        await self.s.set_long_mode(True)
#                        logged = True
#                    await self.s.mud.print("STALL: "+str(self))
#            if self.way:
#                self.prev_room_id = self.way.pop(0)
#            if not self.way:
#                await self.s.walk_done(True)
#                return
#            if resumed or logged:
#                await self.s.set_long_mode(False)
#            await self.step_to(self.next_room_id)
#
#
#    async def step_to(self, next_room:int):
#        """
#        Go to the adjacent room next_room by sending the relevant commands.
#        """
#        try:
#            exit = self.s.room.exit_to(next_room)
#            if isinstance(exit,list):
#                # grab the shortest
#                em = 99999
#                for x in exit:
#                    xl = sum(len(xm) for xm in x.moves)
#                    if xl > 0 and xl < em:
#                        em = xl
#                        exit = x
#            self.state = _("step to {exit.dst.id_str}").format(exit=exit)
#        except KeyError:
#            if isinstance(next_room,int):
#                next_room = self.s.db.r_old(next_room)
#            await self.s.mud.print(_("Cannot step from {self.s.room.id_str} to {next_room.id_str}").format(next_room=next_room, self=self))
#            self._done.set()
#            self.state = _("No exit from {self.s.room.id_str} to {exit.dst.id_str} ??").format(self=self, exit=exit)
#            return
#        if not exit.dst.id_mudlet:
#            self.state = _("No map going to {exit.dst.id_str}").format(exit=exit)
#            await self.s.mud.print(_("step to {exit.dst.id_str}: no map").format(exit=exit))
#            return
#        self.state = _("step to {exit.dst.id_str}: {jxm}").format(exit=exit, jxm=':'.join(exit.moves))
#        await self.s.send_commands(*exit.moves, err_str = _("walking from {self.s.room.id_str} to {exit.dst.id_str}").format(self=self,exit=exit))
#
#
#    async def stepback(self, did_move = True):
#        """
#        Call this when you run into an obstacle, like darkness.
#
#        Set did_move=True if you got a location update for the bad
#        room you find yourself in.
#
#        Set did_move=False if you're in darkness and didn't get a location
#        update.
#
#        In both cases the walk will resume after you manually entered that
#        room again.
#        """
#        self.s.walker = None
#        try:
#            if did_move:
#                exit = self.s.room.exit_to(self.s.last_room)
#            else:
#                # assume we got to the room
#                exit = self.s.db.r_old(self.way[0]).exit_to(self.s.room)
#        except KeyError:
#            await self.s.mud.print(_("Dunno how to get back from {self.s.room.id_str} to {self.s.last_room.id_str}").format(self=self))
#            return
#        await self.s.send_commands(*exit.moves, err_str = _("walking back from {self.s.room.id_str} to {self.s.last_room.id_str}").format(self=self))
#
#
#    async def resume(self, skip=0):
#        if skip:
#            del self.way[:skip]
#        self._resumed.set()
#        self._resumed = trio.Event()
#
#    async def cancel(self):
#        self.scope.cancel()
#        if self.s.walker is self:
#            await self.s.walk_done(False)
#
#    async def wait(self):
#        """
#        Wait for the walker to finish.
#
#        Returns a flag whether it succeeded.
#        """
#        await self._done.wait()
#        return not self.way
#
