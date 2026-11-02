"""#67: the LibCST adapter behind the ``ImportRewriter`` port, and the scanner it runs to refuse what it cannot rewrite safely.

Unit tests drive the adapter through a scripted ``ToolRunner``; the real-LibCST tests (skipped when it is absent) run the
real scanner and the real rename command on a scratch tree.
"""

from __future__ import annotations

import ast
import hashlib
import json
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

from arborescent.domain import ToolResult
from arborescent.driver_libcst import LibCstRename
from arborescent.driver_port import ImportRewriter
from arborescent.driver_request import RewriteFinding, RewriteRequest
from arborescent.package_model import ModuleRename
from arborescent.tool_runner import SubprocessToolRunner

ROOT = Path("/new")
RENAMES = (ModuleRename("app.core.internal", "core.internal"), ModuleRename("app.core.api", "core.api"))
REQUEST = RewriteRequest(ROOT, ("src/core",), RENAMES)
HAVE_LIBCST = SubprocessToolRunner().run([sys.executable, "-I", "-c", "import libcst"], Path.cwd()).returncode == 0


class Scripted:
    """A ``ToolRunner`` that answers by what is asked: availability, the scanner's two modes, the rename command."""

    def __init__(self, *, installed: bool = True, forms: Sequence[dict[str, object]] = (), remaining: Sequence[dict[str, object]] = (), rename_rc: int = 0, rename_err: str = "boom") -> None:
        self.installed, self.forms, self.remaining, self.rename_rc, self.rename_err = installed, list(forms), list(remaining), rename_rc, rename_err
        self.calls: list[list[str]] = []

    def available(self, executable: str) -> bool:
        return True

    def run(self, argv: Sequence[str], cwd: Path, timeout: float = 600.0) -> ToolResult:
        self.calls.append(list(argv))
        if argv[2] == "-c":
            return ToolResult(0 if self.installed else 1, "", "")
        if argv[3] == "arborescent.driver_libcst_scan":
            return ToolResult(0, json.dumps({"forms": self.forms, "remaining": self.remaining}.get(argv[4], [])), "")
        return ToolResult(self.rename_rc, "", self.rename_err if self.rename_rc else "")


def adapter(runner: Scripted | None = None) -> tuple[LibCstRename, Scripted]:
    runner = runner or Scripted()
    return LibCstRename(runner), runner


def test_it_satisfies_the_port() -> None:
    library: ImportRewriter = adapter()[0]
    assert library.requirement.startswith("libcst")


def test_availability_is_whether_libcst_imports() -> None:
    assert adapter(Scripted(installed=True))[0].available() is True
    assert adapter(Scripted(installed=False))[0].available() is False


def test_the_commands_are_our_rewrite_then_one_rename_per_module_in_the_given_order_over_the_given_paths() -> None:
    commands = adapter()[0].commands(REQUEST)
    assert commands[0] == (sys.executable, "-I", "-m", "arborescent.driver_libcst_scan", "rewrite", str(ROOT), "--under", "src/core",
                           "app.core.internal=core.internal", "app.core.api=core.api")
    assert commands[1:] == tuple(
        (sys.executable, "-I", "-m", "libcst.tool", "codemod", "--external", "--hide-progress", "--no-format", "libcst.codemod.commands.rename.RenameCommand",
         str(ROOT / "src/core"), "--old_name", rename.source, "--new_name", rename.target)
        for rename in RENAMES
    )


def test_unsupported_forms_come_from_the_scanner_with_every_old_name() -> None:
    library, runner = adapter(Scripted(forms=[{"file": "src/core/x.py", "line": 4, "text": "from app.core import api"}]))
    assert library.unsupported(REQUEST) == (RewriteFinding("src/core/x.py", 4, "from app.core import api"),)
    scan = runner.calls[0]
    assert scan[1:5] == ["-I", "-m", "arborescent.driver_libcst_scan", "forms"]
    assert str(ROOT) in scan and "src/core" in scan
    assert {"app.core.api=core.api", "app.core.internal=core.internal"} <= set(scan)


def test_a_scanner_that_fails_fails_closed() -> None:
    class Broken(Scripted):
        def run(self, argv: Sequence[str], cwd: Path, timeout: float = 600.0) -> ToolResult:
            return ToolResult(1, "", "no libcst") if argv[3] == "arborescent.driver_libcst_scan" else super().run(argv, cwd, timeout)

    found = adapter(Broken())[0].unsupported(REQUEST)
    assert len(found) == 1 and "could not check" in found[0].text


def test_a_refusal_happens_before_any_rename_command_runs() -> None:
    library, runner = adapter(Scripted(forms=[{"file": "a.py", "line": 1, "text": "from app.core import api"}]))
    outcome = library.apply(REQUEST)
    assert outcome.ok is False and "no file was modified" in outcome.detail and "a.py:1" in outcome.detail
    assert [call[4] for call in runner.calls if call[3] == "arborescent.driver_libcst_scan"] == ["forms"]
    assert not any("libcst.tool" in call or call[4:5] == ["rewrite"] for call in runner.calls)


