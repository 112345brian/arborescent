"""Equivalence of the incremental watcher (tools/incremental_analysis.py) with a fresh full scan."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tomllib
from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path

import pytest

from arborescent.cli import default_services
from arborescent.config import load_policy, policy_from_mapping
from arborescent.domain import EFFECT_CATEGORIES, INFORMATIONAL_RULES, Policy, Violation
from arborescent.engine import blocking, run_own
from arborescent.filesystem_adapter import LocalFileSystem
from arborescent.presets import trait_of
from tools import incremental_analysis
from tools.incremental_analysis import (
    _EFFECT_PARITY_RULES,
    _IMPORT_RULES,
    PARITY_RULES,
    Analysis,
    Result,
)

ROOT = Path(__file__).resolve().parents[1]

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


SMALL = {
    "sources": ["."],
    "rings": ["domain", "adapters"],
    "classify": {"domain": "domain", "adapters": "adapters"},
}


def _comparable(violations: Iterable[Violation]) -> list[tuple[object, ...]]:
    return sorted(
        (v.rule, v.subject, v.detail, v.line, v.target, v.hint, v.root_cause)
        for v in violations
        if v.rule in PARITY_RULES
    )


def _full_scan(policy: Policy, root: Path) -> list[Violation]:
    """What `arborescent check` itself finds, with no cache."""
    found, _ = run_own(
        policy, root, default_services(root, cache=False), practices=False
    )
    return found


def _assert_parity(analysis: Analysis, policy: Policy, root: Path) -> Result:
    result = analysis.refresh(policy)
    full = _full_scan(policy, root)
    # The same findings, on the same side of `check`'s failing / reported-only split.
    assert _comparable(result.violations) == _comparable(blocking(full))
    assert _comparable(result.notes) == _comparable(
        v for v in full if v.rule in INFORMATIONAL_RULES
    )
    assert {v.rule for v in (*result.violations, *result.notes)} <= PARITY_RULES
    return result


def _found(result: Result) -> tuple[Violation, ...]:
    return result.violations + result.notes


def _rules(result: Result) -> set[str]:
    return {v.rule for v in _found(result)}


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


# Python 3.11 rejects these, so there is no full scan to match there.
NEEDS_312 = {"generics", "nested quotes"}


@pytest.mark.parametrize("label", SYNTAX)
def test_unusual_syntax_matches_a_full_scan(tmp_path: Path, label: str) -> None:
    if label in NEEDS_312 and sys.version_info < (3, 12):
        pytest.skip("syntax added in Python 3.12")
    (tmp_path / "domain.py").write_text(SYNTAX[label], encoding="utf-8", newline="\n")
    policy = policy_from_mapping(
        {**SMALL, "ring": {"domain": {"external": []}, "adapters": {}}}
    )
    assert _assert_parity(Analysis(tmp_path), policy, tmp_path).syntax_errors == ()


def test_findings_match_a_full_scan_across_edits(tmp_path: Path
) -> None:
    _write(tmp_path, FILES)
    _repository(tmp_path)
    policy = _policy(tmp_path)
    scopes_only = {"scope-import"}  # needs `scopes`; covered below

    analysis = Analysis(tmp_path)
    initial = _assert_parity(analysis, policy, tmp_path)
    # Every claimed rule really occurs, so equality above is not two empty lists.
    assert _rules(initial) == _IMPORT_RULES - scopes_only
    assert initial.syntax_errors == ()
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
    # A vendor-folder change touches no scanned module.
    _write(tmp_path, {"vendor/dependency.py": "x = 2\n"})
    assert _assert_parity(analysis, policy, tmp_path).parsed == 0
    changed = replace(policy, forbid_circular_imports=False, entry_scripts=())
    assert _assert_parity(analysis, changed, tmp_path).parsed == 0


def test_scoped_findings_match_a_full_scan(tmp_path: Path) -> None:
    ring = {"rings": ["domain"], "ring": {"domain": {"traits": ["no-env"]}}}
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
    analysis = Analysis(tmp_path)
    result = _assert_parity(analysis, policy, tmp_path)
    assert _rules(result) == {"scope-import"}
    # A public module of another scope starts using an effect: the leak appears in the untouched importer.
    _write(
        tmp_path,
        {
            "two/private.py": "",
            "two/api.py": "import os\n\ndef f():\n    os.getenv('A')\n",
            "one/user.py": "from two import api\n",
        },
    )
    public = replace(
        policy,
        scopes=tuple(
            replace(scope, public=("two.api",)) if scope.name == "two" else scope
            for scope in policy.scopes
        ),
    )
    leaked = _assert_parity(analysis, public, tmp_path)
    assert "scope-effect-leak" in _rules(leaked)
    _write(tmp_path, {"two/api.py": "def f():\n    pass\n"})
    assert "scope-effect-leak" not in _rules(
        _assert_parity(analysis, public, tmp_path)
    )


# One use of every effect category, and one finding of every effect rule.
EFFECT_POLICY = """
sources = ["app", "devtools"]
exclude = ["tests"]
rings = ["domain", "effects", "adapters"]
dev_tools = ["devtools"]
dev_tools_forbid = ["process"]

