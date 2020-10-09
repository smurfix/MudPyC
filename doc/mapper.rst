=======================
Using the Mudlet Mapper
=======================

This package implements a "real" map generator and a heap of
explore-a-MUD-and-play features.

Setup
=====

You need Mudlet, ideally an account on a supported MUD you're already
somewhat familiar with, and some patience to teach your fingers a bunch of
new macros. Even more ideally you should use Smurf's version of Mudlet
because it can display the MUD's room labels.

You also need a database. Databases are nice because they don't need to be
saved, quite unlike the map you create in Mudlet. Create one. This example
uses sqlite, so simply copy ``mapper.cfg.sample`` to ``mapper.cfg`` and
adapt it.

You find supported MUDs by checking the contents of ``mudpyc/driver/XX``
where ``XX`` is the language code (currently either "de" for German or "en"
for English"). Adapting things to a different MUD isn't that difficult;
please submit your code to the project so that other players may benefit
from it, and so we can try to keep things working when code is refactored.

Next start ``./run -c mapper.cfg --migrate`` which will create the database
tables. Start Mudlet, create a new profile for your MUD account, log
in, and go to an area which you know and which has reasonable cardinal
directions (i.e. ``north`` then ``south`` returns you to the room you
started at).

Then, load the ``mudpyc.xml`` module, type ``#py+`` and hit Enter.

You should see ``Connected.`` which means that Mudlet and MudPyC now talk
to each other. Essentially, Mudlet is now remote-controlled my MudPyC (or
vice versa).

Now let's start with mapping. If you don't already have a map, just walk
around a bit. Your mapping pane should auto-create interesting rooms.

If you already have a map, things get more complicated. Load the map and
type ``lua getRoomHashByID(123)`` where 123 is the number of the room
you're currently in. You should see a string of hex digits, which means
that your MUD uses GMCP to tell Mudlet about distinct rooms and that your
old mapping script knows how to set them in the map.

If there's no output, things might still work but your job just got a bit
more complicated. We'll talk about non-GMCP operation later. For now, just
proceed.

Now, your old mapping script is not loaded, so you need to manually set the
current location. Click on the room you're in and type ``#v ?``. You'll get
an error message because Mudlet doesn't know your room yet. That's OK, say
``#ms ??`` which tells MudPyC to set your location to the room you've
selected. That room is not known so we double the ``?`` to tell Mudlet to
create it in its database.

Thus the next step is ``#mdi .``. This instructs MudPyC to examine the
current room, or more specifically the room's exits. It'll then import the
rooms which those exits point to and do the same thing with them,
recursively. Note that this process won't find rooms which don't have any
exits pointing to them; if you have any of those you can click on each of
them and say ``#mdi ??``.

Congratulations, you now have copied your Mudlet map to your database.
While playing it'll update both.

If you want to copy your database back to Mudlet because you forgot to (or
could not) save the Mudlet map, say ``#mds``. Conversely, if you moved
around rooms in Mudlet while MudPyC was disconnected, say ``#mdt``.

You may wonder what to do if you forgot about MudPyC, extended your Mudlet
map using your old mapper, and then want to re-sync things. The problem
here is that Mudlet assigns IDs to the new rooms which may or may not match
MudPyC's idea of these IDs. The command to use is ``#mdi! ?`` This drops
all room associations between Mudlet and MudPyC except for the
currently-selected room (which should obviously be one you didn't touch
since the last import into MudPyC), and then re-creates the association
between Mudlet and MudPyC. For this command to work, you need to select a
room which existed before the versions diverged.

Room shortnames
===============

Your MUD sends short room names. Smurf's Mudlet mod can display them if you
turn that on.

The names start out below the room, centered. The map looks best with room
size set to 10 and a room distance of 5 or so.

Room labels will probably collide. You can move them around with the
left/right arrow keys (press Control for horizontal, Shift-Control for
vertical adjustment). The step sizes are configurable. Use ``#rs`` to
change a room's name if is not unique enough or too long.

Note that Smurf's mod to Mudlet uses a new experimental map version 21 to
store the labels' locations. If you didn't or couldn't use it, ``#mds``
will restore the label positions after restarting Mudlet.


Those ``#`` macro things
========================

MudPyC intentionally doesn't use whole-word commands. Your MUD already does
that. Also, ``#auto go recharge`` gets tedious to type after a while.

Also, we want macros to be autodiscoverable and to have help texts. Thus
all macros can be followed by a question mark which triggers the display
of a multi-line help text for the macro itself, plus a single-line help
text for any macro that starts with this sequence. Thus ``#?`` shows all
the top-level macros. They're grouped by function and hopefully it's all
mostly-self-explaining.

Many macros have arguments. These are typically rooms, exits or other short
names, or "the rest of the line". If you have multi-word exits, you may use
quotes. Backslash-scaping the space between them also works.


Room selection
--------------

You can use

* a positive integer: the room number shown by Mudlet.
* a negative integer: a room number in MudPyC's database. Some
  commands show that when they don't know the corresponding Mudlet room.
* ``.`` – the room your character currently is in (OK … the room MudPyC
  thinks your character is in, which hopefully is the same but obviously
  may not be)
* ``:`` – the room you just left
* ``!`` – the room that's the "current player location" in Mudlet, which
  can be changed with ``#v`` commands
* ``?`` – the room currently selected on the Mudlet map, or the current
  selection's center
* If you just created a room list with ``#gt``, you can use ``#N``
  to select the N'th room on that list.

Movement
========

In an ideal world your character moves around in the MUD and your map stays
in sync with it.

We don't live in an ideal world.

There's atypical exits, rooms without GMCP info, closed doors, darkness,
and whatnot.

MudPyC will try to take all of these into account but sometimes it'll miss
out. Here's what happens.

* If the MUD sends a GMCP ID in response to a command, Mudlet will create
  a new room with that ID (if it doesn't already know the ID), and an exit
  named with that command (unless it already exists).

* MudPyC will create a "timed" exit if this happens outside of a command.

* If the exit does exist but points someplace else, MudPyC will tell you
  but not update the exit.

* An empty GMCP ID forces a room change. So does an Exits line ("There are
  5 visible exits: left, right, up, down, and wherever"). If the exit is
  not known, a new room is created.

* If you ended up in a different room, call ``#mn room`` (which sets
  the exits and deletes the new room it just created by mistake), or
  ``#ms`` (which does not).

* If MudPyC doesn't recognize the fact that you moved, use ``#mm``.

* "w" is the same as "west". Likewise for nw w ne e se s sw w u d (up and
  down).

Mapping
=======

A back exit is auto-created if the opposite direction is known ("north" vs
"south"). Otherwise you can do it yourself: ``#mp . wayback_name :``. TODO:
If your destination advertises a special way back exit (e.g. "leave") which
isn't set, that'll be used.

You can turn off creation of back exits with ``#cfr``.

New rooms are placed according to the configuration. Use ``#c?`` to
discover the parameters you can change.

The configuration is stored in the database.

If you want to shorten your exit names, or map them to known directions,
use ``#xc`` or ``#xt``. If you have entire regions which need a specific
command to enter and exit, like "turn on/off torch", create a feature
(``#xf?`` tells you more)

Auto movement
=============

To quickly go from A to B, say ``#g# B`` (if you're in A of course). MudPyC
will calculate the fastest way to do that, and do it.

You can assign exits with higher cost with ``#xp`` if e.g. taking the lift
is slower than running down the stairs. Pricing an entire room (``#rp``) is
equivalent to doing that to all the room's exits (not entrances).

You can label rooms (``#rt``), then generate a list of the three closest
rooms with that label (``#gt label``), and select one of them with ``#gg
NUMBER``. These ways are stored, thus ``#gg`` only works when you're in the
room you did the ``#gt`` in.

To return to your previous location, use ``#gr`` which shows the last nine
starting points. Use ``#gr N`` to use the N'th entry in that list.

You can create quests, which are currently just a list of rooms and actions
to do there. Use ``#qq+ NAME`` to create one, ``#q+ COMMAND`` to add a
command to it, ``#qqa NAME`` to start running the quest, and ``#qn`` to
execute the quest's next step. If you keep that in Mudlet's command buffer
you just need to press Enter repeatedly. No, there's currently no way to
auto-run a complete quest, that's on the TODO list.

Finally, to explore, use ``#mux`` to find the closest rooms with exits you
didn't explore yet. That list doesn't go generate paths through such rooms;
if you want to include these, use ``#muxx``.

In general, most auto-move commands will not continue through rooms that
they found because that's rarely interesting. If you want a route to any of
an area's entrances, for instance, the route to entry point B is only of
interest if it does **not** pass through A.

View vs. location
=================

Mudlet's current player location is distinct from MudPyC's. Specifically
you can set Mudlet's location to any room you choose, which is very helpful
e.g. when you want to choose which room from a result list to visit: use
``#v`` for that. You can then use ``#vg`` to move the avatar around. ``#v .``
resets the view to the room you're really in. ``#g# !`` speed-walks you to
the viewed location.



