#!/usr/bin/pyton3

from itertools import product

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
    "F1":(1,16777264),
    "F2":(2,16777265),
    "F3":(3,16777266),
    "F4":(4,16777267),
    "F5":(5,16777268),
    "F6":(6,16777269),
    "F7":(7,16777270),
    "F8":(8,16777271),
    "F9":(9,16777272),
    "F10":(10,16777273),
    "F11":(11,16777274),
    "F12":(12,16777275),
    "Pause":(15,16777224),
    "ScrollLock":(14,16777254),
    "KP 0":(0,48,536870912),
    "KP 1":(8,49,536870912),
    "KP 2":(8,50,536870912),
    "KP 3":(8,51,536870912),
    "KP 4":(8,52,536870912),
    "KP 5":(8,53,536870912),
    "KP 6":(8,54,536870912),
    "KP 7":(8,55,536870912),
    "KP 8":(8,56,536870912),
    "KP 9":(8,57,536870912),
    "KP *":(8,42,536870912),
    "KP +":(8,43,536870912),
    "KP -":(8,45,536870912),
    "KP /":(8,47,536870912),
    "KP ,":(8,44,536870912),
    "KP .":(8,46,536870912),
    }
mods = {
    "Shift":(1,33554432),
    "Control":(2,67108864),
    "AltGr":(4,1073741824),
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