[effects]
sanctioned = ["app.seam"]

[effect_seam_bypassed]

[unguarded_module_state]
rings = ["domain"]
allow = [{ module = "app.gone", name = "X", reason = "stale on purpose" }]

[practices]
effect-patch = "app/checks"
unreachable-patch = "tests"
unresolved-patch-target = "tests"

[ring.domain]
external = ["*"]
may_import = ["*"]
traits = [FORBID, "no-env-at-import", "no-filesystem-at-import"]

[ring.effects]
external = ["*"]
may_import = ["*"]
traits = ["permits-effects"]

[ring.adapters]
external = ["*"]
may_import = ["*"]
traits = ["permits-effects"]

[classify]
"app" = "domain"
"app.direct" = "domain"
"app.registry" = "domain"
"app.register" = "domain"
"app.helper" = "domain"
"app.user" = "domain"
"app.at_import" = "domain"
"app.clocked" = "domain"
"app.origin" = "domain"
"app.caller" = "domain"
"app.checks" = "domain"
"app.deeper" = "domain"
"app.moved" = "domain"
"app.checks.test_patch" = "domain"
"app.provider" = "effects"
"app.seam" = "effects"
"app.elsewhere" = "adapters"
""".replace(
    "FORBID",
    ", ".join(f'"{trait_of(name)}"' for name in sorted(EFFECT_CATEGORIES - {"registry"})),
)

DIRECT = """\
import functools, importlib, os, random, sqlite3, subprocess, sys, threading, time, uuid
import urllib.request

LOCK = threading.Lock()
count = 0
memo = {}


def uses(path):
    time.time()
    time.monotonic()
    time.sleep(1)
    os.getenv("A")
    os.getpid()
    os.path.exists(path)
    uuid.uuid4()
    random.random()
    threading.Thread()
    subprocess.run([])
    sqlite3.connect(path)
    urllib.request.urlopen(path)
    open(path)
    importlib.import_module(path)
    print(path)
    return sys.platform


def writes(key):
    global count
    count = 1
    memo[key] = 1


@functools.lru_cache
def load():
    return 1
