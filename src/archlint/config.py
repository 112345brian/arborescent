"""Locate and load the policy: ``archlint.toml`` (top-level keys) or ``[tool.archlint]`` in ``pyproject.toml``."""

from __future__ import annotations

import tomllib
from pathlib import Path

from .domain import Policy, PolicyError

__all__ = ["find_policy", "load_policy"]


def find_policy(start: Path) -> Path:
    for folder in (start, *start.parents):
        for name in ("archlint.toml", "pyproject.toml"):
            candidate = folder / name
            if candidate.is_file():
                if name == "archlint.toml" or "archlint" in tomllib.loads(candidate.read_text(encoding="utf-8")).get("tool", {}):
                    return candidate
    raise PolicyError(f"no archlint.toml or [tool.archlint] found from {start}")


def load_policy(path: Path) -> tuple[Policy, Path]:
    """The parsed policy and the repository root (the folder holding the policy file)."""
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    raw = data if path.name == "archlint.toml" else data.get("tool", {}).get("archlint")
    if raw is None:
        raise PolicyError(f"{path} has no [tool.archlint] table")
    return Policy.from_mapping(raw), path.parent
