"""Locate and load the policy: ``arborescent.toml`` (top-level keys) or ``[tool.arborescent]`` in ``pyproject.toml``."""

from __future__ import annotations

import re
import tomllib
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from .domain import ArchitectureScope, ChildArbor, Policy, PolicyError, RingRule
from .ports import FileSystem
from .presets import DEFAULT_PRESET, EFFECT_CATEGORIES, IO_MODULES, PRESETS, Preset

__all__ = ["find_child_policy", "find_policy", "load_policy", "policy_from_mapping"]


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
    derived = _with_derived_roots(fs, path.parent, _with_derived_classify(fs, path.parent, raw))
    return policy_from_mapping(derived), path.parent


_ROOT_KEYS = ("vendor_roots", "library_roots")


def _with_derived_roots(fs: FileSystem, root: Path, raw: Mapping[str, Any]) -> Mapping[str, Any]:
    """Expand `vendor_roots` / `library_roots` into `sources` and `source_roots`.

    Every child folder `X` of a declared root is one package tree: `<root>/X/src` when that exists, else `<root>/X`.
    Explicit `sources` / `source_roots` entries stay first; derived ones are appended without duplicates.
    """
    declared = [key for key in _ROOT_KEYS if key in raw]
    if not declared:
        return raw
    if "scopes" in raw:
        raise PolicyError(f"`{declared[0]}` cannot be combined with `scopes`; list each scope's source roots instead")
    derived: list[str] = []
    for key in declared:
        for folder in _string_list(raw, key):
            base = root / folder
            if not fs.is_dir(base):
                raise PolicyError(f"`{key}` entry {folder!r} is not a directory")
            for child in fs.glob(base, "*"):
                if not fs.is_dir(child) or child.name.startswith((".", "_")):
                    continue
                tree = child / "src" if fs.is_dir(child / "src") else child
                derived.append(tree.relative_to(root).as_posix())
    merged = {key: _with_paths(raw, key, derived) for key in ("sources", "source_roots")}
    return {**raw, **merged}


def _with_paths(raw: Mapping[str, Any], key: str, extra: list[str]) -> list[str]:
    explicit = raw.get(key, ["."])
    if not isinstance(explicit, list):
        raise PolicyError(f"`{key}` must be a list of paths")
    return list(dict.fromkeys([*explicit, *extra]))


def _with_derived_classify(fs: FileSystem, root: Path, raw: Mapping[str, Any]) -> Mapping[str, Any]:
    """Expand `classify_from` (top level and per scope) into `classify` entries read from a tach layer file."""
    scopes = raw.get("scopes")
    if isinstance(scopes, Mapping):
        raw = {**raw, "scopes": {
            name: _with_derived_classify(fs, root, scope) if isinstance(scope, Mapping) else scope
            for name, scope in scopes.items()
        }}
    if "classify_from" not in raw:
        return raw
    source = raw["classify_from"]
    if not isinstance(source, str) or not source:
        raise PolicyError("`classify_from` must be a path to a tach.toml file")
    explicit = raw.get("classify", {})
    if not isinstance(explicit, Mapping):
        raise PolicyError("`classify` must map module globs to rings")
    layers, unlayered = _tach_classify(fs, root / source, source)
    derived = {pattern: ring for pattern, ring in layers.items() if pattern not in explicit}
    explicit_rings = {pattern: ring for pattern, ring in explicit.items() if isinstance(ring, str)}
    inferred = _shared_parent_rings({**layers, **explicit_rings}, unlayered)  # an explicit descendant's ring counts as one of the children's
    derived.update({package: ring for package, ring in inferred.items() if package not in explicit and package not in layers})
    return {**{k: v for k, v in raw.items() if k != "classify_from"}, "classify": {**explicit, **derived}}


