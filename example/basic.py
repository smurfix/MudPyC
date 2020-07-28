#!/usr/bin/python3

from mudlet import Server
import trio

async def hello(prompt):
    return "Hello, "+prompt

async def main():
    async with Server(cfg=dict(name="sample_basic")) as s:
        print("connected")
        bb = await s.mud.py.backoff
        print("current back-off is",bb)
        s.register_call("hello", hello)

        async with s.events("gmcp.MG.room.info") as h:
            async for msg in h:
                info = await s.mud.gmcp.MG.room.info
                print("ROOM",info)


trio.run(main)
