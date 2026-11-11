"""The policy's ``exclude`` patterns applied to one repository-relative path. Pure; shared by the scanner and the
package asset listing so both agree on what is out of scope.

Two forms, like a ``.gitignore`` entry:

- ``name`` or ``dir/*.gen``: unanchored. Matches the path itself, its file or folder name, or any ancestor folder (by full
  relative path or by its own name), at any depth. ``client`` therefore also excludes ``src/pkg/client/``.
- ``/name`` or ``/src/generated``: anchored to the repository root. Matches the path or an ancestor folder by its full
  relative path only, so ``/client`` excludes the top-level ``client/`` and nothing named ``client`` deeper down.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Sequence

__all__ = ["excluded_by_policy", "matches_exclusion", "nested_bare_pattern"]

_GLOB_CHARS = frozenset("*?[")


def matches_exclusion(candidate: str, pattern: str) -> bool:
    """Whether one exclude ``pattern`` covers ``candidate``, a file or folder path relative to the root."""
    if pattern.startswith("/"):
        return fnmatch.fnmatch(candidate, pattern[1:].rstrip("/"))
    return fnmatch.fnmatch(candidate, pattern) or fnmatch.fnmatch(candidate.rsplit("/", 1)[-1], pattern)


def excluded_by_policy(rel: str, patterns: Sequence[str]) -> bool:
    parts = rel.split("/")
    candidates = [rel, *("/".join(parts[:index]) for index in range(1, len(parts)))]
    return any(matches_exclusion(candidate, pattern) for pattern in patterns for candidate in candidates)


def nested_bare_pattern(folder: str, patterns: Sequence[str]) -> str | None:
    """The bare name (no ``/``, no glob) in ``patterns`` that excludes ``folder`` only through a folder below the root.

    Those are the entries that quietly reach into a first-party package: ``client`` hiding ``src/pkg/client``. ``None`` when
    the folder is excluded at the top level, by an anchored or a path pattern, or not by a bare name at all."""
    parts = folder.split("/")
    for pattern in patterns:
        if pattern.startswith("/") or "/" in pattern or _GLOB_CHARS & set(pattern):
            continue
        if len(parts) > 1 and pattern in parts[1:] and pattern != parts[0]:
            return pattern
    return None
