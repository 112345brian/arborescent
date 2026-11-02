"""The version in the package, in pyproject.toml and in the newest changelog heading must agree (it was stale once)."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import arborescent

ROOT = Path(__file__).resolve().parent.parent


def test_dunder_version_matches_pyproject() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert arborescent.__version__ == project["version"]


def test_the_newest_released_changelog_heading_is_the_current_version() -> None:
    headings = re.findall(r"^## (\d+\.\d+\.\d+) — ", (ROOT / "CHANGELOG.md").read_text(encoding="utf-8"), re.MULTILINE)
    assert headings[0] == arborescent.__version__
