from functools import partial
from inspect import cleandoc

class Alias:
    """
    This class implements an iterative alias processor for MudPyC.

    Usage:
      Do this in your derived class::

        def do_register_aliases(self):
            super().do_register_aliases()

            self.alias.at("m").helptext = "Mapping"
            # any other help texts for submenues without command
            # this 'at' doesn't need a "create=True" if, as is to be
            # expected, you do have an alias under that letter(s)

        async def alias_ma(self, command):
            "add a room to the map."
            if not command.strip():
                await self.mud.print(_("A room needs a name!"))
                return
            ...  # the actual code

        Thus `#?` and `#m?` and `#ma?` will print one-line help texts
        so that the user can quickly discover which command they need but
        have forgotten, while `#ma Harry's Freehold` will add that room to
        the map.

        Aliases can be arbitrarily long but each step is a single letter.
        A space ends the alias invocation, everything after it is passed to
        the command as a single string. The alias is expected to do word
        splitting itself if it needs to do so.
    """
    parent = None
    sub = None
    cmd = None
    func = None
    s = None  
    helptext = None
        
    def __init__(self, parent, cmd, func=None, helptext=None):
        if len(cmd) != 1:
            raise RuntimeError("Command letters, not strings")
        self.sub = {}
        if isinstance(parent, Alias):
            self.s = parent.s
            self.parent = parent
            parent.sub[cmd] = self
        else:    
            self.s = parent   
        self.cmd = cmd
        self.func=func
        self.helptext=helptext

    def at(self, cmd, create=False):
        """
        Select the (sub) command behind this alias.
        """
        if not cmd or cmd[0] in " ?":
            return self
        try:
            sub = self.sub[cmd[0]]
        except KeyError:
            if not create:
                raise
            sub = Alias(self,cmd[0])    
        return sub.at(cmd[1:], create=create)
    
    @property
    def prompt(self):
        """collect my prompt"""
        return (self.parent.prompt if self.parent else "") + self.cmd

    async def print_help(self, err=None, with_sub=None):
        """
        Print a (sub) command's help text,
        including one-liners for direct subcommands.

        Params:
            err:
                error message, printed before the help text if given.
            with_sub:
                controls whether to print one-liners for subcommands.
                Defaults to True iff no error message is used.
        """
        ht = []
        p = self.prompt
        if err:
            ht.append(p + "  : " + err)
        if with_sub is None:
            with_sub = err is None
        if self.helptext:
            ht.append(p + "  : " + cleandoc(self.helptext).replace("\n","\n"+" "*len(p)+"  : "))

        if with_sub and self.sub:
            p = self.prompt
            ht.append(_("Subcommands:"))
            for k,v in self.sub.items():
                vh = cleandoc(v.helptext).split("\n",1)[0] if v.helptext else _("(no help text known)")
                ht.append(f"{p+k} {'*' if v.sub and v.func else '+' if v.sub else ':'} {vh}")
        await self.s.mud.print("\n".join(ht), noreply=True)

    async def __call__(self, cmd):
        """Call the function associated with this subcommand."""
        if self.func:
            return await self.func(cmd)
        await self.print_help(with_sub=True)


def with_alias(x):
    """
    Ability to set an alias name that can't be expressed as function name,
    like 'mx-'.
    """

    def fn(y):
        y.real_alias = x
        return y 
    return fn

