#!/usr/bin/python3

from mudpyc import Server
import trio
from mudpyc.util import ValueEvent
from functools import partial
import logging

class S(Server):
    async def hello2(self, prompt,e):
        await trio.sleep(2)
        # from here you could again call into mudlet
        e.set("Hello, "+prompt)

    def hello(self, prompt):
        # do not call into Mudlet from here, you will deadlock
        e = ValueEvent()
        self.main.start_soon(self.hello2,prompt,e)
        return e

    async def run(self):
        print("connected")
        bb = await self.mud.py.backoff
        print("current back-off is",bb)

        self.register_call("hello", self.hello)

        async with self.event_monitor("*") as h:
            async for msg in h:
                print(msg)

        # example. The code below isn't reached because the loop above
        # doesn't terminate.
        async with self.event_monitor("gmcp.MG.room.info") as h:
            async for msg in h:
                args = msg.args
                if len(args) > 2:
                    # The event sender should add this but we'll check
                    # anyway
                    info = args[2]
                else:
                    info = await self.mud.gmcp.MG.room.info
                print("ROOM",info)

async def main():
    async with S(cfg=dict(name="sample_basic")) as s:
        await s.run()
logging.basicConfig(level=logging.INFO)
trio.run(main)
