"""Opt-in pytest plugin: run the selected tests with real I/O blocked (#105).

Load it with ``-p arborescent.pytest_io_block``. It never auto-loads (no ``pytest11`` entry point). A test whose
path matches a glob in the ``arbor_io_free`` ini setting (``--arbor-io-block`` selects every test) runs its call
phase with these raising ``IOBlockedError``: ``open`` of a non-source file, ``socket.connect``, ``subprocess.Popen``,
``os.system``, ``sqlite3.connect``, file removal and rename, and ``time``, ``datetime``, ``random`` and
``os.environ`` reads. Fixtures (setup and teardown) are not blocked, and the patches are removed again when the test ends. ``@pytest.mark.arbor_allow_io`` opts one test out.

Only calls whose first non-standard-library frame is neither pytest nor this module raise, so pytest's own bookkeeping
keeps working. A passing test shows the code did no I/O on the inputs the test chose. Not covered: names bound before
the patch (``from time import time`` at import), ``builtins.open`` patched by the test, ``ctypes``, C extensions.
"""

from __future__ import annotations

import datetime
import fnmatch
import os
import random
import sys
import time
from collections.abc import Callable, Iterator, MutableMapping
from pathlib import PurePath
from types import FrameType
from typing import cast

import pytest

__all__ = ["IOBlockedError", "pytest_addoption", "pytest_configure", "pytest_runtest_call"]

_SOURCE_SUFFIXES = (".py", ".pyc", ".pyi", ".so", ".pyd", ".pth")
_HOST = {"pytest", "_pytest", "pluggy", "py"}
_AUDIT_EVENTS = frozenset(
    {
        "socket.connect",
        "subprocess.Popen",
        "os.system",
        "sqlite3.connect",
        "os.remove",
        "os.rmdir",
        "os.rename",
        "shutil.rmtree",
    }
)
_active = False
_hook_installed = False
_saved: list[tuple[object, str, object]] = []  # (owner, attribute, original value) of every patch in force, restored in reverse


class IOBlockedError(AssertionError):
    """Raised when code under a blocked test reaches real I/O."""


def _from_host(depth: int) -> bool:
    frame: FrameType | None = sys._getframe(depth)
    while frame is not None:
        name = str(frame.f_globals.get("__name__", ""))
        top = name.split(".")[0]
        if top in sys.stdlib_module_names or top == "builtins":
            frame = frame.f_back
            continue
        return top in _HOST or name == __name__
    return True


def _audit(event: str, args: tuple[object, ...]) -> None:
    if not _active:
        return
    if event == "open":
        target = args[0]
        if isinstance(target, int):
            return
        if isinstance(target, str | bytes | os.PathLike) and os.fsdecode(target).endswith(_SOURCE_SUFFIXES):
            return
    elif event not in _AUDIT_EVENTS:
        return
    if _from_host(2):
        return
    raise IOBlockedError(f"{event} {args[:1]!r} blocked: the ring's tests run without real I/O")


def _blocked(label: str, real: Callable[..., object]) -> Callable[..., object]:
    def wrapper(*args: object, **kwargs: object) -> object:
        if _active and not _from_host(2):
            raise IOBlockedError(f"{label} blocked: inject a fake instead")
        return real(*args, **kwargs)

    return wrapper


class _BlockedEnviron:
    """Wraps ``os.environ``; every access from non-host code raises. A plain class, so no overrides to mark."""

    def __init__(self, real: MutableMapping[str, str]) -> None:
        self._real = real

    def _check(self) -> None:
        if _active and not _from_host(3):
            raise IOBlockedError("os.environ blocked: inject the value instead")

    def __getitem__(self, key: str) -> str:
        self._check()
        return self._real[key]

    def __setitem__(self, key: str, value: str) -> None:
        self._check()
        self._real[key] = value

    def __delitem__(self, key: str) -> None:
        self._check()
        del self._real[key]

    def __contains__(self, key: object) -> bool:
        self._check()
        return key in self._real

    def __iter__(self) -> Iterator[str]:
        self._check()
        return iter(self._real)

    def __len__(self) -> int:
        self._check()
        return len(self._real)

    def __getattr__(self, name: str) -> object:
        self._check()
        return getattr(self._real, name)


_TIME = ("time", "monotonic", "perf_counter", "time_ns", "sleep")
_RANDOM = ("random", "randint", "randrange", "choice", "choices", "shuffle", "sample", "uniform", "getrandbits")


_REAL_DATETIME = datetime.datetime


def _is_datetime(_cls: type, instance: object) -> bool:
    return isinstance(instance, _REAL_DATETIME)


def _datetime_attr(_cls: type, name: str) -> object:
    return getattr(_REAL_DATETIME, name)


# Built with type() so the dunder overrides need no ``typing.override`` (Python 3.12+).
_DatetimeMeta = type("_DatetimeMeta", (type,), {"__instancecheck__": _is_datetime, "__getattr__": _datetime_attr})


class _BlockedDatetime(metaclass=_DatetimeMeta):
    """Stands in for ``datetime.datetime``: construction and parsing pass through, clock reads raise."""

    def __new__(cls, *args: int, **kwargs: object) -> datetime.datetime:
        return cast(Callable[..., datetime.datetime], _REAL_DATETIME)(*args, **kwargs)

    @staticmethod
    def now(tz: datetime.tzinfo | None = None) -> datetime.datetime:
        if _active and not _from_host(2):
            raise IOBlockedError("datetime.now blocked: inject the time instead")
        return _REAL_DATETIME.now(tz)

    @staticmethod
    def today() -> datetime.datetime:
        if _active and not _from_host(2):
            raise IOBlockedError("datetime.today blocked: inject the time instead")
        return _REAL_DATETIME.today()


def _patch(owner: object, name: str, value: object) -> None:
    _saved.append((owner, name, getattr(owner, name)))
    setattr(owner, name, value)


def _install() -> None:
    """Patch for the duration of one selected test. The audit hook cannot be removed, so it stays and is gated by ``_active``."""
    global _hook_installed
    if not _hook_installed:
        _hook_installed = True
        sys.addaudithook(_audit)
    for name in _TIME:
        _patch(time, name, _blocked(f"time.{name}", getattr(time, name)))
    for name in _RANDOM:
        _patch(random, name, _blocked(f"random.{name}", getattr(random, name)))
    _patch(datetime, "datetime", _BlockedDatetime)
    _patch(os, "environ", _BlockedEnviron(os.environ))


def _uninstall() -> None:
    """Undo every patch so a later test, blocked or not, sees the real ``datetime``, ``time``, ``random`` and ``os.environ``."""
    while _saved:
        owner, name, original = _saved.pop()
        setattr(owner, name, original)


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addini("arbor_io_free", "globs of test paths (relative to rootdir) run with real I/O blocked", "linelist")
    parser.addoption("--arbor-io-block", action="store_true", help="run every test with real I/O blocked")


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "arbor_allow_io: run this test without the arborescent I/O block")


def _selected(item: pytest.Item) -> bool:
    if item.get_closest_marker("arbor_allow_io") is not None:
        return False
    if item.config.getoption("--arbor-io-block"):
        return True
    patterns = item.config.getini("arbor_io_free")
    if not isinstance(patterns, list):
        return False
    rel = PurePath(os.path.relpath(str(item.path), str(item.config.rootpath))).as_posix()
    return any(fnmatch.fnmatchcase(rel, pattern) for pattern in patterns)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item: pytest.Item) -> Iterator[None]:
    global _active
    if not _selected(item):
        return (yield)
    _install()
    _active = True
    try:
        return (yield)
    finally:
        _active = False
        _uninstall()
