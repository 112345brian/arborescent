"""Machine-specific migration input: where a package's new repository is checked out on this machine.

``home.repository`` is the committed, shareable identity of a repository (``org/name`` or a URL). A directory on one
machine is not a fact about the code, so it never enters the catalog, the export, a derived file or an engine contract.
It lives in a git-ignored local config that only ``packages migrate`` reads:

* the file is TOML with one table, ``[targets]``, mapping a package id to a directory; a relative directory resolves
  against the config file's own folder, not the current directory;
* it is found through the ``ARBORESCENT_LOCAL_CONFIG`` environment variable, else the same variable in a ``.env`` at the
  repository root, else ``.arborescent/local.toml`` there. The variable may point outside the worktree, so the file
  survives branch switches, clean checkouts and linked worktrees.

This module is pure: files are read through the ``FileSystem`` port, and the environment arrives as a mapping.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .domain import PolicyError
from .ports import FileSystem

__all__ = [
    "CONFIG_VARIABLE",
    "DEFAULT_CONFIG",
    "ChosenTarget",
    "ConfigLocation",
    "LocalTargets",
    "LocalTargetsError",
    "choose_target",
    "load_local_targets",
    "locate_config",
    "missing_target_message",
    "read_dotenv_value",
]

CONFIG_VARIABLE = "ARBORESCENT_LOCAL_CONFIG"
DEFAULT_CONFIG = Path(".arborescent") / "local.toml"
VIA_ENVIRONMENT = f"the {CONFIG_VARIABLE} environment variable"
VIA_DOTENV = f"the {CONFIG_VARIABLE} variable in .env"
VIA_DEFAULT = "the default location"


class LocalTargetsError(PolicyError):
    """The local config is missing where it was asked for, or malformed. Never ignored."""


@dataclass(frozen=True)
class ConfigLocation:
    path: Path
    via: str
    explicit: bool  # named by the environment or .env, so its absence is an error


@dataclass(frozen=True)
class LocalTargets:
    location: ConfigLocation
    directories: Mapping[str, Path] = field(default_factory=dict)  # package id -> directory, relative ones already resolved


@dataclass(frozen=True)
class ChosenTarget:
    path: Path
    source: str  # printed by the command so the user sees where the target came from


def read_dotenv_value(text: str, key: str) -> str | None:
    """The value of ``key`` in ``.env`` text: comments and blank lines skipped, quotes removed, the last assignment wins.

    Nothing else in the file is read or kept; other variables may be secrets.
    """
    found: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("export ").lstrip()
        name, sep, value = line.partition("=")
        if not sep or name.strip() != key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].strip()
        found = value or None
    return found


def locate_config(root: Path, environ: Mapping[str, str], fs: FileSystem) -> ConfigLocation:
    """Where the local config is: the environment variable, else ``.env`` at the repository root, else the default."""
    value = environ.get(CONFIG_VARIABLE, "").strip()
    via = VIA_ENVIRONMENT
    if not value and fs.is_file(root / ".env"):
        value = read_dotenv_value(fs.read_text(root / ".env"), CONFIG_VARIABLE) or ""
        via = VIA_DOTENV
    if value:
        path = Path(value)
        return ConfigLocation(path if path.is_absolute() else root / path, via, True)
    return ConfigLocation(root / DEFAULT_CONFIG, VIA_DEFAULT, False)


def load_local_targets(fs: FileSystem, location: ConfigLocation, known_ids: Sequence[str]) -> LocalTargets:
    """The package-to-directory table. An absent default file is no config; anything else wrong is a named error."""
    path = location.path
    if not fs.is_file(path):
        if location.explicit:
            raise LocalTargetsError(f"{path} does not exist; it was named by {location.via}")
        return LocalTargets(location)
    return LocalTargets(location, _directories(path, _targets_table(path, fs.read_text(path)), known_ids))


def _targets_table(path: Path, text: str) -> dict[str, object]:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise LocalTargetsError(f"{path} is not valid TOML: {exc}") from exc
    unknown = sorted(set(data) - {"targets"})
    if unknown:
        raise LocalTargetsError(f"{path} has unknown key(s) {', '.join(map(repr, unknown))}; the only table is [targets]")
    table: object = data.get("targets", {})
    if not isinstance(table, dict):
        raise LocalTargetsError(f"{path}: `targets` must be a table mapping a package id to a directory")
    return {str(key): value for key, value in table.items()}


def _directories(path: Path, table: dict[str, object], known_ids: Sequence[str]) -> dict[str, Path]:
    directories: dict[str, Path] = {}
    for package_id, value in sorted(table.items()):
        if not isinstance(value, str) or not value.strip():
            raise LocalTargetsError(f"{path}: `{package_id}` must be a non-empty directory string")
        if package_id not in known_ids:
            raise LocalTargetsError(
                f"{path}: unknown package {package_id!r} under [targets]; the catalog has: {', '.join(known_ids) or 'none'}"
            )
        directory = Path(value)
        directories[package_id] = directory if directory.is_absolute() else path.parent / directory
    return directories


def choose_target(package_id: str, flag: Path | None, targets: LocalTargets | None) -> ChosenTarget | None:
    """The flag first, then the local config. ``None`` when neither names a directory, so the caller can explain."""
    if flag is not None:
        return ChosenTarget(flag, "--target")
    if targets is not None and package_id in targets.directories:
        where = targets.location
        return ChosenTarget(targets.directories[package_id], f"local config {where.path} (found via {where.via})")
    return None


def missing_target_message(package_id: str, location: ConfigLocation) -> str:
    return (
        f"no target directory for package {package_id!r}: pass --target DIR, or add `{package_id} = \"DIR\"` under [targets] "
        f"in {location.path} (looked there via {location.via}; set {CONFIG_VARIABLE} to use another file)"
    )