def test_apply_runs_every_rename_and_then_checks_nothing_is_left() -> None:
    library, runner = adapter()
    assert library.apply(REQUEST).ok is True
    modes = [call[4] if call[3] == "arborescent.driver_libcst_scan" else "rename" for call in runner.calls]
    assert modes == ["forms", "rewrite", "rename", "rename", "remaining"]


def test_references_left_behind_are_a_failure_and_are_listed() -> None:
    library, _ = adapter(Scripted(remaining=[{"file": "src/core/y.py", "line": 2, "text": "from app.core import f"}]))
    outcome = library.apply(REQUEST)
    assert outcome.ok is False and "src/core/y.py:2" in outcome.detail and "left" in outcome.detail


def test_a_failing_rename_reports_the_exception_libcst_printed_not_just_the_file() -> None:
    err = "Codemodding /new/src/core/x.py\nTraceback (most recent call last):\n  File \"a.py\", line 1, in f\nValueError: no such thing\n\nFailed to codemod /new/src/core/x.py\n"
    outcome = adapter(Scripted(rename_rc=1, rename_err=err))[0].apply(REQUEST)
    assert outcome.ok is False and "ValueError: no such thing" in outcome.detail and "src/core/x.py" in outcome.detail


def test_a_failing_rename_stops_the_run() -> None:
    library, runner = adapter(Scripted(rename_rc=1))
    outcome = library.apply(REQUEST)
    assert outcome.ok is False and "app.core.internal" in outcome.detail
    assert sum("libcst.tool" in call for call in runner.calls) == 1


# -- real LibCST ----------------------------------------------------------------------------------------------------------

needs_libcst = pytest.mark.skipif(not HAVE_LIBCST, reason="libcst is not installed")
REAL = (ModuleRename("app.core.internal", "core.internal"), ModuleRename("app.core.api", "core.api"), ModuleRename("app.core", "core"))


