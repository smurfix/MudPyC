from .server import Server, WebServer, run_in_task, PostEvent
from .alias import Alias, with_alias

__all__ = ["Server","run_in_task","Alias","with_alias","PostEvent"]

import gettext
gettext.install("mudpyc", localedir="locales", names="gettext ngettext pgettext npgettext".split())
