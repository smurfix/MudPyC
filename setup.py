#!/usr/bin/python3

from setuptools import setup, find_packages

LONG_DESC = """
MudPyC is a client for MUDs written in Python.

A MUD is a text-based multi-user role-playing game. If you don't already
know what that is, you're probably looking at the wrong package.

MudPyC will do it all: sophisticated mapping, macros and shortcuts, support
for combat, and whatnot. The idea is to be MUD-, frontend- and
language-independent, with configurable extension modules for popular MUDs.

We're not there yet. The current version has only been tested with the
German "Morgengrauen" MUD and the only front-end it supports is a lightly
modified version of Mudlet. On the plus side, MudPyC is able to import your
existing maps from Mudlet.
"""

setup(
    name="mudpyc",
    use_scm_version={"version_scheme": "guess-next-dev", "local_scheme": "dirty-tag"},
    setup_requires=["setuptools_scm"],
    description="A MUD client",
    url="http://github.com/smurfix/MudPyC",
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
        "Programming Language :: Python :: 3.7",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: Implementation :: CPython",
        "Programming Language :: Python :: Implementation :: PyPy",
        "Framework :: Trio",
    ],
)

