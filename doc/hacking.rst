General code structure
======================

The main code for the MUD walking and analysis is in ``mudpyc/mapper/main.py``.

At the lowest level we send single commands, wait for a prompt / telnet GA,
repeat. That single command is represented by the `Command` class.

This class also captures basic movement. There are two ways to do that, (a)
GMCP information, (b) the Exits line that a MUD sends when you enter a
room.

Both may be missing. If only one is present, the arrival of the prompt or
GA triggers processing. If none are sent, you need to tell MudPyC.

Single commands are never entered directly; instead they're triggered by a
`Process`. Processes stack on top of each other so the quest process may
start the "kill NPC" sub-process, which may start a go-recharging sub-process,
which starts a "go from A to B" process, which ultimately starts a
"check if the door is open, press button and bribe the concierge" process
before sending the actual "south" command. You get the idea.

A process gets started by calling `Process.setup`. It terminates when
`Process.exit` is called. Both may be overridden in subclasses but you 
**must** eventually call the superclass's method.

The next step of a process is requested by calling `Process.next`, which
after some housekeeping calls `Process.queue_next` which you always
override. If you are finished, simply call the superclass because the
default implementation calls your ``exit`` method and continues with your
parent process, if any.

``queue_next`` doesn't return anything; instead, it should proceed by queueing
another process or a command, or maybe just do nothing until an NPC leaves,
which requires monitoring the MUD, which you can do via the
`S.text_watch` async context manager + iterator.

That leaves the question of how the user starts your code. This is what
macros are for. Macros start with a leader (hardcoded to '#' right now)
followed by any sequence of characters, optionally followed by whitespace
plus whatever argument(s) the macro wants. Argument resolution, handling
of quoting (multi-word exits) etc. is currently done by `S.cmdfix`.

Macros are single-letter because typing "#auto go recharge" gets tedious
to type after a while. Also, we want macros to be autodiscoverable and to
have help texts. Thus all macros can be followed by a question mark which
triggers the display of a multi-line help text for the macro itself plus a
single-line help text for any macro that starts with this sequence. Thus
``#?`` shows all the top-level macros. They're grouped by 

If you need input from the user, use an `S.input_grab_multi` async context
(multi-line, async-iterate the result), or `S.input_grab` (single line,
call ``await ctx.wait()`` to read the line). Be aware that this is
disruptive to the user. Also, input grabbers collide, so esp. for single
lines you should usually use a macro instead.

