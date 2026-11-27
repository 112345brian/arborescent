"""Reports and baselines must not depend on PYTHONHASHSEED. Each seed needs its own process: it is read at start-up."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from arborescent_helpers import write_files

_SRC = Path(__file__).resolve().parents[1] / "src"
_SEEDS = (0, 1, 2, 3, 4, 5, 6, 7)

# One module with many effect categories, reached directly, through another module and through a cycle, so every ordering
# choice has ties. `strict` makes the middle ring forbid them too, and `must_be_pure` takes the same reach through #107.
_PROJECT = {
    "pyproject.toml": """
[tool.arborescent]
python = "3.11"
sources = ["app"]
rings = ["domain", "ports", "adapters"]

[tool.arborescent.ring.domain]
external = ["os", "time", "random", "subprocess", "socket", "logging", "sys"]

[tool.arborescent.ring.ports]
may_import = ["domain"]

[tool.arborescent.ring.adapters]
may_import = ["domain", "ports"]
external = ["os", "time", "random", "subprocess", "socket", "logging", "sys"]

[tool.arborescent.classify]
"app" = "domain"
"app.domain" = "domain"
"app.domain_b" = "domain"
"app.ports" = "ports"
"app.adapters" = "adapters"

[tool.arborescent.package.constraints]
must_be_pure = true
""",
    "app/__init__.py": "",
    "app/adapters.py": (
        "import os\nimport random\nimport socket\nimport subprocess\nimport sys\nimport time\nimport logging\n\n\n"
        "def run() -> None:\n    os.getenv('A')\n    os.remove('a')\n    random.random()\n    socket.socket()\n"
        "    subprocess.run(['a'])\n    sys.exit(0)\n    time.time()\n    time.sleep(1)\n    logging.info('a')\n    print('a')\n"
    ),
    "app/ports.py": "from app import adapters\n",
    "app/domain.py": "from app import ports\nfrom app import adapters\nfrom app import domain_b\n",
    "app/domain_b.py": "from app import domain\nfrom app import ports\n",
}

_SCRIPT = "import sys; from arborescent.cli import main; sys.exit(main(sys.argv[1:]))"


def _run(root: Path, seed: int, *args: str) -> str:
    env = {**os.environ, "PYTHONHASHSEED": str(seed), "PYTHONPATH": str(_SRC), "PYTHONIOENCODING": "utf-8"}
    done = subprocess.run(
        [sys.executable, "-c", _SCRIPT, "--config", str(root / "pyproject.toml"), *args],
        env=env, capture_output=True, text=True, encoding="utf-8", check=False, timeout=120,
    )
    assert done.returncode in (0, 1), done.stdout + done.stderr
    return done.stdout


def _without_timings(report: str) -> str:
    return re.sub(r"\d+\.\d+s\b", "Ns", report)


def test_check_report_and_baselines_are_the_same_under_every_hash_seed(tmp_path: Path) -> None:
    write_files(tmp_path, _PROJECT)
    baseline = tmp_path / "arborescent-baseline.json"
    seen: set[tuple[str, str, str]] = set()
    for seed in _SEEDS:
        report = _without_timings(_run(tmp_path, seed, "check", "--no-tools", "--no-cache"))
        assert "effect-via-import" in report and "package-constraint-facts" in report, report
        baseline.unlink(missing_ok=True)
        _run(tmp_path, seed, "baseline")
        plain = baseline.read_text(encoding="utf-8")
        baseline.unlink()
        _run(tmp_path, seed, "baseline", "--by-root-cause")
        grouped = baseline.read_text(encoding="utf-8")
        assert "root_causes" in grouped, grouped
        baseline.unlink()
        seen.add((report, plain, grouped))
    assert len({report for report, _, _ in seen}) == 1, "report order depends on PYTHONHASHSEED"
    assert len({plain for _, plain, _ in seen}) == 1, "baseline depends on PYTHONHASHSEED"
    assert len({grouped for _, _, grouped in seen}) == 1, "root-cause baseline depends on PYTHONHASHSEED"