"""

EFFECT_FILES = {
    "arborescent.toml": EFFECT_POLICY,
    "app/__init__.py": "",
    "app/direct.py": DIRECT,
    "app/registry.py": "CACHE = {}\nREG = {}\n",
    "app/register.py": "from app.registry import CACHE, REG\n\ndef add(k):\n    CACHE[k] = 1\n\ndef put(k):\n    REG[k] = 1\n",
    "app/helper.py": "import os\n\ndef load():\n    return os.getenv('A')\n",
    "app/provider.py": "import os\n\ndef read():\n    return os.getenv('A')\n",
    "app/seam.py": "import os\n\ndef read():\n    return os.getenv('A')\n",
    "app/elsewhere.py": "SEEN = []\n\ndef note(x):\n    SEEN.append(x)\n",
    "app/user.py": "from app import helper, provider, seam\n\ndef f():\n    return helper.load()\n",
    "app/at_import.py": "import os\nfrom app import helper\n\ndef here():\n    return open('x')\n\n"
    "A = os.getenv('A')\nB = helper.load()\nhere()\n",
    "app/clocked.py": "import time\n\ndef f(now):\n    return now(), time.time()\n",
    "app/origin.py": "def fn():\n    pass\n",
    "app/caller.py": "from app.origin import fn\n\ndef go():\n    return fn()\n",
    "app/checks/__init__.py": "",
    "app/checks/test_patch.py": "from unittest import mock\n\ndef test_x():\n    with mock.patch('app.direct.uses'):\n        pass\n",
    "devtools/__init__.py": "",
    "devtools/tool.py": "import subprocess\nsubprocess.run([])\n",
    "tests/test_patches.py": "from unittest import mock\nfrom app import caller\n\ndef test_x():\n"
    "    with mock.patch('app.origin.fn'), mock.patch('app.origin.missing'):\n        caller.go()\n",
}


class _Edits:
    """A project on disk whose every state is compared with a full scan."""

    def __init__(self, analysis: Analysis, policy: Policy, root: Path) -> None:
        self.analysis, self.policy, self.root = analysis, policy, root

    def check(self, policy: Policy | None = None) -> Result:
        return _assert_parity(self.analysis, policy or self.policy, self.root)

    def after(self, files: dict[str, str]) -> Result:
        _write(self.root, files)
        return self.check()


@pytest.fixture
def effects(tmp_path: Path) -> _Edits:
    _write(tmp_path, EFFECT_FILES)
    return _Edits(Analysis(tmp_path), _policy(tmp_path), tmp_path)


def _of(result: Result, subject: str) -> set[str]:
    return {v.rule for v in _found(result) if v.subject == subject}


def test_every_effect_rule_matches_a_full_scan(effects: _Edits) -> None:
    initial = effects.check()
    # Every claimed effect rule really occurs; the scoped one is covered by its own test.
    assert _rules(initial) >= _EFFECT_PARITY_RULES - {"scope-effect-leak"}
    assert {v.rule for v in initial.notes} == {
        "unguarded-module-state-note",
        "effect-unlisted-category",
    }
    assert initial.syntax_errors == ()
    assert effects.check() == replace(initial, parsed=0)
    policy = effects.policy
    # Policy only: nothing is parsed again.
    for changed in (
        replace(policy, effects=replace(policy.effects, sanctioned=("app.provider",))),
        replace(policy, module_state=None),
        replace(policy, effect_seam_bypassed=None),
        replace(policy, practices={}),
    ):
        assert effects.check(changed).parsed == 0
    assert effects.check() == replace(initial, parsed=0)


def test_effect_findings_in_untouched_modules_follow_edits(effects: _Edits) -> None:
    effects.check()
    # Lines move.
    assert effects.after({"app/direct.py": "\n\n" + DIRECT}).parsed == 1
    # A helper stops using its effect: findings in untouched importers go.
    quiet = effects.after({"app/helper.py": "def load():\n    return 1\n"})
    assert quiet.parsed == 1
    assert not [v for v in quiet.violations if "'app.helper'; effect" in v.detail]
    # It starts again, one module further away: the chain and the root cause change in `app.user`.
    chained = effects.after(
        {
            "app/deeper.py": "import time\n\ndef now():\n    return time.time()\n",
            "app/helper.py": "from app import deeper\n\ndef load():\n    return deeper.now()\n",
        }
    )
    assert any("app.helper -> app.deeper" in v.detail for v in chained.violations)
    # An import-time call changes only through the module it calls into.
    at_import = effects.after({"app/helper.py": "def load():\n    return open('x')\n"})
    assert any(v.hint == " (it uses open)" for v in at_import.violations)
    # A container stops being one: the write in another, untouched module is no longer a cache write.
    assert "effect-state" in _of(at_import, "app.register")
    gone = effects.after({"app/registry.py": "CACHE = REG = None\n"})
    assert "effect-state" not in _of(gone, "app.register")
    back = effects.after({"app/registry.py": EFFECT_FILES["app/registry.py"]})
    assert "effect-state" in _of(back, "app.register")
    # A seam module loses its effects, then gains them back.
    pure = effects.after({"app/provider.py": "def read():\n    return 1\n"})
    assert "effect-unsanctioned-import" not in _of(pure, "app.user")
    effects.after({"app/provider.py": EFFECT_FILES["app/provider.py"]})
    # The injected seam is now used.
    used = effects.after({"app/clocked.py": "def f(now):\n    return now()\n"})
    assert "effect-seam-bypassed" not in _of(used, "app.clocked")
    (effects.root / "app/deeper.py").unlink()
    effects.check()
    (effects.root / "app/direct.py").rename(effects.root / "app/moved.py")
    effects.check()


def _patching(target: str) -> str:
    return f"from unittest import mock\n\ndef test_x():\n    with mock.patch('{target}'):\n        pass\n"


def test_patch_rules_follow_edits_to_tests_modules_and_folders(effects: _Edits) -> None:
    rule = "unresolved-patch-target"
    test = "tests/test_patches.py"
    assert rule in _of(effects.check(), test)
    defined = effects.after({"app/origin.py": "def fn():\n    pass\nmissing = 1\n"})
    assert rule not in _of(defined, test)
    private = effects.after({"app/origin.py": "def fn():\n    pass\n_missing = 1\n"})
    assert rule in _of(private, test)
    assert rule not in _of(
        effects.after({test: _patching("app.origin._missing")}), test
    )
    # Only a private binding goes, which the module's facts may not show: the rule still has to look again.
    assert rule in _of(effects.after({"app/origin.py": "def fn():\n    pass\n"}), test)
    # A submodule that is a folder, which no scanned module describes.
    assert rule in _of(effects.after({test: _patching("app.data")}), test)
    assert rule not in _of(effects.after({"app/data/readme.txt": ""}), test)
    # A broken test file is left out, which leaves this folder with no tests: that is itself reported.
    broken = effects.after({test: "def broken(:\n"})
    assert broken.syntax_errors == ()
    assert [v.rule for v in broken.violations if v.subject == "tests"] == [
        "unreachable-patch",
        rule,
    ]


def _own_policy_table() -> dict:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return data["tool"]["arborescent"]


def test_this_repository_matches_a_full_scan() -> None:
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
    analysis = Analysis(root)
    assert _assert_parity(analysis, policy, root).syntax_errors == ()
    assert len(_assert_parity(analysis, sealed, root).violations) > 200
    strict = _assert_parity(analysis, _strictest(raw), root)
    # Real code, not only fixtures, produces effect findings to disagree about.
    assert _rules(strict) >= {
        "effect-at-import",
        "effect-env",
        "effect-filesystem",
        "effect-unlisted-category",
        "effect-unsanctioned-import",
        "effect-via-import",
        "os-branching",
        "unguarded-module-state",
    }


def _strictest(raw: dict) -> Policy:
    """This repository's code under a policy with every effect rule on."""
    forbidden = [trait_of(name) for name in sorted(EFFECT_CATEGORIES - {"registry"})]  # one left unlisted, for the note
    traits = [*forbidden, *(f"{trait}-at-import" for trait in forbidden)]
    return policy_from_mapping(
        {
            **raw,
            "ring": {
                # Replaced, so no ring keeps an OS-branching trait; the abstraction requirement is not an effect trait.
                name: {**table, "traits": [*traits, *({"require-abstract"} & set(table.get("traits", [])))]}
                for name, table in raw["ring"].items()
            },
            "effects": {"sanctioned": [], "rings": ["adapters"]},
            "effect_seam_bypassed": {},
            "unguarded_module_state": {},
            "practices": {
                "unreachable-patch": "tests",
                "unresolved-patch-target": "tests",
            },
        }
    )


