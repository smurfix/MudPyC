"""
This module contains various helper functions and classes.
"""
from collections.abc import Mapping
import trio
import os

import logging
logger = logging.getLogger(__name__)


def singleton(cls):
    return cls()


class TimeOnlyFormatter(logging.Formatter):
    default_time_format = "%H:%M:%S"
    default_msec_format = "%s.%03d"


@singleton
class NotGiven:
    """Placeholder value for 'no data' or 'deleted'."""

    def __getstate__(self):
        raise ValueError("You may not serialize this object")

    def __repr__(self):
        return "<*NotGiven*>"


def combine_dict(*d, cls=dict):
    """
    Returns a dict with all keys+values of all dict arguments.
    The first found value wins.

    This recurses if values are dicts.

    Args:
      cls (type): a class to instantiate the result with. Default: dict.
        Often used: :class:`attrdict`.
    """
    res = cls()
    keys = {}
    if len(d) <= 1:
        return d
    for kv in d:
        for k, v in kv.items():
            if k not in keys:
                keys[k] = []
            keys[k].append(v)
    for k, v in keys.items():
        if len(v) == 1:
            res[k] = v[0]
        elif not isinstance(v[0], Mapping):
            for vv in v[1:]:
                assert not isinstance(vv, Mapping)
            res[k] = v[0]
        else:
            res[k] = combine_dict(*v, cls=cls)
    return res


class attrdict(dict):
    """A dictionary which can be accessed via attributes, for convenience"""

    def __getattr__(self, a):
        if a.startswith("_"):
            return object.__getattr__(self, a)
        try:
            return self[a]
        except KeyError:
            raise AttributeError(a) from None

    def __setattr__(self, a, b):
        if a.startswith("_"):
            super(attrdict, self).__setattr__(a, b)
        else:
            self[a] = b

    def __delattr__(self, a):
        try:
            del self[a]
        except KeyError:
            raise AttributeError(a) from None

class OSLineReader:
    def __init__(self, fd, max_line_length=16384):
        self.fd = fd
        self.buf = bytearray()
        self.find_start = 0
        self.max_line_length=max_line_length

    async def readline(self):
        while True:
            newline_idx = self.buf.find(b'\n', self.find_start)
            if newline_idx < 0:
                # no b'\n' found in buf
                if len(self.buf) > self.max_line_length:
                    raise ValueError("line too long")
                # next time, start the search where this one left off
                self.find_start = len(self.buf)
                await self.fill_buf(1024)
            else:
                # b'\n' found in buf so return the line and move up buf
                line = self.buf[:newline_idx+1]
                # Update the buffer in place, to take advantage of bytearray's
                # optimized delete-from-beginning feature.
                del self.buf[:newline_idx+1]
                # next time, start the search from the beginning
                self.find_start = 0
                return line

    async def fill_buf(self, max_bytes=4096):
        await trio.lowlevel.wait_readable(self.fd)
        data = os.read(self.fd, max_bytes)
        if not data:
            raise EOFError
        self.buf += data

    async def read(self, max_bytes):
        if not len(self.buf):
            await self.fill_buf()

        if len(self.buf):
            if len(self.buf) <= max_bytes:
                buf, self.buf = self.buf, bytearray()
                return buf
            buf = self.buf[:max_bytes]
            del self.buf[:max_bytes]
            self.find_start = 0
            return buf

    async def read_all(self, n_bytes):
        while len(self.buf) < n_bytes:
            await self.fill_buf(max(1024, n_bytes.len(self.buf)))
        return await self.read(n_bytes)

