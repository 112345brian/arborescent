"""The policy's ``exclude`` patterns applied to one repository-relative path. Pure; shared by the scanner and the
package asset listing so both agree on what is out of scope."""

from __future__ import annotations

import fnmatch
from collections.abc import Sequence
from pathlib import PurePosixPath

__all__ = ["excluded_by_policy"]


def excluded_by_policy(rel: str, patterns: Sequence[str]) -> bool:
    parts = rel.split("/")
    ancestors = ["/".join(parts[:index]) for index in range(1, len(parts))]
    return any(
        fnmatch.fnmatch(rel, pattern)
        or fnmatch.fnmatch(PurePosixPath(rel).name, pattern)
        or any(fnmatch.fnmatch(parent, pattern) or fnmatch.fnmatch(parent.rsplit("/", 1)[-1], pattern) for parent in ancestors)
        for pattern in patterns
    )