def tree(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(text, encoding="utf-8", newline="\n")


def real(root: Path) -> LibCstRename:
    return LibCstRename(SubprocessToolRunner())


def digest(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*.py"))}


BASE = {"core/__init__.py": "", "core/api.py": "def f() -> int:\n    return 1\n", "core/internal.py": "HIDDEN = 2\n"}


@needs_libcst
@pytest.mark.parametrize("line", [
    "from app.core import api, helper",
    "from app.core import api as a, helper as h, internal",
    "from app import core as c",  # would become `import core as c`, which LibCST's rename then corrupts
    "import core as c",
])
def test_real_scanner_finds_what_the_rewrite_cannot_do_provably_correctly(tmp_path: Path, line: str) -> None:
    tree(tmp_path, {**BASE, "core/use.py": f"x = 1\n{line}\n"})
    found = real(tmp_path).unsupported(RewriteRequest(tmp_path, (".",), REAL))
    assert [(f.file, f.line, f.text) for f in found] == [("core/use.py", 2, line)]


@needs_libcst
@pytest.mark.parametrize("line", ["from app.core import api", "from app.core import api as a", "from app.core import api, internal", "from app import core", "from app.core import *"])
def test_real_scanner_lets_the_forms_the_adapter_rewrites_through(tmp_path: Path, line: str) -> None:
    tree(tmp_path, {**BASE, "core/use.py": f"{line}\n"})
    assert real(tmp_path).unsupported(RewriteRequest(tmp_path, (".",), REAL)) == ()


@needs_libcst
@pytest.mark.parametrize("line", [
    "import app.core.api",
    "import app.core.api as a",
    "from app.core.api import f",
    "from app.core.api import *",
    "from . import api",
    "from .api import f",
    "import os",
    "from collections import OrderedDict",
])
def test_real_scanner_accepts_the_idioms_the_rename_handles(tmp_path: Path, line: str) -> None:
    tree(tmp_path, {**BASE, "core/use.py": f"{line}\n"})
    assert real(tmp_path).unsupported(RewriteRequest(tmp_path, (".",), REAL)) == ()


@needs_libcst
def test_real_scanner_reports_a_file_it_cannot_parse(tmp_path: Path) -> None:
    tree(tmp_path, {**BASE, "core/bad.py": "def (:\n"})
    found = real(tmp_path).unsupported(RewriteRequest(tmp_path, (".",), REAL))
    assert [f.file for f in found] == ["core/bad.py"] and "unparsable" in found[0].text


@pytest.fixture
def no_black(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """LibCST formats what it writes with ``black`` unless told not to; CI has none. Hide any that this machine has."""
    empty = tmp_path.parent / "empty-path"
    empty.mkdir(exist_ok=True)
    monkeypatch.setenv("PATH", str(empty))


@needs_libcst
def test_real_rename_rewrites_the_supported_idioms_without_black_and_without_reformatting(tmp_path: Path, no_black: None) -> None:
    tree(tmp_path, {
        **BASE,
        "core/use.py": "import app.core.api\nimport app.core.api as alias\nfrom app.core.api import f\nfrom app.core.internal import HIDDEN\n\n"
                       "X   =   app.core.api.f() + alias.f() + f() + HIDDEN\n",
    })
    outcome = real(tmp_path).apply(RewriteRequest(tmp_path, (".",), REAL))
    assert outcome.ok, outcome.detail
    text = (tmp_path / "core/use.py").read_text(encoding="utf-8")
    assert "app" not in text
    assert "from core.api import f" in text and "from core.internal import HIDDEN" in text and "core.api.f()" in text
    assert "X   =   " in text  # nothing but the renamed names changed: the code was not reformatted
    compile(text, "use.py", "exec")


@needs_libcst
def test_a_crash_of_the_rename_names_the_file_and_stops(tmp_path: Path, no_black: None) -> None:
    # LibCST's own package-level rename raises ParserSyntaxError on this form (an attribute of the renamed package).
    tree(tmp_path, {**BASE, "core/use.py": "from app.core import f\nx = f()\n"})
    outcome = real(tmp_path).apply(RewriteRequest(tmp_path, (".",), REAL))
    assert outcome.ok is False and "core/use.py" in outcome.detail and "app.core" in outcome.detail


@needs_libcst
def test_the_leftover_scan_finds_every_import_that_still_names_an_old_module(tmp_path: Path) -> None:
    tree(tmp_path, {**BASE, "core/use.py": "import app.core.api\nfrom app.core import f\nfrom app.core.api import g\nimport os\nfrom core.api import ok\n"})
    scanner = pytest.importorskip("arborescent.driver_libcst_scan")  # imports libcst, so only reachable when it is installed
    found = scanner.scan("remaining", tmp_path, ["."], frozenset(m.source for m in REAL))
    assert [(f["file"], f["line"]) for f in found] == [("core/use.py", 1), ("core/use.py", 2), ("core/use.py", 3)]


# -- #71: ``from <old> import <module>`` is rewritten by the adapter's own transformer (real LibCST) ---------------------------

def bound_and_used(text: str) -> tuple[set[str], set[str]]:
    """Names a module binds at its top level by imports, and every name it reads, from the standard library only."""
    tree = ast.parse(text)
    bound: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            bound |= {(a.asname or a.name.split(".")[0]) for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            bound |= {(a.asname or a.name) for a in node.names}
    return bound, {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}


def run_real(root: Path) -> tuple[bool, str]:
    outcome = real(root).apply(RewriteRequest(root, (".",), REAL))
    return outcome.ok, outcome.detail


@needs_libcst
def test_issue_71_reproduction_every_name_is_bound_afterwards(tmp_path: Path, no_black: None) -> None:
    tree(tmp_path, {**BASE, "core/use.py": "import app.core.api\nimport app.core.api as alias\nfrom app.core import api as api2\nfrom app.core.api import f\n\n"
                                          "X = app.core.api.f() + alias.f() + api2.f() + f()\n"})
    ok, detail = run_real(tmp_path)
    assert ok, detail
    text = (tmp_path / "core/use.py").read_text(encoding="utf-8")
    assert "app" not in text and "from core import api as api2" in text
    bound, used = bound_and_used(text)
    assert {"api2", "f", "core"} <= bound and not (used - bound)  # nothing read is unbound


@needs_libcst
@pytest.mark.parametrize(("before", "after"), [
    ("from app.core import api", "from core import api"),
    ("from app.core import api as a", "from core import api as a"),
    ("from app.core import api, internal", "from core import api, internal"),
    ("from app.core import (api,\n    internal as i)", "from core import (api,\n    internal as i)"),
    ("from app import core", "import core"),
    ("from app.core import *", "from core import *"),
])
def test_the_form_is_rewritten_and_keeps_every_binding(tmp_path: Path, no_black: None, before: str, after: str) -> None:
    tree(tmp_path, {**BASE, "core/use.py": f"{before}\n"})
    ok, detail = run_real(tmp_path)
    assert ok, detail
    assert (tmp_path / "core/use.py").read_text(encoding="utf-8") == f"{after}\n"


@needs_libcst
def test_a_module_that_changes_its_last_name_keeps_the_name_it_was_bound_to(tmp_path: Path, no_black: None) -> None:
    renames = (ModuleRename("app.core.api", "core.service"),)
    tree(tmp_path, {"core/use.py": "from app.core import api\nfrom app.core import api as a\nX = api.f() + a.f()\n"})
    outcome = real(tmp_path).apply(RewriteRequest(tmp_path, (".",), renames))
    assert outcome.ok, outcome.detail
    assert (tmp_path / "core/use.py").read_text(encoding="utf-8") == "from core import service as api\nfrom core import service as a\nX = api.f() + a.f()\n"


@needs_libcst
def test_several_names_are_split_when_their_new_names_differ_and_the_rest_stays(tmp_path: Path, no_black: None) -> None:
    renames = (ModuleRename("app.core.api", "core.service"), ModuleRename("app.core.internal", "other.internal"))
    tree(tmp_path, {"core/use.py": "if True:\n    from app.core import api, internal as i, helper  # keep\n    X = 1; y = 2\n"})
    outcome = real(tmp_path).apply(RewriteRequest(tmp_path, (".",), renames))
    assert outcome.ok, outcome.detail
    text = (tmp_path / "core/use.py").read_text(encoding="utf-8")
    assert text == "if True:\n    from core import service as api; from other import internal as i; from app.core import helper  # keep\n    X = 1; y = 2\n"
    compile(text, "use.py", "exec")


@needs_libcst
def test_a_renamed_package_with_a_name_that_is_not_a_renamed_module_is_refused_and_nothing_changes(tmp_path: Path) -> None:
    tree(tmp_path, {**BASE, "core/ok.py": "from app.core import api\n", "core/use.py": "from app.core import api, helper\n"})
    before = digest(tmp_path)
    ok, detail = run_real(tmp_path)
    assert not ok and "core/use.py:1" in detail and "core/ok.py" not in detail and "no file was modified" in detail
    assert digest(tmp_path) == before


@needs_libcst
@pytest.mark.parametrize("line", ["from . import api", "from .api import f", "from .. import core", "from app import other", "from app.other import api"])
def test_relative_and_unrelated_imports_are_left_alone(tmp_path: Path, no_black: None, line: str) -> None:
    tree(tmp_path, {**BASE, "core/use.py": f"{line}\n"})
    ok, detail = run_real(tmp_path)
    assert ok, detail
    assert (tmp_path / "core/use.py").read_text(encoding="utf-8") == f"{line}\n"


@needs_libcst
def test_a_local_name_that_shadows_the_new_module_name_is_not_touched(tmp_path: Path, no_black: None) -> None:
    # The binding name is preserved, so no use is renamed and nothing can start to collide with a local `core` or `service`.
    renames = (ModuleRename("app.core.api", "core.service"),)
    source = "from app.core import api\n\n\ndef run(core, service):\n    core = service\n    return api.f(core)\n"
    tree(tmp_path, {"core/use.py": source})
    assert real(tmp_path).apply(RewriteRequest(tmp_path, (".",), renames)).ok
    assert (tmp_path / "core/use.py").read_text(encoding="utf-8") == source.replace("from app.core import api", "from core import service as api")


@needs_libcst
def test_a_second_run_changes_nothing(tmp_path: Path, no_black: None) -> None:
    tree(tmp_path, {**BASE, "core/use.py": "from app.core import api as a, internal\nfrom app import core\nimport app.core.api\nX = a.f() + app.core.api.f()\n"})
    assert run_real(tmp_path)[0]
    once = digest(tmp_path)
    ok, detail = run_real(tmp_path)
    assert ok, detail
    assert digest(tmp_path) == once


@needs_libcst
def test_a_file_that_does_not_parse_is_listed_and_nothing_is_modified(tmp_path: Path) -> None:
    tree(tmp_path, {**BASE, "core/use.py": "from app.core import api\n", "core/bad.py": "def (:\n"})
    before = digest(tmp_path)
    ok, detail = run_real(tmp_path)
    assert not ok and "core/bad.py" in detail and "unparsable" in detail
    assert digest(tmp_path) == before


@needs_libcst
def test_libcst_own_rename_still_leaves_a_name_unbound_on_the_issue_71_input(tmp_path: Path, no_black: None) -> None:
    """The upstream behaviour that makes the adapter rewrite this form itself. If this starts failing, LibCST fixed it."""
    tree(tmp_path, {**BASE, "core/use.py": "import app.core.api\nimport app.core.api as alias\nfrom app.core import api as api2\nfrom app.core.api import f\n\n"
                                          "X = app.core.api.f() + alias.f() + api2.f() + f()\n"})
    command = LibCstRename(SubprocessToolRunner()).commands(RewriteRequest(tmp_path, (".",), (ModuleRename("app.core.api", "core.api"),)))[1]
    assert SubprocessToolRunner().run(list(command), tmp_path).returncode == 0
    text = (tmp_path / "core/use.py").read_text(encoding="utf-8")
    bound, used = bound_and_used(text)
    assert "api" in used and "api" not in bound  # `api.f()` is read and nothing binds `api`
