"""Locate and load the policy: ``archlint.toml`` (top-level keys) or ``[tool.archlint]`` in ``pyproject.toml``."""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from .domain import Policy, PolicyError, RingRule
from .ports import FileSystem
from .presets import DEFAULT_PRESET, IO_MODULES, PRESETS

__all__ = ["find_policy", "load_policy", "policy_from_mapping"]


def find_policy(fs: FileSystem, start: Path) -> Path:
    for folder in (start, *start.parents):
        for name in ("archlint.toml", "pyproject.toml"):
            candidate = folder / name
            if fs.is_file(candidate) and (
                name == "archlint.toml" or "archlint" in tomllib.loads(fs.read_text(candidate)).get("tool", {})
            ):
                return candidate
    raise PolicyError(f"no archlint.toml or [tool.archlint] found from {start}")


def load_policy(fs: FileSystem, path: Path) -> tuple[Policy, Path]:
    """The parsed policy and the repository root (the folder holding the policy file)."""
    data = tomllib.loads(fs.read_text(path))
    raw = data if path.name == "archlint.toml" else data.get("tool", {}).get("archlint")
    if raw is None:
        raise PolicyError(f"{path} has no [tool.archlint] table")
    return policy_from_mapping(raw), path.parent


def policy_from_mapping(raw: Mapping[str, Any]) -> Policy:
    """Translate the raw TOML table (``@group`` references, ``preset``, ``io_modules``, defaults) into a ``Policy``."""
    groups: Mapping[str, Sequence[str]] = raw.get("groups", {})

    def expand(items: Sequence[str]) -> frozenset[str]:
        out: set[str] = set()
        for item in items:
            if item.startswith("@"):
                if item[1:] not in groups:
                    raise PolicyError(f"unknown group {item!r}")
                out.update(groups[item[1:]])
            else:
                out.add(item)
        return frozenset(out)

    names = list(raw.get("rings", []))  # distinct and non-empty: checked by Policy
    ring_tables: Mapping[str, Mapping[str, Any]] = raw.get("ring", {})
    for unknown in set(ring_tables) - set(names):
        raise PolicyError(f"[ring.{unknown}] is not in `rings`")
    rings = tuple(
        RingRule(
            name,
            expand(ring_tables.get(name, {}).get("may_import", [])),
            expand(ring_tables.get(name, {}).get("external", [])),
            bool(ring_tables.get(name, {}).get("os_branching", False)),
        )
        for name in names
    )
    classify = tuple((str(k), str(v)) for k, v in raw.get("classify", {}).items())
    # The ring invariants (onion rule, classify targets) are checked by Policy itself, before the options below.
    core = Policy(rings=rings, classify=classify)
    preset_name = raw.get("preset", DEFAULT_PRESET)
    if not isinstance(preset_name, str) or preset_name not in PRESETS:
        raise PolicyError(f"preset {preset_name!r} is unknown; choose one of {sorted(PRESETS)}")
    python = str(raw.get("python", "3.11"))  # an unquoted TOML 3.12 is a float; str() reads it back (3.10 -> "3.1": unsupported)
    minor = re.fullmatch(r"(\d+\.\d+)(?:\.\d+)*", python.strip())  # "3.12.1" -> "3.12"
    checkers: object = raw.get("type_checkers", ["pyright"])
    if not isinstance(checkers, list) or not checkers or not all(isinstance(c, str) and c for c in cast("list[object]", checkers)):
        raise PolicyError("`type_checkers` must be a non-empty list of type checker names")
    io_modules: object = raw["io_modules"] if "io_modules" in raw else sorted(IO_MODULES)
    if not isinstance(io_modules, list) or not all(isinstance(m, str) and m for m in cast("list[object]", io_modules)):
        raise PolicyError("`io_modules` must be a list of non-empty module names")
    return replace(
        core,
        sources=tuple(raw.get("sources", ["."])),
        source_roots=tuple(raw.get("source_roots", ["."])),
        exclude=tuple(raw.get("exclude", [])),
        entry_scripts=tuple(raw.get("entry_scripts", [])),
        dependencies=dict(raw.get("dependencies", {})),
        forbid_everywhere=tuple(raw.get("forbid_everywhere", [])),
        python=minor.group(1) if minor else python,
        type_checkers=tuple(cast("list[str]", checkers)),
        output_dir=str(raw.get("output_dir", ".")),
        baseline=str(raw.get("baseline", "archlint-baseline.json")),
        practices=dict(raw.get("practices", {})),
        preset=PRESETS[preset_name],
        io_modules=frozenset(cast("list[str]", io_modules)),
    )