def _tach_classify(fs: FileSystem, path: Path, label: str) -> tuple[dict[str, str], set[str]]:
    """Each tach `[[modules]]` with a `layer` becomes `path` and `path.*` entries, mirroring tach's own layers.

    Also returns the paths of tach modules that have no layer: a package holding one is not wholly classified."""
    try:
        data = tomllib.loads(fs.read_text(path))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise PolicyError(f"`classify_from` could not read {label}: {error}") from error
    out: dict[str, str] = {}
    unlayered: set[str] = set()
    for module in data.get("modules", []):
        module_path, layer = module.get("path"), module.get("layer")
        if not isinstance(module_path, str) or not module_path:
            continue
        if isinstance(layer, str) and layer:
            out[module_path] = layer
            out[module_path + ".*"] = layer
        else:
            unlayered.add(module_path)
    return out, unlayered


def _shared_parent_rings(rings: Mapping[str, str], unlayered: Collection[str]) -> dict[str, str]:
    """A package nobody names gets the one ring all of its classified descendants share (its `__init__`).

    ``rings`` maps module patterns to rings (globs are ignored); a package with any descendant that has no ring, or with
    descendants in two rings, is left out: then the choice is the author's."""
    below: dict[str, set[str]] = {}
    for module_path, ring in rings.items():
        if "*" not in module_path:
            for package in _parent_packages(module_path):
                below.setdefault(package, set()).add(ring)
    blocked = {package for module_path in unlayered for package in _parent_packages(module_path)}
    return {package: next(iter(found)) for package, found in below.items() if len(found) == 1 and package not in blocked}


def _parent_packages(module_path: str) -> list[str]:
    parts = module_path.split(".")
    return [".".join(parts[:end]) for end in range(1, len(parts))]


def find_child_policy(fs: FileSystem, parent_root: Path, child_path: str) -> Path | None:
    """Find a direct child policy, or None so the parent keeps governing that directory."""
    child_root = parent_root / child_path
    if not fs.is_dir(child_root):
        raise PolicyError(f"child arbor {child_path!r} is not a directory")
    try:
        parent_resolved = fs.resolve(parent_root)
        child_resolved = fs.resolve(child_root)
    except OSError as exc:
        raise PolicyError(f"child arbor {child_path!r} cannot be resolved: {exc}") from exc
    if child_resolved == parent_resolved or parent_resolved not in child_resolved.parents:
        raise PolicyError(f"child arbor {child_path!r} resolves outside the parent root")
    arborescent = child_root / "arborescent.toml"
    if fs.is_file(arborescent):
        return arborescent
    pyproject = child_root / "pyproject.toml"
    if fs.is_file(pyproject):
        try:
            data = tomllib.loads(fs.read_text(pyproject))
        except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
            raise PolicyError(f"child arbor {child_path!r} has an invalid pyproject.toml: {exc}") from exc
        if isinstance(data.get("tool"), Mapping) and "arborescent" in data["tool"]:
            return pyproject
    return None


