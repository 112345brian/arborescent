"""Fixtures. The autouse guard keeps the developer's real secrets out of every test (practice: secrets-guard)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from archlint_helpers import MINI_FILES, write_files

_SECRET_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")


@pytest.fixture(autouse=True)
def _scrub_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if any(marker in name.upper() for marker in _SECRET_MARKERS):
            monkeypatch.delenv(name)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    write_files(tmp_path, MINI_FILES)
    return tmp_path
