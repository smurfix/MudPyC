++++++++++++
Message Spec
++++++++++++

Messages to Mudlet contain ``action`` and (optionally) ``seq`` entries.
``seq`` is a sequence number and will be echoed to Python, along with 
either the result of the action (``result``) or an error message
(``error``).

Messages from Mudlet are replies unless they contain an ``action`` entry.

To Mudlet
+++++++++

init
----

Setup. ``fifo`` is the file to use for messages to Python.

call
----

Call a Lua function. ``name`` is the function name as a list (instead of a
dotted string), ``args`` the arguments. If ``meth`` is True, the function
is assumed to be a method (``func:name`` in Lua) and will receive its
object as the first parameter.

get
---

Read a value. ``name`` is the name of the variable, as a list (instead of a
dotted string).

set
---

Write a value. ``name`` is the name of the variable, as a list (instead of a
dotted string). ``value`` is the value.

The old value is returned if ``old`` is set.

As in Lua, send a value of ``None`` to clear it.

handle
------

Listen to an event. ``event`` is its name. Returns ``True`` if the handler
has been added successfully.

unhandle
--------

Stop listening to an event. ``event`` is its name. Returns ``True`` if the
handler has been removed successfully.


From Mudlet
+++++++++++

init
----

The initial link-up message. Must be replied to with an ``init`` message.

poll
----

No-op for polling.

event
-----

An event has occurred. ``event`` is the event name, ``args`` its arguments.

