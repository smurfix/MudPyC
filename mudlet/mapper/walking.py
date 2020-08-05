# Wayfinders and walkers

import trio
from inspect import iscoroutine

from .const import SignalThis, SkipRoute, SkipSignal

class PathGenerator:
    _scope: trio.CancelScope = None
    _result_wait: trio.Event = None

    def __init__(self, server, start_room, check_fn, n_results = 3):
        self.s = server
        self.check_fn = check_fn
        self.start_room = start_room

        self.results = []
        self._n_results = trio.Semaphore(n_results)
        self._stall_wait = trio.Event()

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

    async def more_results(self):
        """Call to wait for more results; return True if any are actually available."""
        if self._scope is None:
            return False
        if self._result_wait is None:
            self._result_wait = trio.Event()
        await self._result_wait.wait()
        if self._scope is None:
            return False
        return True

    async def __aenter__(self):
        await self.s.main.start(self._build)
        return self

    async def __aexit__(self, *tb):
        self.cancel()

    async def cancel(self):
        if self._scope:
            self._scope.cancel()
            self._scope = None
        if self._result_wait:
            self._result_wait.set()
        if self._stall_wait:
            self._stall_wait.set()

    async def _build(self, *, task_status=trio.TASK_STATUS_IGNORED):
        """The task that actually generates the results."""
        try:
            with trio.CancelScope() as self._scope:
                task_status.started()

                seq = self.start_room.reachable.__aiter__()
                p = None
                while True:
                    try:
                        h = await seq.asend(p)
                    except StopAsyncIteration:
                        return
                    r = self.s.db.r_old(h[-1])
                    p = self.check_fn(len(self.results), r,h)
                    if iscoroutine(p):
                        p = await p
                    if p is StopIteration:
                        await seq.aclose()
                        return
                    if p is SignalThis or p is SkipSignal:
                        self.results.append((r,h))
                        if self._result_wait:
                            self._result_wait.set()
                            self._result_wait = None
                        try:
                            self._n_results.acquire_nowait()
                        except trio.WouldBlock:
                            self._stall_wait.set()
                            self._n_results.acquire()
                        await self._n_results.acquire()
        finally:
            self._scope = None


class Walker:
    """
    This class implements the actual movement through the MUD.
    """
    first_room = None
    last_room = None

    prev_room_id = None
    _rec = None

    def __init__(self, server, way):
        self.s = server
        self.way = way

        self._done = trio.Event()
        self._resumed = trio.Event()
        self.prev_room = None
        self.state = "new"

        if way:
            self.first_room = self.s.db.r_old(way[0])
            self.last_room = self.s.db.r_old(way[-1])
            self.s.main.start_soon(self.walk)
        else:
            self.state = "done"
            self._done.set()

    def __str__(self):
        if self._rec:
            return repr(self)
        self._rec = True
        res = f"Walk from {self.first_room.id_str}"
        if self.first_room != self.next_room_id and self.next_room_id != self.last_room.id_old:
            next_room = self.s.db.r_old(self.next_room_id)
            res += f" via {next_room.id_str}"
        res += f" to {self.last_room.id_str} ({self.state})"
        self._rec = False
        return res

    @property
    def in_room(self):
        """Returns the room were currently in, from the server"""
        r = self.s.room
        if not r:
            return None
        return r.id_old

    @property
    def next_room_id(self):
        """The room we want to enter next."""
        return self.way[0]

    async def walk(self):
        """Start the walker."""
        if self.s.walker is not self:
            if self.s.walker is not None:
                await self.s.walker.cancel()
            self.s.walker = self
        try:
            with trio.CancelScope() as self.scope:
                await self._walk()
        finally:
            self.state = "ended"
            self._done.set()
            if self.s.walker is self:
                self.s.walker = None

    async def _walk(self):
        """The actual walker."""
        await self.s.send_commands("kurz")
        while self.way:
            logged = False
            resumed = False
            while self.in_room != self.way[0]:
                self.state = f"Wait for movement to {self.next_room_id}"
                with trio.move_on_after(99 if logged else 5) as cs:
                    await self.s.wait_moved()
                if self.s.walker is not self:
                    self.state = "Wait for reactivation"
                    await self._resumed.wait()
                    resumed = True
                elif cs.cancel_called:
                    if not logged:
                        await self.s.send_commands("lang")
                        logged = True
                    await self.s.mud.print("STALL: "+str(self))
            self.prev_room_id = self.way.pop(0)
            if not self.way:
                await self.s.walk_done(True)
                return
            if resumed or logged:
                await self.s.send_commands("kurz")
            await self.step_to(self.next_room_id)


    async def step_to(self, next_room:int):
        """
        Go to the adjacent room next_room by sending the relevant commands.
        """
        try:
            exit = self.s.room.exit_to(next_room)
            if isinstance(exit,list):
                # grab the shortest
                em = 99999
                for x in exit:
                    xl = sum(len(xm) for xm in x.moves)
                    if xl > 0 and xl < em:
                        em = xl
                        exit = x
            self.state = f"step to {exit.dst.id_str}"
        except KeyError:
            if isinstance(next_room,int):
                next_room = self.s.db.r_old(next_room)
            await self.s.mud.print(f"Cannot step from {self.s.room.id_str} to {next_room.id_str}")
            self._done.set()
            self.state = f"No exit from {self.s.room.id_str} to {exit.dst.id_str} ??"
            return
        if not exit.dst.id_mudlet:
            self.state = f"No map going to {exit.dst.id_str}"
            await self.s.mud.print(f"step to {exit.dst.id_str}: no map")
            return
        self.state = f"step to {exit.dst.id_str}: {':'.join(exit.moves)}"
        await self.s.send_commands(*exit.moves, err_str = f"walking from {self.s.room.id_str} to {exit.dst.id_str}")


    async def stepback(self, did_move = True):
        """
        Call this when you run into an obstacle, like darkness.

        Set did_move=True if you got a location update for the bad
        room you find yourself in.

        Set did_move=False if you're in darkness and didn't get a location
        update.

        In both cases the wallk will resume after you manually entered that
        room again.
        """
        self.s.walker = None
        try:
            if did_move:
                exit = self.s.room.exit_to(self.s.last_room)
            else:
                # assume we got to the room
                exit = self.s.db.r_old(self.way[0]).exit_to(self.s.room)
        except KeyError:
            await self.s.mud.print(f"Dunno how to get back from {self.s.room.id_str} to {self.s.last_room.id_str}")
            return
        await self.s.send_commands(*exit.moves, err_str = f"walking back from {self.s.room.id_str} to {self.s.last_room.id_str}")


    async def resume(self):
        self._resumed.set()
        self._resumed = trio.Event()

    async def cancel(self):
        self.scope.cancel()
        if self.s.walker is self and not self.done:
            await self.s.walk_done(False)

    async def wait(self):
        """
        Wait for the walker to finish.

        Returns a flag whether it succeeded.
        """
        await self._done.wait()
        return not self.way

