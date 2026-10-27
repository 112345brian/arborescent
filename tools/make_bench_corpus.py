"""Build read-only COPIES of real codebases for ``arborescent bench`` (the originals are never touched).

    python tools/make_bench_corpus.py OUT_DIR NAME=SRC_DIR [NAME=SRC_DIR ...]

Only ``*.py`` files are copied. Each copy gets a permissive single-ring ``arborescent.toml``: the bench measures how long
the engines take on that much code, not whether the code obeys a policy (a real policy is a human decision).
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

SKIP = {".venv", "venv", "node_modules", "__pycache__", ".git", ".tox", ".mypy_cache", ".ruff_cache", "build", "dist"}

POLICY = """\
python = "3.11"
sources = ["."]
rings = ["all"]
type_checkers = ["pyright", "basedpyright", "ty", "pyrefly"]
forbid_everywhere = ["telnetlib"]

[ring.all]
external = ["*"]
os_branching = true

[classify]
"*" = "all"
"""


def copy_py(src: Path, dst: Path) -> int:
    count = 0
    for path in src.rglob("*.py"):
        rel = path.relative_to(src)
        if SKIP & set(rel.parts) or any(p.startswith(".") for p in rel.parts):
            continue
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        _ = shutil.copyfile(path, target)
        count += 1
    return count


def main(argv: list[str]) -> int:
    out = Path(argv[0])
    for spec in argv[1:]:
        name, _, src = spec.partition("=")
        dst = out / name
        shutil.rmtree(dst, ignore_errors=True)
        dst.mkdir(parents=True)
        n = copy_py(Path(src), dst)
        if not (dst / "arborescent.toml").exists():
            _ = (dst / "arborescent.toml").write_text(POLICY, encoding="utf-8", newline="\n")
        print(f"{name}: copied {n} python files from {src}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
