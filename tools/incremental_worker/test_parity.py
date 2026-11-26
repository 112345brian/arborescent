"""Equivalence of the incremental driver and worker with a fresh full scan."""

from __future__ import annotations

import ast
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from arborescent.cli import default_services
from arborescent.config import load_policy, policy_from_mapping
from arborescent.domain import Policy, Violation
from arborescent.engine import run_own
from arborescent.filesystem_adapter import LocalFileSystem
from tools import incremental_analysis
from tools.incremental_analysis import PARITY_RULES, Analysis, Result, Worker

ROOT = Path(__file__).resolve().parents[2]

# Valid Python that differs from the common forms the repository itself uses.
SYNTAX = {
    "future": "from __future__ import (annotations, division)\n",
    "attribute guard": "if t.TYPE_CHECKING:\n    import a\n",
    "parenthesised guard": "if (typing.TYPE_CHECKING):\n    import a\n",
    "spaced guard": "if typing . TYPE_CHECKING:\n    import a\n",
    "elif guard": "if x:\n    import a\nelif TYPE_CHECKING:\n    import b\nelse:\n    import c\n",
    "after guard": "if TYPE_CHECKING:\n    import a\nelif y:\n    import b\nelse:\n    import c\n",
    "inside guard": "if TYPE_CHECKING:\n    if x:\n        import a\n    def f():\n        import b\n",
    "definitions": "async def f():\n    import a\n@d\ndef g():\n    from . import b\nclass C:\n    def h(self):\n"
    "        from ..x.y import (z,  # why\n            w as v)\n",
    "handlers": "try: import c\nexcept* E: import d\nelse: import e\nfinally: import f\n",
    "match": "match x:\n    case 1:\n        import a\n    case _:\n        from b import *\n",
    "generics": "type X[T] = list[T]\ndef f[T](x: T) -> T:\n    import b\n",
    "nested quotes": "x = f'{y['k']}'\nimport a\n",
    "continuations": "from a.\\\n    b import c\nimport d\\\n  .e\n",
    "spaced dots": "from . . import a\nfrom ... a . b import c\nimport x . y as z\n",
    "compatibility identifier": "import \ufb01le\nfrom \ufb01le import \ufb01x\n",
    "form feed": "\x0cimport a\nif x:\n\timport b\n",
}


def _update(worker: Worker, text: str):
    evidence = worker.update({"m": {"text": text, "is_package": False}}, [])
    facts = evidence["modules"]["m"]
    worker.update({}, ["m"])
    return facts


def test_the_worker_parses_every_file_of_this_repository(worker_binary: Path) -> None:
    """A file the grammar rejects loses its call evidence."""
    files = sorted(
        path
        for folder in ("src", "tests", "tools")
        for path in (ROOT / folder).rglob("*.py")
        if "target" not in path.parts
    )
    assert len(files) > 200
    with Worker(worker_binary) as worker:
        for path in files:
            text = path.read_text(encoding="utf-8-sig")
            assert not _update(worker, text)["syntax_error"], path


SMALL = {
    "sources": ["."],
    "rings": ["domain", "adapters"],
    "classify": {"domain": "domain", "adapters": "adapters"},
}


def _comparable(violations: Iterator[Violation]) -> list[tuple[object, ...]]:
    return sorted(
        (v.rule, v.subject, v.detail, v.line, v.target, v.hint, v.root_cause)
        for v in violations
        if v.rule in PARITY_RULES
    )


def _full_scan(policy: Policy, root: Path) -> list[tuple[object, ...]]:
    """What `arborescent check` itself finds, with no cache."""
    found, _ = run_own(
        policy, root, default_services(root, cache=False), practices=False
    )
    return _comparable(iter(found))


def _assert_parity(analysis: Analysis, policy: Policy, root: Path) -> Result:
    result = analysis.refresh(policy)
    assert _comparable(iter(result.violations)) == _full_scan(policy, root)
    assert {v.rule for v in result.violations} <= PARITY_RULES | {"effect-via-call"}
    return result


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")


POLICY = """
sources = ["app", "devtools"]
exclude = ["generated"]
rings = ["domain", "ports", "adapters"]
forbid_circular_imports = true
forbidden_modules = ["*.legacy"]
require_arbor_annotations = true
dev_tools = ["devtools"]
dev_tools_may_import = ["app.ports"]
entry_scripts = ["app.script"]
importable_by = { "app.secret" = ["app.adapters"], "app.gone" = ["app.nobody"] }

[ring.domain]
external = []
may_import = ["*"]

[ring.ports]
may_import = ["domain"]
external = []

[ring.adapters]
may_import = ["domain", "ports"]
external = ["os"]

[classify]
"app" = "domain"
"app.domain" = "domain"
"app.secret" = "domain"
"app.a" = "domain"
"app.b" = "domain"
"app.ports" = "ports"
"app.adapters" = "adapters"
"app.legacy" = "domain"
"app.mismatch" = "domain"
"app.script" = "adapters"
"""

