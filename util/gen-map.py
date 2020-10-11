#!/usr/bin/pyton3

from itertools import product

# This code generates a set of Mudlet key bindings that translate function
# and keypad keys with up to three modifiers (Shift, Control, AltGr) to
# "simple" codes which are then sent to MudPyC.
# Function keys are numbered starting with 1, keypad digits are 20+digit.
# the rest of the keypad is 30+.
# PrintScr can't be grabbed, at least not under Linux, but scroll and pause are.

def key(name,keyid,modid,code,mod):
    return f"""\
            <Key isActive="yes" isFolder="no">
                <name>{name}</name>
                <packageName></packageName>
                <script>py.fn({keyid},{modid})</script>
                <command></command>
                <keyCode>{code}</keyCode>
                <keyModifier>{mod}</keyModifier>
            </Key>
"""

codes = {
    "F1":(1,0x1000030),
    "F2":(2,0x1000031),
    "F3":(3,0x1000032),
    "F4":(4,0x1000033),
    "F5":(5,0x1000034),
    "F6":(6,0x1000035),
    "F7":(7,0x1000036),
    "F8":(8,0x1000037),
    "F9":(9,0x1000038),
    "F10":(10,0x1000039),
    "F11":(11,0x100003a),
    "F12":(12,0x100003b),
    "Pause":(15,0x1000008),
    "ScrollLock":(14,0x1000026),
    "KP 0":(20,48,0x20000000),
    "KP 1":(21,49,0x20000000),
    "KP 2":(22,50,0x20000000),
    "KP 3":(23,51,0x20000000),
    "KP 4":(24,52,0x20000000),
    "KP 5":(25,53,0x20000000),
    "KP 6":(26,54,0x20000000),
    "KP 7":(27,55,0x20000000),
    "KP 8":(28,56,0x20000000),
    "KP 9":(29,57,0x20000000),
    "KP *":(32,42,0x20000000),
    "KP +":(30,43,0x20000000),
    "KP -":(31,45,0x20000000),
    "KP /":(34,47,0x20000000),
    "KP ,":(35,44,0x20000000),  # you have one or the other
    "KP .":(35,46,0x20000000),  # so we use the same fn code for both
    }
mods = {
    "AltGr":(4,0x40000000),
    "Control":(2,0x4000000),
    "Shift":(1,0x2000000),
}
f_mods = ((None,k) for k in mods.keys())
def all_mods():
    for k in product(*f_mods):
        v = 0
        n = []
        x = 0
        for kk in k:
            if kk is not None:
                n.append(kk)
                xv,nv = mods[kk]
                v |= nv
                x |= xv
        n = " ".join(n)
        yield n,x,v

for k,m in product(codes.keys(),all_mods()):
    n,x,v = m
    c = codes[k]
    if len(c)==3:
        nr,kc,v2 = c
    else:
        nr,kc = c
        v2 = 0
    print(key(f"{n} {k}".strip(),nr,x,kc,v+v2))
