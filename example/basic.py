#!/usr/bin/python3

from mudlet import Server
import trio
from mudlet.util import ValueEvent
from functools import partial

async def hello2(prompt,e):
    await trio.sleep(2)
    e.set("Hello, "+prompt)

def hello(s, prompt):
    e = ValueEvent()
    s.main.start_soon(hello2,prompt,e)
    return e

async def main():
    async with Server(cfg=dict(name="sample_basic")) as s:
        print("connected")
        bb = await s.mud.py.backoff
        print("current back-off is",bb)
        s.register_call("hello", partial(hello,s))

        async with s.events("gmcp.MG.room.info") as h:
            async for msg in h:
                info = await s.mud.gmcp.MG.room.info
                print("ROOM",info)


trio.run(main)