FILES = {
    "arborescent.toml": POLICY,
    "app/__init__.py": "",
    "app/domain.py": "import os\nfrom app import adapters, secret\nfrom devtools import helper\nfrom ... import climbing\n",
    "app/secret.py": "",
    "app/a.py": "from app import b\n",
    "app/b.py": "from app import a\n",
    "app/ports.py": "from . import domain\n",
    "app/adapters.py": "import os\nimport requests\n",
    "app/legacy.py": "",
    "app/mismatch.py": "__arbor__ = {'ring': 'ports'}\n",
    "app/invalid.py": "__arbor__ = 1\n",
    "app/annotated.py": "__arbor__ = {'ring': 'ports'}\nfrom app import adapters\n",
    "app/orphan.py": "",
    "app/claims.py": "__arbor__ = {'role': 'dev_tool'}\n",
    "app/script.py": "# /// script\n# dependencies = []\n# ///\nimport requests\n",
    "devtools/__init__.py": "",
    "devtools/helper.py": "from app import domain\n",
    "outside/loose.py": "import app\n",
    "vendor/dependency.py": "x = 1\n",
    "node_modules/stray.py": "x = 1\n",
    "generated/skipped.py": "def broken(:\n",
}


def _repository(root: Path) -> None:
    """Only a git repository has tracked files in noise folders, which the full scan reports."""
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is needed to list tracked noise-folder files")
    subprocess.run([git, "init", "-q"], cwd=root, check=True)
    subprocess.run([git, "add", "-f", "."], cwd=root, check=True)


def _policy(root: Path) -> Policy:
    return load_policy(LocalFileSystem(), root / "arborescent.toml")[0]


@pytest.mark.parametrize("label", SYNTAX)
def test_unusual_syntax_matches_a_full_scan(
    worker_binary: Path, tmp_path: Path, label: str
) -> None:
    (tmp_path / "domain.py").write_text(SYNTAX[label], encoding="utf-8", newline="\n")
    policy = policy_from_mapping(
        {**SMALL, "ring": {"domain": {"external": []}, "adapters": {}}}
    )
    with Worker(worker_binary) as worker:
        result = _assert_parity(Analysis(worker, tmp_path), policy, tmp_path)
    assert result.incomplete == result.syntax_errors == ()


def test_findings_match_a_full_scan_across_edits(
    worker_binary: Path, tmp_path: Path
) -> None:
    _write(tmp_path, FILES)
    _repository(tmp_path)
    policy = _policy(tmp_path)
    scopes_only = {"scope-import"}  # needs `scopes`; covered below

    with Worker(worker_binary) as worker:
        analysis = Analysis(worker, tmp_path)
        initial = _assert_parity(analysis, policy, tmp_path)
        # Every claimed rule really occurs, so equality above is not two empty lists.
        assert {v.rule for v in initial.violations} == PARITY_RULES - scopes_only
        assert initial.syntax_errors == initial.incomplete == ()
        again = _assert_parity(analysis, policy, tmp_path)
        assert again == replace(initial, parsed=0)

        for edit in (
            {"app/domain.py": "VALUE = 1\n"},  # clear findings
            {
                "app/domain.py": "def late():\n    from app import adapters\n"
            },  # lazy import
            {"app/orphan.py": "__arbor__ = {'ring': 'domain'}\n"},  # annotation only
            {"app/script.py": "import requests\n"},  # metadata only
            {"app/added.py": "from app import b\n"},  # addition
            {"app/domain/__init__.py": ""},  # replaced below
        ):
            if "app/domain/__init__.py" in edit:
                (tmp_path / "app/domain.py").unlink()  # module becomes a package
            _write(tmp_path, edit)
            _assert_parity(analysis, policy, tmp_path)

        (tmp_path / "app/b.py").unlink()  # deletion breaks the cycle
        _assert_parity(analysis, policy, tmp_path)
        (tmp_path / "app/a.py").rename(tmp_path / "app/renamed.py")
        _assert_parity(analysis, policy, tmp_path)
        # A vendor-folder change touches no module the worker holds.
        _write(tmp_path, {"vendor/dependency.py": "x = 2\n"})
        assert _assert_parity(analysis, policy, tmp_path).parsed == 0
        changed = replace(policy, forbid_circular_imports=False, entry_scripts=())
        assert _assert_parity(analysis, changed, tmp_path).parsed == 0