def policy_from_mapping(raw: Mapping[str, Any]) -> Policy:
    """Translate the raw TOML table (``@group`` references, ``preset``, ``io_modules``, defaults) into a ``Policy``."""
    if "scopes" in raw:
        return _scoped_policy(raw)
    core, ring_tables = _core_policy(raw)
    preset_name, python, checkers, io_modules, disabled_tools, suggestion_threshold = _tool_options(raw)
    forbid_circular_imports = _boolean_option(raw, "forbid_circular_imports", True)
    require_arbor_annotations = _boolean_option(raw, "require_arbor_annotations", False)
    require_arbor_annotations_exclude = _string_list(raw, "require_arbor_annotations_exclude")
    raw_module_forbids = _module_forbids(raw)
    ruff_overrides = _ruff_overrides(raw.get("ruff", {}))
    customizations = _customization_labels(raw, ring_tables, suggestion_threshold, ruff_overrides, raw_module_forbids)
    minor = re.fullmatch(r"(\d+\.\d+)(?:\.\d+)*", python.strip())
    return replace(
        core,
        sources=tuple(raw.get("sources", ["."])),
        source_roots=tuple(raw.get("source_roots", ["."])),
        exclude=tuple(raw.get("exclude", [])),
        scan_vendors=_boolean_option(raw, "scan_vendors", False),
        scan_vendor_paths=_string_list(raw, "vendor_roots"),
        child_policy_mode=_child_policy_mode(raw),
        module_forbid_imports=tuple((pattern, tuple(targets)) for pattern, targets in raw_module_forbids.items()),
        entry_scripts=tuple(raw.get("entry_scripts", [])),
        dependencies=dict(raw.get("dependencies", {})),
        forbid_everywhere=tuple(raw.get("forbid_everywhere", [])),
        forbidden_modules=_string_list(raw, "forbidden_modules"),
        dev_tools=_dev_tool_paths(raw),
        dev_tools_forbid=_effect_list(raw, "dev_tools_forbid", ("process",)),
        dev_tools_may_import=_string_list(raw, "dev_tools_may_import"),
        effect_defaults=_effect_defaults(raw),
        tach_external_exclude=tuple(raw.get("tach_external_exclude", [])),
        forbid_circular_imports=forbid_circular_imports,
        require_arbor_annotations=require_arbor_annotations,
        require_arbor_annotations_exclude=require_arbor_annotations_exclude,
        python=minor.group(1) if minor else python,
        type_checkers=tuple(cast("list[str]", checkers)),
        disabled_tools=disabled_tools,
        output_dir=str(raw.get("output_dir", ".")),
        baseline=str(raw.get("baseline", "arborescent-baseline.json")),
        suggestion_threshold=suggestion_threshold,
        ruff_overrides=ruff_overrides,
        customization_labels=customizations,
        practices=dict(raw.get("practices", {})),
        package_catalog=_package_catalog(raw),
        package_parts=_package_parts(raw),
        package_pyproject=_package_pyproject(raw),
        modularity=_modularity(raw),
        children=_child_arbors(raw),
        preset=_preset_with_middle_forbid(PRESETS[preset_name], raw),
        io_modules=frozenset(io_modules),
    )


_SCOPED_ROOT_GLOBALS = frozenset({
    "python", "type_checkers", "disabled_tools", "output_dir", "baseline", "suggestion_threshold", "children",
    "scan_vendors",
    "child_policy_mode",
    "ruff", "practices", "exclude", "package_catalog", "package_parts", "package_pyproject",
    "modularity", "dev_tools", "dev_tools_forbid", "dev_tools_may_import",
})
_LEGACY_ARCHITECTURE_KEYS = frozenset({
    "sources", "source_roots", "rings", "classify", "ring", "preset", "middle_forbid", "io_modules", "groups",
    "module_forbid_imports", "entry_scripts", "dependencies", "forbid_everywhere", "forbid_circular_imports",
    "forbidden_modules", "tach_external_exclude", "require_arbor_annotations", "require_arbor_annotations_exclude",
    "effect_defaults",
})
_SCOPE_ARCHITECTURE_KEYS = _LEGACY_ARCHITECTURE_KEYS - {"sources"}


