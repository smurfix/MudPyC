#!/usr/bin/python3

from setuptools import setup, find_packages

LONG_DESC = """
Assume you are really annoyed with Lua and want to use a reasonable
scripting language. Say, Python.

This module lets you do that.

It establishes a bidirectional link between Mudlet/Lua and Python and
exchanges structured messages between the two.

Mudlet can do HTTP requests in the background, so we send a "long poll" PUSH
request to the Python server. The reply contains the incoming messages (as
a JSON array).

There are a couple of optimizations to be had:

* if "httpGET" is available, we use that instead of an empty PUSH.

* if the platform supports Unix FIFO nodes in the file system, we use that
  for sending to Python, as that's faster and less expensive than a HTTP
  request per message.

The only required parameter on the Mudlet side is the port number.

Errors / exceptions are generally propagated to the caller.
"""

setup(
    name="mudlet",
    use_scm_version={"version_scheme": "guess-next-dev", "local_scheme": "dirty-tag"},
    setup_requires=["setuptools_scm"],
    description="Talk to Mudlet with",
    url="http://github.com/smurfix/pymudlet",
    long_description=LONG_DESC,
    author="Matthias Urlichs",
    author_email="matthias@urlichs.de",
    license="GPLv3 or later",
    packages=find_packages(),
    install_requires=["trio >= 0.16", "hypercorn >= 10.2", "quart-trio"],
    keywords=["async", "mudlet", "MUD"],
    python_requires=">=3.6",
    classifiers=[
        "License :: OSI Approved :: GNU General Public License v3 or later (GPLv3+)",
        "Operating System :: POSIX :: Linux",
        "Programming Language :: Python :: 3 :: Only",
        "Programming Language :: Python :: 3.6",
        "Programming Language :: Python :: 3.7",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: Implementation :: CPython",
        "Programming Language :: Python :: Implementation :: PyPy",
        "Framework :: Trio",
    ],
)