def test_scoped_findings_match_a_full_scan(worker_binary: Path, tmp_path: Path) -> None:
    ring = {"rings": ["domain"], "ring": {"domain": {}}}
    policy = policy_from_mapping(
        {
            "scopes": {
                "one": {
                    **ring,
                    "source_roots": ["."],
                    "sources": ["one"],
                    "classify": {"one*": "domain"},
                },
                "two": {
                    **ring,
                    "source_roots": ["."],
                    "sources": ["two"],
                    "classify": {"two*": "domain"},
                },
            }
        }
    )
    _write(
        tmp_path,
        {
            "one/__init__.py": "",
            "one/user.py": "from two import private\n",
            "two/__init__.py": "",
            "two/private.py": "",
        },
    )
    with Worker(worker_binary) as worker:
        result = _assert_parity(Analysis(worker, tmp_path), policy, tmp_path)
    assert {v.rule for v in result.violations} == {"scope-import"}


def _own_policy_table() -> dict:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return data["tool"]["arborescent"]


def test_this_repository_matches_a_full_scan(worker_binary: Path) -> None:
    policy, root = load_policy(LocalFileSystem(), ROOT / "pyproject.toml")
    raw = _own_policy_table()
    # The same code under a policy that forbids every dependency, so there is plenty to disagree about.
    sealed = policy_from_mapping(
        {
            **raw,
            "ring": {
                name: {**table, "may_import": [], "external": []}
                for name, table in raw["ring"].items()
            },
        }
    )
    with Worker(worker_binary) as worker:
        analysis = Analysis(worker, root)
        assert _assert_parity(analysis, policy, root).incomplete == ()
        assert len(_assert_parity(analysis, sealed, root).violations) > 200


