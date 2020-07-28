=================
Mudlet vs. Python
=================

Assume you are really annoyed with Lua and want to use a reasonable
scripting language. Say, Python.

This module lets you do that.

It establishes a bidirectional link between Mudlet/Lua and Python and
exchanges structured messages between the two.

Mudlet can do HTTP requests in the background, so we send a "long poll" PUSH
request to the Python server. The reply contains the incoming messages (as
a JSON array).

There are a couple of optimizations to be had:

* if "httpGET" is available, we use that instead of an empty PUSH.

* if the platform supports Unix FIFO nodes in the file system, we use that
  for sending to Python, as that's faster and less expensive than a HTTP
  request per message.

The only required parameter on the Mudlet side is the port number.

Errors / exceptions are generally propagated to the caller.

.. note::

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
connection is established.

A ``PyDisconnect`` event is raised when the connection terminates.

Call ``py.call(NAME, PARAMS, CALLBACK)`` to call a function on the Python
side (needs to register, see below). ``CALLBACK`` is called with ``true``
and the result(s), or ``false`` and an error message.

+++++++++++++++++
Usage from Python
+++++++++++++++++

See ``example/basic.py`` for a simple server that emits the info of every
room you're entering. (Requires adjustment for your MUD.)

Call ``await s.mud.NAME`` to retrieve the Mudlet variable NAME. The name
may include dots; the value must be JSON-encodeable.

Call ``await s.mud.NAME._set(X)`` to set the Mudlet variable NAME to X. The
name may include dots; the value must be JSON-encodeable.

Call ``await s.mud.NAME(ARGS)`` to call the Mudlet function NAME. The name
may include dots; the return value(s) must be JSON-encodeable. If you
set ``meth=True`` the function is treated as a method (in Lua: a colon
in front of the name's last component, e.g. ``foo:bar()``). If you set
``dest`` to a list of names, the (first) result of the function is assigned
to that name instead of being returned.

Open an async context + async loop using ``s.events(NAME)`` to listen
for the Mudlet event ``NAME``.

Call ``s.register_call(NAME, FUNC)`` to register ``FUNC`` as being callable
from Mudlet; see above. If the result is a list/tuple, the Lua callback
will receive multiple arguments.

Call ``s.event(NAME, ARGS…)`` to raise an event within Mudlet.

The server's async context terminates with an ``EOFError`` if the Mudlet
connection ends, or a ``trio.TooSlowError`` if the server's regular Ping is
not answered within a couple of seconds. Otherwise it continues until
cancelled.

