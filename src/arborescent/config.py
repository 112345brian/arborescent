"""Locate and load the policy: ``arborescent.toml`` (top-level keys) or ``[tool.arborescent]`` in ``pyproject.toml``."""

from __future__ import annotations

import re
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from .domain import Policy, PolicyError, RingRule
from .ports import FileSystem
from .presets import DEFAULT_PRESET, EFFECT_CATEGORIES, IO_MODULES, PRESETS

__all__ = ["find_policy", "load_policy", "policy_from_mapping"]


def find_policy(fs: FileSystem, start: Path) -> Path:
    for folder in (start, *start.parents):
        for name in ("arborescent.toml", "pyproject.toml"):
            candidate = folder / name
            if fs.is_file(candidate) and (
                name == "arborescent.toml" or "arborescent" in tomllib.loads(fs.read_text(candidate)).get("tool", {})
            ):
                return candidate
    raise PolicyError(f"no arborescent.toml or [tool.arborescent] found from {start}")


def load_policy(fs: FileSystem, path: Path) -> tuple[Policy, Path]:
    """The parsed policy and the repository root (the folder holding the policy file)."""
    data = tomllib.loads(fs.read_text(path))
    raw = data if path.name == "arborescent.toml" else data.get("tool", {}).get("arborescent")
    if raw is None:
        raise PolicyError(f"{path} has no [tool.arborescent] table")
    return policy_from_mapping(raw), path.parent


def policy_from_mapping(raw: Mapping[str, Any]) -> Policy:
    """Translate the raw TOML table (``@group`` references, ``preset``, ``io_modules``, defaults) into a ``Policy``."""
    core, ring_tables = _core_policy(raw)
    preset_name, python, checkers, io_modules, disabled_tools, suggestion_threshold = _tool_options(raw)
    forbid_circular_imports = _boolean_option(raw, "forbid_circular_imports", True)
    raw_module_forbids = _module_forbids(raw)
    ruff_overrides = _ruff_overrides(raw.get("ruff", {}))
    customizations = _customization_labels(raw, ring_tables, suggestion_threshold, ruff_overrides, raw_module_forbids)
    minor = re.fullmatch(r"(\d+\.\d+)(?:\.\d+)*", python.strip())
    return replace(
        core,
        sources=tuple(raw.get("sources", ["."])),
        source_roots=tuple(raw.get("source_roots", ["."])),
        exclude=tuple(raw.get("exclude", [])),
        module_forbid_imports=tuple((pattern, tuple(targets)) for pattern, targets in raw_module_forbids.items()),
        entry_scripts=tuple(raw.get("entry_scripts", [])),
        dependencies=dict(raw.get("dependencies", {})),
        forbid_everywhere=tuple(raw.get("forbid_everywhere", [])),
        forbid_circular_imports=forbid_circular_imports,
        python=minor.group(1) if minor else python,
        type_checkers=tuple(cast("list[str]", checkers)),
        disabled_tools=disabled_tools,
        output_dir=str(raw.get("output_dir", ".")),
        baseline=str(raw.get("baseline", "arborescent-baseline.json")),
        suggestion_threshold=suggestion_threshold,
        ruff_overrides=ruff_overrides,
        customization_labels=customizations,
        practices=dict(raw.get("practices", {})),
        preset=PRESETS[preset_name],
        io_modules=frozenset(io_modules),
    )


def _core_policy(raw: Mapping[str, Any]) -> tuple[Policy, Mapping[str, Mapping[str, Any]]]:
    groups_value = raw.get("groups", {})
    if not isinstance(groups_value, Mapping):
        raise PolicyError("`groups` must map names to lists of packages")
    groups = cast(Mapping[str, Sequence[str]], groups_value)

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
    ring_rules = [_ring_from_mapping(name, ring_tables.get(name, {}), expand) for name in names]
    rings = tuple(ring_rules)
    classify = tuple((str(k), str(v)) for k, v in raw.get("classify", {}).items())
    # The ring invariants (onion rule, classify targets) are checked by Policy itself, before the options below.
    return Policy(rings=rings, classify=classify), ring_tables


def _tool_options(raw: Mapping[str, Any]) -> tuple[str, str, object, list[str], tuple[str, ...], int]:
    preset_name = raw.get("preset", DEFAULT_PRESET)
    if not isinstance(preset_name, str) or preset_name not in PRESETS:
        raise PolicyError(f"preset {preset_name!r} is unknown; choose one of {sorted(PRESETS)}")
    python = str(raw.get("python", "3.11"))  # an unquoted TOML 3.12 is a float; str() reads it back (3.10 -> "3.1": unsupported)
    checkers: object = raw.get("type_checkers", ["ty"])
    if not isinstance(checkers, list) or not checkers or not all(isinstance(c, str) and c for c in cast("list[object]", checkers)):
        raise PolicyError("`type_checkers` must be a non-empty list of type checker names")
    disabled_tools = _disabled_tools(raw)
    io_modules: object = raw["io_modules"] if "io_modules" in raw else sorted(IO_MODULES)
    if not isinstance(io_modules, list) or not all(isinstance(m, str) and m for m in cast("list[object]", io_modules)):
        raise PolicyError("`io_modules` must be a list of non-empty module names")
    suggestion_threshold = _suggestion_threshold(raw)
    return preset_name, python, checkers, cast("list[str]", io_modules), disabled_tools, suggestion_threshold


