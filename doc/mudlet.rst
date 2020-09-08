=================
Mudlet vs. Python
=================

This package's main author's primary motivation to write this was that he
really dislikes Lua and Mudlet's built-in editor. Instead, he thinks that
Python is a reasonable script language to use for his MUDding.

The project spiralled off from there.

Anyway. The way this works is that we install a set of small scripts into
Mudlet which establish a bidirectional link between it and Python.
Then we exchange structured messages (JSON) between the two.

Python-to-Lua: Mudlet can do HTTP requests in the background. so we send a
"long poll" PUSH request to the Python server. The reply contains the
incoming messages (as a JSON array).

Lua-to-Python: Same thing, but using a PUT request.

There are a couple of optimizations to be had:

* if "httpGET" is available, we use that instead of an almost-empty PUSH.

* if the platform supports FIFO nodes in the file system (Unix/Linux), we
  use that for Lua-to-Python, as it's less expensive than a HTTP request.

The only required parameter on the Mudlet side is the TCP port number.

Data storage is done via SQL. The Mudlet map is strictly write-only, with
one exception: rooms which are in the Mudlet map but not in SQL can be
imported.

Errors / exceptions are generally propagated to the caller.

:Note:
	The Python code uses `Trio <https://trio.readthedocs.io>`_. Trio is an
	alternate async framework for Python, i.e. it is *not* compatible with
	asyncio.

	There is a compatibility layer, trio-asyncio, but it's not really good
	for production use.

	There's another compatibility layer, anyio, but that is currently being
	rewritten and this author doesn't like doing any work twice.

	This code will become anyio-compatible, and thus will work
	with asyncio in addition to Trio, sometime later in 2020.

	That being said, Trio's programming paradigm ("Structured Concurrency")
	has a lot of advantages. You might want to look at it somewhat more
	closely.

License: GPLv3 or later.

+++++++++++++++++
Usage from Mudlet
+++++++++++++++++

Call ``py.init(PORT)``. A ``PyConnect`` event is raised when the
connection is established. The alias ``#py+`` does this; ``#py-`` turns the
connector off, all other strings starting with ``#`` will be forwarded to
the alias handler on th Python side.

A ``PyDisconnect`` event is raised when the connection terminates.

Call ``py.call(CALLBACK, NAME, PARAMS)`` to call a function on the Python
side (you need to register the function, see below). ``CALLBACK`` is called
with ``true`` and the result(s), or ``false`` and an error message.
Parameters must be json-encodeable. You can skip the `CALLBACK` if you
don't want to get a result back.

The author strongly recommends to use a CJSON-ified version of Mudlet
because if you ever pass something non-encodeable to yajl by mistake,
interesting things *will* happen. These can be had via
`the forum <https://forums.mudlet.org/viewtopic.php?f=5&t=22934>`_
(4.9.1) or `github <https://github.com/Mudlet/Mudlet/pull/4004>`_.

Also, this code exposes a race condition in Python's `Hypercorn
<https://pypi.org/project/Hypercorn/>`_ web server. The fix is `here
<https://gitlab.com/pgjones/hypercorn/-/merge_requests/41>`_.

+++++++++++++++++
Usage from Python
+++++++++++++++++

See ``example/basic.py`` for a simple server that emits the info of every
room you're entering. (Requires adjustment for your MUD.)

Call ``await self.mud.NAME`` to retrieve the Mudlet variable NAME. The name
may include dots; the value must be JSON-encodeable.

Call ``await self.mud.NAME._set(X)`` to set the Mudlet variable NAME to X. The
name may include dots; the value must be JSON-encodeable.

Call ``await self.mud.NAME(ARGS)`` to call the Mudlet function NAME. The name
may include dots; the return value(s) must be JSON-encodeable. If you
set ``meth=True`` the function is treated as a method (in Lua: a colon
in front of the name's last component, e.g. ``foo:bar()``). If you set
``dest`` to a list of names, the (first) result of the function is assigned
to that variable or object instead of being returned.

Open an async context + async loop using ``self.events(NAME)`` to listen
for the Mudlet event ``NAME``.

Either create a "called_NAME" method, or call ``self.register_call(NAME,
FUNC)`` to register ``FUNC`` as being callable from Mudlet; see above. If
the result is a list/tuple, the Lua callback will receive multiple
arguments. Callables may be async functions.

Call ``self.event(NAME, ARGS…)`` to raise an event within Mudlet.

Add ``alias_xy(STRING)`` methods to recognize the input ``#xy STRING``.
Do ``self.alias.at("x").helptext = "XX Handling"`` to add an info text
that's printed when the user just types ``#`` or ``#x?``. By default the
list of submenus contains the help text's first line.

The server's async context terminates with an ``EOFError`` if the Mudlet
connection ends, or a ``trio.TooSlowError`` if the server's regular Ping is
not answered within a couple of seconds. Otherwise it continues until
cancelled.

:Note:
    Handlers and callables are started directly from the server's main loop.
    If you want to call back into Mudlet from them, you **must** do this
    from a separate task. Use ``self.main.start_soon()`` to start it. The
    example program shows the basic structure.

    If you need to return a value to Mudlet from that separate task, you
    can do so via a ``mudlet.util.ValueEvent``. Create an instance of that
    in your callable, pass it on to your separate task, and return it.
    Just don't forget to set its value, or its error, at *some* point.

    The reason why MudPyC doesn't just run everything in parallel is that
    you might care about the order things arrive in. Mixing up the lines in
    your room descriptions is only fun the first ttime it happens.

