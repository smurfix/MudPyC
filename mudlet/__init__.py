from .server import Server, run_in_task
from .alias import Alias, with_alias

__all__ = ["Server","run_in_task","Alias","with_alias"]

import gettext
gettext.install("pymudlet", localedir="locales", names="gettext ngettext pgettext npgettext".split())