def _scoped_policy(raw: Mapping[str, Any]) -> Policy:
    legacy = _LEGACY_ARCHITECTURE_KEYS & raw.keys()
    if legacy:
        raise PolicyError(f"root-level architecture keys cannot be combined with `scopes`: {sorted(legacy)}")
    raw_scopes = raw.get("scopes")
    if not isinstance(raw_scopes, Mapping) or not raw_scopes:
        raise PolicyError("`scopes` must map non-empty scope names to tables")
    scopes: list[ArchitectureScope] = []
    for name, raw_scope in raw_scopes.items():
        if not isinstance(name, str) or not name:
            raise PolicyError("scope names must be non-empty strings")
        if not isinstance(raw_scope, Mapping):
            raise PolicyError(f"scope {name!r} must be a table")
        missing = {"sources", "source_roots", "rings"} - raw_scope.keys()
        if missing:
            raise PolicyError(f"scope {name!r} requires {sorted(missing)}")
        public = _scope_strings(raw_scope, "public")
        depends_on = _scope_strings(raw_scope, "depends_on")
        child_raw = {
            key: value for key, value in raw.items() if key in _SCOPED_ROOT_GLOBALS and key != "children"
        }
        child_raw.update({key: value for key, value in raw_scope.items() if key in _SCOPE_ARCHITECTURE_KEYS})
        child_raw["sources"] = raw_scope["sources"]
        child_raw["source_roots"] = raw_scope["source_roots"]
        policy = policy_from_mapping(child_raw)
        for key, value in (("sources", raw_scope["sources"]), ("source_roots", raw_scope["source_roots"])):
            if not isinstance(value, list) or not all(isinstance(path, str) and path for path in value):
                raise PolicyError(f"scope {name!r} `{key}` must be a list of non-empty paths")
        scopes.append(ArchitectureScope(name, policy, public, depends_on))

    first = scopes[0].policy
    return replace(
        first,
        rings=(),
        classify=(),
        sources=tuple(path for scope in scopes for path in scope.policy.sources),
        source_roots=tuple(path for scope in scopes for path in scope.policy.source_roots),
        module_forbid_imports=(),
        entry_scripts=tuple(script for scope in scopes for script in scope.policy.entry_scripts),
        dependencies={},
        forbid_everywhere=(),
        forbidden_modules=(),
        tach_external_exclude=(),
        require_arbor_annotations=False,
        require_arbor_annotations_exclude=(),
        customization_labels=tuple(sorted({
            label for scope in scopes for label in scope.policy.customization_labels
        })),
        children=_child_arbors(raw),
        scan_vendors=_boolean_option(raw, "scan_vendors", False),
        child_policy_mode=_child_policy_mode(raw),
        scopes=tuple(scopes),
    )


def _scope_strings(raw: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = raw.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise PolicyError(f"scope `{key}` must be a list of non-empty strings")
    if len(value) != len(set(value)):
        raise PolicyError(f"scope `{key}` must not contain duplicates")
    return tuple(cast("list[str]", value))


def _child_arbors(raw: Mapping[str, Any]) -> tuple[ChildArbor, ...]:
    value = raw.get("children", [])
    if not isinstance(value, list) or not all(isinstance(path, str) and path for path in value):
        raise PolicyError("`children` must be a list of non-empty relative package paths")
    return tuple(ChildArbor(path) for path in value)


def _child_policy_mode(raw: Mapping[str, Any]) -> str:
    value = raw.get("child_policy_mode", "certify")
    if not isinstance(value, str) or value not in {"certify", "parent"}:
        raise PolicyError("`child_policy_mode` must be 'certify' or 'parent'")
    return value


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


def _preset_with_middle_forbid(preset: Preset, raw: Mapping[str, Any]) -> Preset:
    """The preset, with ``middle_forbid`` replaced when the policy sets it, so effects can be enforced under any preset."""
    if "middle_forbid" not in raw:
        return preset
    value: object = raw["middle_forbid"]
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in cast("list[object]", value)):
        raise PolicyError("`middle_forbid` must be a list of non-empty effect categories")
    categories = frozenset(cast("list[str]", value))
    unknown = categories - EFFECT_CATEGORIES
    if unknown:
        raise PolicyError(f"unknown effect category in `middle_forbid`: {sorted(unknown)}")
    return replace(preset, middle_forbid=categories)


def _boolean_option(raw: Mapping[str, Any], key: str, default: bool) -> bool:
    value = raw.get(key, default)
    if not isinstance(value, bool):
        raise PolicyError(f"`{key}` must be a boolean")
    return value