def _boolean_option(raw: Mapping[str, Any], key: str, default: bool) -> bool:
    value = raw.get(key, default)
    if not isinstance(value, bool):
        raise PolicyError(f"`{key}` must be a boolean")
    return value


def _module_forbids(raw: Mapping[str, Any]) -> Mapping[str, list[str]]:
    raw_module_forbids = raw.get("module_forbid_imports", {})
    if not isinstance(raw_module_forbids, Mapping) or any(
        not isinstance(pattern, str) or not pattern or not isinstance(targets, list)
        or not all(isinstance(target, str) and target for target in targets)
        for pattern, targets in raw_module_forbids.items()
    ):
        raise PolicyError("`module_forbid_imports` must map module globs to lists of internal module names")
    return cast("Mapping[str, list[str]]", raw_module_forbids)


def _customization_labels(
    raw: Mapping[str, Any], ring_tables: Mapping[str, Mapping[str, Any]], suggestion_threshold: int,
    ruff_overrides: Mapping[str, Any], raw_module_forbids: Mapping[str, list[str]],
) -> tuple[str, ...]:
    customizations: list[str] = []
    if ruff_overrides:
        customizations.append("ruff")
    if any("forbid" in table for table in ring_tables.values()):
        customizations.append("forbid")
    if suggestion_threshold != 300 or any(key.startswith("max-") for key in ruff_overrides):
        customizations.append("thresholds")
    if raw.get("preset", DEFAULT_PRESET) != DEFAULT_PRESET:
        customizations.append("preset")
    if "exclude" in raw and raw.get("exclude"):
        customizations.append("exclude")
    if raw_module_forbids:
        customizations.append("module import restrictions")
    if not raw.get("forbid_circular_imports", True):
        customizations.append("circular imports")
    return tuple(customizations)


def _disabled_tools(raw: Mapping[str, Any]) -> tuple[str, ...]:
    value = raw.get("disabled_tools", [])
    if not isinstance(value, list) or not all(isinstance(tool, str) and tool for tool in cast("list[object]", value)):
        raise PolicyError("`disabled_tools` must be a list of non-empty tool names")
    return tuple(cast("list[str]", value))


def _ruff_overrides(raw: object) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise PolicyError("`ruff` must be a table of generated Ruff overrides")
    allowed = {"extend-select", "ignore", "per-file-ignores", "max-complexity", "max-args", "max-branches", "max-statements"}
    unknown = set(raw) - allowed
    if unknown:
        raise PolicyError(f"unknown `[tool.arborescent.ruff]` setting(s): {sorted(unknown)}")
    for key in ("extend-select", "ignore"):
        value = raw.get(key, [])
        if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
            raise PolicyError(f"`{key}` in `[tool.arborescent.ruff]` must be a list of non-empty rule names")
    per_file = raw.get("per-file-ignores", {})
    if not isinstance(per_file, Mapping) or any(
        not isinstance(pattern, str) or not pattern or not isinstance(codes, list)
        or not all(isinstance(code, str) and code for code in codes)
        for pattern, codes in per_file.items()
    ):
        raise PolicyError("`per-file-ignores` in `[tool.arborescent.ruff]` must map file globs to rule lists")
    for key in ("max-complexity", "max-args", "max-branches", "max-statements"):
        if key in raw and (not isinstance(raw[key], int) or isinstance(raw[key], bool) or raw[key] < 1):
            raise PolicyError(f"`{key}` in `[tool.arborescent.ruff]` must be a positive integer")
    return dict(raw)


def _ring_from_mapping(
    name: str,
    table: Mapping[str, Any],
    expand: Callable[[Sequence[str]], frozenset[str]],
) -> RingRule:
    has_forbid = "forbid" in table
    raw_forbid = table.get("forbid")
    if has_forbid and (
        not isinstance(raw_forbid, list)
        or not all(isinstance(item, str) and item for item in cast("list[object]", raw_forbid))
    ):
        raise PolicyError(f"`forbid` in [ring.{name}] must be a list of non-empty effect categories")
    forbid = expand(cast("Sequence[str]", raw_forbid)) if has_forbid else None
    if forbid is not None:
        unknown_effects = forbid - EFFECT_CATEGORIES
        if unknown_effects:
            raise PolicyError(f"unknown effect category in [ring.{name}].forbid: {sorted(unknown_effects)}")
    return RingRule(
        name,
        expand(table.get("may_import", [])),
        expand(table.get("external", [])),
        bool(table.get("os_branching", False)),
        forbid,
    )


def _suggestion_threshold(raw: Mapping[str, Any]) -> int:
    value = raw.get("suggestion_threshold", 300)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise PolicyError("`suggestion_threshold` must be a positive integer")
    return value
