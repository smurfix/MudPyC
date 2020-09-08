======
MudPyC
======

MudPyC is a client for MUDs, written in Python.

A MUD is a text-based role-playing game. If you don't already know what tha
is, you probably downloaded the wrong package.

MudPyC will do it all: sophisticated mapping, macros and shortcuts, support
for combat and recharge, and whatnot. The idea is to be MUD-, frontend- and
language-independent, with configurable extension modules for popular MUDs.

We're not there yet. The current version is based on the German
"Morgengrauen" MUD, and the only front-end it supports is a lightly modified
version of Mudlet, and the only translation is, surprise, German.

On the plus side, MudPyC is able to import your existing maps from Mudlet.

Mudlet is programmed in Lua. MudPyC includes an adapter for fast and
reliable bidirectional communication between Mudlet and the core MudPyC
driver. A single RPC call in either direction takes around one millisecond.