def _string_list(raw: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = raw.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in cast("list[object]", value)):
        raise PolicyError(f"`{key}` must be a list of non-empty strings")
    return tuple(cast("list[str]", value))


def _effect_defaults(raw: Mapping[str, Any]) -> str:
    value = raw.get("effect_defaults", "injection")
    if value not in ("injection", "typed", "count"):
        raise PolicyError("`effect_defaults` must be \"injection\", \"typed\" or \"count\"")
    return str(value)


def _effect_list(raw: Mapping[str, Any], key: str, default: tuple[str, ...]) -> tuple[str, ...]:
    if key not in raw:
        return default
    categories = _string_list(raw, key)
    unknown = set(categories) - EFFECT_CATEGORIES
    if unknown:
        raise PolicyError(f"unknown effect category in `{key}`: {sorted(unknown)}")
    return categories


def _dev_tool_paths(raw: Mapping[str, Any]) -> tuple[str, ...]:
    """`dev_tools` entries as canonical repository-relative paths: `./tools/` becomes `tools`; `.`, `..` and absolute paths are refused."""
    out: list[str] = []
    for entry in _string_list(raw, "dev_tools"):
        parts = [part for part in entry.replace("\\", "/").split("/") if part not in ("", ".")]
        if ".." in parts or not parts or entry.startswith(("/", "\\")) or (len(entry) > 1 and entry[1] == ":"):
            raise PolicyError(f"`dev_tools` entry {entry!r} must be a folder or file below the repository root (no `.`, `..` or absolute path)")
        out.append("/".join(parts))
    return tuple(out)


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
    if "middle_forbid" in raw or any("forbid" in table for table in ring_tables.values()):
        customizations.append("forbid")
    if suggestion_threshold != 300 or any(key.startswith("max-") for key in ruff_overrides):
        customizations.append("thresholds")
    if raw.get("preset", DEFAULT_PRESET) != DEFAULT_PRESET:
        customizations.append("preset")
    if "exclude" in raw and raw.get("exclude"):
        customizations.append("exclude")
    if raw_module_forbids:
        customizations.append("module import restrictions")
    customizations.extend(_flag_labels(raw))
    return tuple(customizations)


def _flag_labels(raw: Mapping[str, Any]) -> list[str]:
    """Customization labels driven by a single boolean (or boolean-like) top-level key each."""
    labels: list[str] = []
    if raw.get("children") or raw.get("child_policy_mode", "certify") != "certify":
        labels.append("nested arbors")
    if raw.get("scan_vendors", False):
        labels.append("vendor scanning")
    if not raw.get("forbid_circular_imports", True):
        labels.append("circular imports")
    if raw.get("require_arbor_annotations", False):
        labels.append("annotations required")
    return labels


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
        bool(table.get("require_abstract", False)),
    )


def _suggestion_threshold(raw: Mapping[str, Any]) -> int:
    value = raw.get("suggestion_threshold", 300)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise PolicyError("`suggestion_threshold` must be a positive integer")
    return value


def _package_catalog(raw: Mapping[str, Any]) -> str | None:
    value = raw.get("package_catalog")
    if value is not None and not isinstance(value, str):
        raise PolicyError("`package_catalog` must be a relative path to a packages.toml file")
    return value


def _package_parts(raw: Mapping[str, Any]) -> tuple[str, ...]:
    value: object = raw.get("package_parts", [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise PolicyError("`package_parts` must be a list of relative paths to extracted part trees")
    return tuple(item for item in value if isinstance(item, str))

def _package_pyproject(raw: Mapping[str, Any]) -> bool:
    value = raw.get("package_pyproject", False)
    if not isinstance(value, bool):
        raise PolicyError("`package_pyproject` must be a boolean")
    return value


def _modularity(raw: Mapping[str, Any]) -> str:
    value = raw.get("modularity", "ready")
    if not isinstance(value, str):
        raise PolicyError("`modularity` must be \"ready\" or \"report\"")
    return value
