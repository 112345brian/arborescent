"""Warm equals cold: every cached fact, and every field of every record inside one, survives the scan cache.

``facts_from_json`` builds the records positionally, so a field that is added to a dataclass and not to the codec is
silently its default on a warm run only. The sample below is checked to give every field a non-default value somewhere,
which is what makes the equality assertion mean something."""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from arborescent_helpers import mini_policy, write_files

from arborescent.adapters import FsScanner
from arborescent.facts import SourceFacts, extract
from arborescent.scan_cache_codec import facts_from_json, facts_to_json

SAMPLE = '''# /// script
# dependencies = ["requests"]
# ///
"""A module that gives every fact something to record."""

from __future__ import annotations

import abc
import dataclasses
import json
import logging
import os
import random
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple, Protocol

import pytest
from unittest import mock

from . import sibling
from .sibling import (
    helper,  # deferred: not really, this one records a wrapped import
)

if TYPE_CHECKING:
    from .typing_only import Thing

__arbor__ = {"ring": "domain", "role": "dev_tool", "ring_reason": "because"}
__all__ = ["helper", "run", "Port"]
NOW = time.time()
CACHE: dict[str, int] = {}
NAME = "SOME_ENV_NAME"
HERE = Path(__file__)


class Port(Protocol):
    def get(self) -> Any: ...


class Base(abc.ABC):
    @abc.abstractmethod
    def go(self) -> None: ...


@dataclass(frozen=True)
class Model:
    one: int
    two: int = field(default=0, metadata={"arbor": "derived"})
    three: int = field(default=0, kw_only=True)


class Built(metaclass=Meta):
    def __init__(self, a, *, b=1, **rest) -> None:
        self.a = a


def forward(x):
    return helper(x)


def member_forward(x):
    return json.loads(x)


logging.warning("at import")


def use_port(port: Port) -> None:
    port.get()


def fallback(now=None):
    if now is None:
        now = time.time()
    return now


LOCK = threading.Lock()


@guarded
def write_state(key):
    with other_lock():
        CACHE[key] = 2


def run_result() -> RunResult:
    from json import dumps  # deferred: heavy
    helper(dumps)
    discard(helper(1), reason="why")
    sibling._private.thing()
    return RunResult()


def register(key: str, clock=time.time) -> None:
    CACHE[key] = 1
    global counter
    counter = 1
    value = time.time()
    Model(1, 2, three=3)
    Model(*(1,), **extra)
    replaced = Model(1)
    dataclasses.replace(replaced, two=3)
    __import__("os")
    import_module("json")
    helper(value)
    value.record_outcome()


def timed(clock: Clock):
    if clock:
        return time.time()
    return clock.now()


class Service:
    def __init__(self, rng: random.Random) -> None:
        self.rng = rng

    def pick(self):
        return random.random()


def boot():
    register("a")


boot()


def branchy(key: str, path: Path) -> None:
    path.with_suffix(".tmp").replace(path)
    if key:
        helper(key)
    try:
        helper(key)
    except ValueError:
        return
    if key:
        key = ""


# companion-exempt: exercised only
def exempted():
    helper(1)


def test_it(monkeypatch):
    monkeypatch.setattr("app.domain.helper", None)
    with mock.patch("app.domain.other"):
        pass


x = 1  # noqa: E501

if __name__ == "__main__":
    register("main")
'''


def _records(value: object) -> Iterator[Any]:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        yield value
        for item in dataclasses.fields(value):
            yield from _records(getattr(value, item.name))
    elif isinstance(value, tuple | list):
        for item in value:
            yield from _records(item)


def _default(item: dataclasses.Field[Any]) -> object:
    if item.default is not dataclasses.MISSING:
        return item.default
    if item.default_factory is not dataclasses.MISSING:
        return item.default_factory()
    return dataclasses.MISSING


def _roundtrip(facts: SourceFacts) -> SourceFacts:
    return facts_from_json(json.loads(json.dumps(facts_to_json(facts))))


# set only by a file `ast` accepts and `tokenize` does not
_UNREACHABLE_BY_THE_SAMPLE = {("SourceFacts", "arbor_error"), ("SourceFacts", "untokenizable")}


def test_every_field_of_every_cached_record_survives_the_codec() -> None:
    facts = extract(SAMPLE, "sample.py")
    assert _roundtrip(facts) == facts
    seen: dict[tuple[str, str], bool] = {}
    for record in _records(facts):
        for item in dataclasses.fields(record):
            key = (type(record).__name__, item.name)
            value, default = getattr(record, item.name), _default(item)
            seen[key] = seen.get(key, False) or (bool(value) if default is dataclasses.MISSING else value != default)
    unexercised = sorted(key for key, used in seen.items() if not used and key not in _UNREACHABLE_BY_THE_SAMPLE)
    assert unexercised == [], "give these a value in SAMPLE, or the roundtrip says nothing about them"


def test_the_two_fields_the_sample_cannot_set_survive_too() -> None:
    facts = dataclasses.replace(extract("x = 1\n"), arbor_error="bad annotation", untokenizable="1: doubled CR")
    assert _roundtrip(facts) == facts


def test_a_warm_scan_returns_the_facts_of_the_cold_one(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    write_files(repo, {"app/__init__.py": "", "app/domain.py": SAMPLE, "app/sibling.py": "def helper(x): return x\n"})
    scanner = FsScanner(tmp_path / "cache")
    cold = scanner.scan(mini_policy(), repo)
    warm = scanner.scan(mini_policy(), repo)
    assert warm.parsed == 0 < cold.parsed
    assert {name: info.facts for name, info in warm.modules.items()} == {name: info.facts for name, info in cold.modules.items()}
