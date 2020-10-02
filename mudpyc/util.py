"""
This module contains various helper functions and classes.
"""
from collections.abc import Mapping
import trio
import outcome
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

def doc(docstring):
    def decorate(fn):
        fn.__doc__ = docstring
        return fn
    return decorate

def combine_dict(*d, cls=dict, force=False):
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
    if not d:
        return res
    if len(d) == 1 and not force:
        return d[0]
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
            res[k] = combine_dict(*v, cls=cls, force=force)
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

def AD(x):
    return combine_dict(x, cls=attrdict, force=True)


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
            await self.fill_buf(max(1024, n_bytes-len(self.buf)))
        return await self.read(n_bytes)

class CancelledError(RuntimeError):
    pass

class ValueEvent:
    """A waitable value useful for inter-task synchronization,
    inspired by :class:`threading.Event`.

    An event object manages an internal value, which is initially
    unset, and a task can wait for it to become True.

    Args:
      ``scope``:  A cancelation scope that will be cancelled if/when
                  this ValueEvent is. Used for clean cancel propagation.

    Note that the value can only be read once.
    """

    event = None
    value = None
    scope = None

    def __init__(self):
        self.event = trio.Event()

    def set(self, value):
        """Set the result to return this value, and wake any waiting task.
        """
        self.value = outcome.Value(value)
        self.event.set()

    def set_error(self, exc):
        """Set the result to raise this exceptio, and wake any waiting task.
        """
        self.value = outcome.Error(exc)
        self.event.set()

    def is_set(self):
        """Check whether the event has occurred.
        """
        return self.value is not None

    async def cancel(self):
        """Send a cancelation to the recipient.
        """
        if self.scope is not None:
            self.scope.cancel()
        await self.set_error(CancelledError())

    async def get(self):
        """Block until the value is set.

        If it's already set, then this method returns immediately.

        The value can only be read once.
        """
        await self.event.wait()
        return self.value.unwrap()