def test_edits_to_a_copy_of_this_repository_match_a_full_scan(tmp_path: Path
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
    analysis = Analysis(tmp_path)
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

    # Effect edits deep in the import graph, with every effect rule on.
    strict = _strictest(_own_policy_table())
    before = _assert_parity(analysis, strict, tmp_path)
    domain.write_text(
        original
        + "\nimport os, time\nSEEN = {}\nSTARTED = time.time()\n"
        + "def remember(key):\n    SEEN[key] = os.getenv(key)\n",
        encoding="utf-8",
    )
    effects = _assert_parity(analysis, strict, tmp_path)
    assert effects.parsed == 1
    added = set(_found(effects)) - set(_found(before))
    assert {
        "effect-at-import",
        "effect-clock",
        "effect-env",
        "effect-via-import",
        "unguarded-module-state",
    } <= {v.rule for v in added}
    # Importers that were not edited gained findings.
    assert {v.subject for v in added} - {"arborescent.domain"}
    domain.write_text(original, encoding="utf-8")
    assert _found(_assert_parity(analysis, strict, tmp_path)) == _found(before)


@pytest.mark.parametrize(
    "source",
    [
        b"import adapters\r\nfrom adapters import (\r\n    x,\r\n)\r\n",
        b"import os\rimport adapters\r",
        b"\xef\xbb\xbfimport adapters\n",
    ],
    ids=["crlf", "lone carriage return", "byte order mark"],
)
def test_line_numbers_match_for_other_encodings(tmp_path: Path, source: bytes
) -> None:
    (tmp_path / "domain.py").write_bytes(source)
    (tmp_path / "adapters.py").write_bytes(b"x = 1\n")
    result = _assert_parity(
        Analysis(tmp_path), policy_from_mapping(SMALL), tmp_path
    )
    assert result.violations


@pytest.mark.parametrize(
    "source",
    [
        'print "python 2"\n',
        "return 1\nbreak = 2\n",
        "def f():\n  x\n y\n",
        "x = '\0'\n",
        # Deep enough that CPython refuses it.
        "def f():\n    return " + "1+" * 200_000 + "1\n",
    ],
    ids=["python 2", "keyword", "indentation", "nul", "very deep"],
)
def test_python_decides_what_is_a_syntax_error(tmp_path: Path, source: str
) -> None:
    policy = policy_from_mapping(SMALL)
    _write(tmp_path, {"domain.py": "import adapters\n", "adapters.py": source})
    with pytest.raises((SyntaxError, ValueError, RecursionError)):
        _full_scan(policy, tmp_path)
    analysis = Analysis(tmp_path)
    broken = analysis.refresh(policy)
    assert broken.syntax_errors == ("adapters.py",)
    assert [v.rule for v in _found(broken)] == ["internal-import"]
    _write(tmp_path, {"adapters.py": "x = 1\n"})
    assert _assert_parity(analysis, policy, tmp_path).syntax_errors == ()


def test_a_file_outside_enforcement_that_breaks_is_not_hidden_by_the_cache(tmp_path: Path
) -> None:
    # Annotation discovery parses it, so a full scan fails on it, yet no scanned module changes.
    policy = policy_from_mapping({**SMALL, "sources": ["domain.py"]})
    _write(tmp_path, {"domain.py": "", "loose.py": "x = 1\n"})
    analysis = Analysis(tmp_path)
    assert analysis.refresh(policy).syntax_errors == ()
    _write(tmp_path, {"loose.py": "def broken(:\n"})
    with pytest.raises(SyntaxError):
        _full_scan(policy, tmp_path)
    assert analysis.refresh(policy).syntax_errors == ("loose.py",)
    _write(tmp_path, {"loose.py": "x = 1\n"})
    assert analysis.refresh(policy).syntax_errors == ()


def test_import_rules_rerun_only_when_something_they_read_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
    analysis = Analysis(tmp_path)
    first = analysis.refresh(policy)
    _write(tmp_path, {"domain.py": domain.replace("return 1", "return 2")})
    body = analysis.refresh(policy)
    assert (body.parsed, body.violations, len(runs)) == (1, first.violations, 1)
    _write(tmp_path, {"domain.py": "\n" + domain})
    moved = _assert_parity(analysis, policy, tmp_path)
    assert [v.line for v in moved.violations] == [2]
    # `_assert_parity` runs the real full scan, which does not go through the patched name.
    assert len(runs) == 2


@pytest.mark.parametrize(
    ("domain", "code", "printed"),
    [
        ("import adapters\n", 1, '"rule": "internal-import"'),
        # A note does not fail `check`, and is still printed.
        (
            "import os\nX = 1\n\ndef f():\n    print(os.getpid())\n",
            0,
            '"rule": "effect-unlisted-category"',
        ),
        ("", 0, '"violations": []'),
    ],
    ids=["violation", "note only", "clean"],
)
def test_command_line_fails_only_on_what_check_fails_on(tmp_path: Path, domain: str, code: int, printed: str
) -> None:
    _write(
        tmp_path,
        {
            "arborescent.toml": 'sources = ["."]\nrings = ["domain", "adapters"]\n'
            '[ring.domain]\nexternal = ["os"]\ntraits = ["no-env"]\n'
            '[classify]\n"domain" = "domain"\n"adapters" = "adapters"\n',
            "domain.py": domain,
            "adapters.py": "",
        },
    )
    done = _command_line(tmp_path)
    assert done.returncode == code, done.stdout + done.stderr
    assert printed in done.stdout


def _command_line(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "tools.incremental_analysis",
            "--policy",
            str(root / "arborescent.toml"),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def test_command_line_reports_a_full_scan(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "arborescent.toml": 'sources = ["."]\nrings = ["domain", "adapters"]\n'
            '[classify]\n"domain" = "domain"\n"adapters" = "adapters"\n',
            "domain.py": "import adapters\n",
            "adapters.py": "",
        },
    )
    done = _command_line(tmp_path)
    assert done.returncode == 1, done.stderr
    assert '"rule": "internal-import"' in done.stdout
    assert '"syntax_errors": []' in done.stdout