def test_edits_to_a_copy_of_this_repository_match_a_full_scan(
    worker_binary: Path, tmp_path: Path
) -> None:
    shutil.copytree(
        ROOT / "src/arborescent",
        tmp_path / "src/arborescent",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    for name in ("pyproject.toml", "packages.toml"):
        shutil.copy(ROOT / name, tmp_path)
    policy = policy_from_mapping(_own_policy_table())
    domain = tmp_path / "src/arborescent/domain.py"
    original = domain.read_text(encoding="utf-8")
    with Worker(worker_binary) as worker:
        analysis = Analysis(worker, tmp_path)
        clean = _assert_parity(analysis, policy, tmp_path)
        domain.write_text(
            original + "\nimport subprocess\nfrom . import cli\n", encoding="utf-8"
        )
        broken = _assert_parity(analysis, policy, tmp_path)
        assert broken.parsed == 1
        assert {"internal-import", "external-import", "circular-import"} <= {
            v.rule for v in broken.violations
        }
        domain.write_text(original, encoding="utf-8")
        assert _assert_parity(analysis, policy, tmp_path).violations == clean.violations
        (tmp_path / "src/arborescent/rules.py").unlink()
        _assert_parity(analysis, policy, tmp_path)


@pytest.mark.parametrize(
    "source",
    [
        b"import adapters\r\nfrom adapters import (\r\n    x,\r\n)\r\n",
        b"import os\rimport adapters\r",
        b"\xef\xbb\xbfimport adapters\n",
    ],
    ids=["crlf", "lone carriage return", "byte order mark"],
)
def test_line_numbers_match_for_other_encodings(
    worker_binary: Path, tmp_path: Path, source: bytes
) -> None:
    (tmp_path / "domain.py").write_bytes(source)
    (tmp_path / "adapters.py").write_bytes(b"x = 1\n")
    with Worker(worker_binary) as worker:
        result = _assert_parity(
            Analysis(worker, tmp_path), policy_from_mapping(SMALL), tmp_path
        )
    assert result.violations


@pytest.mark.parametrize(
    "source",
    [
        'print "python 2"\n',
        "return 1\nbreak = 2\n",
        "def f():\n  x\n y\n",
        "x = '\0'\n",
        # Deep enough to overflow the worker's stack if it were ever sent there.
        "def f():\n    return " + "1+" * 200_000 + "1\n",
    ],
    ids=["python 2", "keyword", "indentation", "nul", "very deep"],
)
def test_python_decides_what_is_a_syntax_error(
    worker_binary: Path, tmp_path: Path, source: str
) -> None:
    policy = policy_from_mapping(SMALL)
    _write(tmp_path, {"domain.py": "import adapters\n", "adapters.py": source})
    with pytest.raises((SyntaxError, ValueError, RecursionError)):
        _full_scan(policy, tmp_path)
    with Worker(worker_binary) as worker:
        analysis = Analysis(worker, tmp_path)
        broken = analysis.refresh(policy)
        assert broken.syntax_errors == ("adapters.py",)
        assert [v.rule for v in broken.violations] == ["internal-import"]
        _write(tmp_path, {"adapters.py": "x = 1\n"})
        assert _assert_parity(analysis, policy, tmp_path).syntax_errors == ()


def test_a_file_outside_enforcement_that_breaks_is_not_hidden_by_the_cache(
    worker_binary: Path, tmp_path: Path
) -> None:
    # Annotation discovery parses it, so a full scan fails on it, yet no scanned module changes.
    policy = policy_from_mapping({**SMALL, "sources": ["domain.py"]})
    _write(tmp_path, {"domain.py": "", "loose.py": "x = 1\n"})
    with Worker(worker_binary) as worker:
        analysis = Analysis(worker, tmp_path)
        assert analysis.refresh(policy).syntax_errors == ()
        _write(tmp_path, {"loose.py": "def broken(:\n"})
        with pytest.raises(SyntaxError):
            _full_scan(policy, tmp_path)
        assert analysis.refresh(policy).syntax_errors == ("loose.py",)
        _write(tmp_path, {"loose.py": "x = 1\n"})
        assert analysis.refresh(policy).syntax_errors == ()


def test_valid_python_the_worker_cannot_parse_keeps_imports_and_is_reported(
    worker_binary: Path, tmp_path: Path
) -> None:
    source = "import adapters\ndef f():\n    (bar.\nbaz)\n"
    ast.parse(source)
    _write(tmp_path, {"domain.py": source, "adapters.py": ""})
    with Worker(worker_binary) as worker:
        # If a grammar update accepts this, pick another construct.
        assert _update(worker, source)["syntax_error"]
        result = _assert_parity(
            Analysis(worker, tmp_path), policy_from_mapping(SMALL), tmp_path
        )
    assert result.incomplete == ("domain.py",)
    assert result.syntax_errors == ()
    assert [v.rule for v in result.violations] == ["internal-import"]


def test_import_rules_rerun_only_when_something_they_read_changes(
    worker_binary: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs: list[int] = []
    real = incremental_analysis.check_modules
    monkeypatch.setattr(
        incremental_analysis,
        "check_modules",
        lambda *args: (runs.append(1), real(*args))[1],
    )
    policy = policy_from_mapping(SMALL)
    domain = "import adapters\n\ndef f():\n    return 1\n"
    _write(tmp_path, {"domain.py": domain, "adapters.py": ""})
    with Worker(worker_binary) as worker:
        analysis = Analysis(worker, tmp_path)
        first = analysis.refresh(policy)
        _write(tmp_path, {"domain.py": domain.replace("return 1", "return 2")})
        body = analysis.refresh(policy)
        assert (body.parsed, body.violations, len(runs)) == (1, first.violations, 1)
        _write(tmp_path, {"domain.py": "\n" + domain})
        moved = _assert_parity(analysis, policy, tmp_path)
    assert [v.line for v in moved.violations] == [2]
    # `_assert_parity` runs the real full scan, which does not go through the patched name.
    assert len(runs) == 2


def test_developer_tools_get_no_ring_effect_findings(
    worker_binary: Path, tmp_path: Path
) -> None:
    policy = policy_from_mapping(
        {**SMALL, "sources": ["."], "dev_tools": ["domain.py"]}
    )
    _write(
        tmp_path,
        {"domain.py": "import os\ndef load():\n    return os.getenv('X')\n"},
    )
    with Worker(worker_binary) as worker:
        violations = Analysis(worker, tmp_path).refresh(policy).violations
    assert "effect-via-call" not in {v.rule for v in violations}


def test_command_line_reports_a_full_scan(worker_binary: Path, tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "arborescent.toml": 'sources = ["."]\nrings = ["domain", "adapters"]\n'
            '[classify]\n"domain" = "domain"\n"adapters" = "adapters"\n',
            "domain.py": "import adapters\n",
            "adapters.py": "",
        },
    )
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "tools.incremental_analysis",
            "--worker",
            str(worker_binary),
            "--policy",
            str(tmp_path / "arborescent.toml"),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert done.returncode == 1, done.stderr
    assert '"rule": "internal-import"' in done.stdout
    assert '"incomplete": []' in done.stdout
