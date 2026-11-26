"""#216: the opt-in runtime measurement (``coupling startup --measure``) and its heavy third-party import report.

Most tests feed canned ``-X importtime`` output through a fake runner; the ones near the end run the real interpreter.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path

import pytest
from arborescent_helpers import services, write_files

from arborescent.adapters import SubprocessToolRunner
from arborescent.cli import main
from arborescent.coupling import (
    IMPORTTIME_MARKER,
    LOADED_MARKER,
    ImportRecord,
    package_costs,
    parse_importtime,
    parse_loaded_modules,
)
from arborescent.domain import ToolResult
from arborescent.testing import FakeToolRunner


def line(name: str, cumulative: int, depth: int = 0, own: int = 1) -> str:
    return f"import time: {own:9d} | {cumulative:10d} | {'  ' * depth}{name}\n"


HEADER = "import time: self [us] | cumulative | imported package\n"


def test_parse_rebuilds_the_tree_from_post_order_output() -> None:
    text = IMPORTTIME_MARKER + "\n" + line("c", 1, 2) + line("b", 2, 1) + line("d", 3, 1) + line("a", 9, 0) + line("e", 4, 0)
    found = {record.name: record for record in parse_importtime(text)}
    assert found["c"].ancestors == ("a", "b")
    assert found["d"].ancestors == ("a",)
    assert found["a"].ancestors == () and found["e"].ancestors == ()
    assert found["a"].cumulative_us == 9


def test_parse_ignores_everything_before_the_marker_headers_noise_and_partial_lines() -> None:
    text = HEADER + line("site", 99) + IMPORTTIME_MARKER + "\n" + HEADER
    text += "DeprecationWarning: x\nimport time:  12 |\n" + line("a", 5) + "import time:   1 |  2"
    assert [record.name for record in parse_importtime(text)] == ["a"]


def test_parse_of_empty_or_markerless_output_is_empty() -> None:
    assert parse_importtime("") == [] == parse_importtime(line("a", 1))


def test_a_deep_import_chain_does_not_recurse() -> None:
    text = IMPORTTIME_MARKER + "\n" + "".join(line(f"m{n}", n, n) for n in range(3000, -1, -1))
    assert len(parse_importtime(text)) == 3001


def test_package_cost_sums_only_the_outermost_records_of_a_package() -> None:
    records = [
        ImportRecord("heavy.core", 40, ("app", "heavy")),
        ImportRecord("heavy", 60, ("app",)),
        ImportRecord("heavy.extra", 15, ("app",)),  # imported later, outside `heavy`'s own record
        ImportRecord("dep", 30, ("app", "heavy")),
        ImportRecord("app", 200, ()),
    ]
    costs = package_costs(records)
    assert costs["heavy"] == 75 and costs["dep"] == 30


FILES = {
    "app/__init__.py": "",
    "app/cli.py": (
        "import typer\n\nfrom app import models\n\napp = typer.Typer()\n\n\n"
        "@app.command()\ndef export() -> None:\n    models.dump()\n\n\n"
        "@app.command()\ndef show() -> None:\n    print('x')\n"
    ),
    "app/models.py": "import pydantic\nimport json\n\n\ndef dump() -> None:\n    pydantic.BaseModel()\n",
    "app/backfill.py": "def run() -> None:\n    import boto3\n",
}
CANNED = (
    HEADER + IMPORTTIME_MARKER + "\n" + HEADER
    + line("app", 100)
    + line("typer", 60_000, 1)
    + line("pydantic", 180_000, 2)
    + line("app.models", 190_000, 1)
    + line("app.cli", 260_000)
    + f"\n{LOADED_MARKER} sys app typer pydantic app.models app.cli \n"
)


def project(tmp_path: Path, files: Mapping[str, str] | None = None, extra: str = "") -> list[str]:
    write_files(tmp_path, {**FILES, **(files or {})})
    config = tmp_path / "arborescent.toml"
    config.write_text(
        'sources = ["app"]\nrings = ["domain"]\n[ring.domain]\nexternal = ["*"]\n[classify]\n"app" = "domain"\n"app.*" = "domain"\n'
        '[coupling]\nentrypoints = ["app.cli"]\n' + extra,
        encoding="utf-8",
    )
    return ["--config", str(config), "coupling"]


def fake(stderr: str = CANNED, returncode: int | None = 0) -> FakeToolRunner:
    return FakeToolRunner(installed={sys.executable}, results={sys.executable: ToolResult(returncode, "", stderr)})


def test_measure_reports_time_and_heavy_imports_with_their_commands(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    runner = fake()
    assert main([*project(tmp_path), "startup", "--measure"], services(runner)) == 0
    out = capsys.readouterr().out
    assert "app.cli: measured 260.1 ms, 3 project modules loaded" in out
    assert "app.cli -> app.models pulls 1 module(s), 190.0 ms" in out
    assert "pydantic 180.0 ms: imported by app.models; only used by app.cli:export" in out
    assert "typer 60.0 ms: imported by app.cli; used by no command found" in out
    (call,) = runner.calls
    assert call[:4] == [sys.executable, "-X", "importtime", "-c"]
    assert "__import__('app.cli')" in call[4] and repr(str(tmp_path.resolve())) in call[4]


def test_the_threshold_filters_and_none_is_said(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base = project(tmp_path)
    assert main([*base, "startup", "--measure", "--threshold-ms", "100"], services(fake())) == 0
    out = capsys.readouterr().out
    assert "pydantic 180.0 ms" in out and "typer 60.0" not in out
    assert main([*base, "startup", "--measure", "--threshold-ms", "500"], services(fake())) == 0
    assert "heavy third-party imports (>= 500 ms): none" in capsys.readouterr().out


def test_a_lazy_mount_and_several_commands_are_listed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    files = {
        "app/cli.py": FILES["app/cli.py"].replace("print('x')", "models.dump()"),
        "app/backfill.py": "from app import models\n",
    }
    base = project(tmp_path, files, '[coupling.lazy_mounts]\nbackfill = "app.backfill:app"\n')
    assert main([*base, "startup", "--measure"], services(fake())) == 0
    assert "pydantic 180.0 ms: imported by app.models; used by 3 commands: app.cli:export, app.cli:show, backfill" in capsys.readouterr().out


def test_a_command_that_imports_the_package_in_its_body_uses_it(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    files = {"app/cli.py": FILES["app/cli.py"].replace("models.dump()", "pass").replace("print('x')", "import pydantic")}
    assert main([*project(tmp_path, files), "startup", "--measure"], services(fake())) == 0
    assert "only used by app.cli:show" in capsys.readouterr().out


def test_a_handler_registered_from_another_module_counts_as_a_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    files = {
        "app/cli.py": "from app import handlers, models\n\np.set_defaults(func=handlers.export)\n",
        "app/handlers.py": "from app import models\n\n\ndef export(args):\n    models.dump()\n",
    }
    assert main([*project(tmp_path, files), "startup", "--measure"], services(fake())) == 0
    assert "only used by app.handlers:export" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("stderr", "returncode", "expected"),
    [
        (CANNED + "Traceback (most recent call last):\nModuleNotFoundError: No module named 'typer'\n", 1, "No module named 'typer'"),
        ("python: timed out after 1s", -1, "timed out after 1s"),
        ("python: not installed", None, "not installed"),
        ("", 0, "no import-time output"),
        ("\x00\n", 1, "exit code 1"),
        (f"{IMPORTTIME_MARKER}\n\n{LOADED_MARKER} sys app \n", 3, "exit code 3"),  # a silent sys.exit(3): not the list
        (CANNED + "KeyError: 'x'\n", -1, "KeyError: 'x'"),
        (IMPORTTIME_MARKER + "\n" + line("app.cli", 5), 0, "no list of loaded modules"),
    ],
)
def test_a_failed_measurement_is_said_and_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], stderr: str, returncode: int | None, expected: str
) -> None:
    assert main([*project(tmp_path), "startup", "--measure"], services(fake(stderr, returncode))) == 1
    out = capsys.readouterr().out
    assert "app.cli: measurement failed" in out and expected in out


class Scripted(FakeToolRunner):
    """Succeeds once, then fails; remembers the timeouts it was given."""

    def run(self, argv: Sequence[str], cwd: Path, timeout: float = 600.0) -> ToolResult:
        self.calls.append(list(argv))
        return ToolResult(0, "", CANNED) if len(self.calls) == 1 else ToolResult(1, "", "boom")


def test_a_second_entrypoint_failing_still_fails_the_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base = project(tmp_path)
    config = tmp_path / "arborescent.toml"
    config.write_text(config.read_text(encoding="utf-8").replace('["app.cli"]', '["app.cli", "app.models"]'), encoding="utf-8")
    assert main([*base, "startup", "--measure"], services(Scripted())) == 1
    out = capsys.readouterr().out
    assert "app.cli: measured" in out and "app.models: measurement failed: boom" in out


def test_an_interpreter_that_cannot_start_is_a_failure_not_a_crash(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    class Broken(FakeToolRunner):
        def run(self, argv: Sequence[str], cwd: Path, timeout: float = 600.0) -> ToolResult:
            raise PermissionError("denied")

    assert main([*project(tmp_path), "startup", "--measure"], services(Broken())) == 1
    assert "measurement failed: denied" in capsys.readouterr().out


def test_python_and_timeout_are_passed_on(tmp_path: Path) -> None:
    timeouts: list[float] = []

    class Timed(FakeToolRunner):
        def run(self, argv: Sequence[str], cwd: Path, timeout: float = 600.0) -> ToolResult:
            timeouts.append(timeout)
            return super().run(argv, cwd, timeout)

    runner = Timed(installed={"py9"}, results={"py9": ToolResult(0, "", CANNED)})
    assert main([*project(tmp_path), "startup", "--measure", "--python", "py9", "--timeout", "7"], services(runner)) == 0
    assert runner.calls[0][0] == "py9" and timeouts == [7]


@pytest.mark.parametrize("option", [["--threshold-ms", "5"], ["--python", "py"], ["--timeout", "5"]])
def test_measurement_options_without_measure_are_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str], option: list[str]) -> None:
    runner = fake()
    assert main([*project(tmp_path), "startup", *option], services(runner)) == 2
    assert "--measure" in capsys.readouterr().err and not runner.calls


def test_nothing_runs_the_project_unless_measure_is_asked_for(tmp_path: Path) -> None:
    runner = fake()
    base = project(tmp_path, extra='[coupling.closure_max]\n"app.cli" = 9\n')
    for action in (["startup"], ["baseline"], ["check"], ["report"]):
        assert main([*base, *action], services(runner)) == 0
    main([*base[:2], "check", "--no-tools"], services(runner))
    main([*base[:2], "check"], services(runner))
    assert not [call for call in runner.calls if "importtime" in call]


def test_a_real_interpreter_produces_output_the_report_reads(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    write_files(tmp_path / "installed", {"fakeheavy/__init__.py": "import time\n\ntime.sleep(0.02)\n"})
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "installed"))  # outside the project, as site-packages is
    files = {"app/cli.py": "from app import models\n", "app/models.py": "import fakeheavy\n"}
    real = replace(services(), runner=SubprocessToolRunner())
    assert main([*project(tmp_path / "project", files), "startup", "--measure", "--threshold-ms", "1"], real) == 0
    out = capsys.readouterr().out
    assert "app.cli: measured" in out and "3 project modules loaded" in out
    assert "fakeheavy" in out and "imported by app.models" in out


def test_the_loaded_list_is_the_last_one_after_the_marker() -> None:
    before = f"{LOADED_MARKER} early\n"
    assert parse_loaded_modules("") is None and parse_loaded_modules(before + IMPORTTIME_MARKER + "\n") is None
    text = before + IMPORTTIME_MARKER + f"\nnoise\n{LOADED_MARKER} a \r\n{LOADED_MARKER} a a.b \r\nlater\n"
    assert parse_loaded_modules(text) == {"a", "a.b"}


def real_run(tmp_path: Path, capsys: pytest.CaptureFixture[str], files: Mapping[str, str], *options: str, code: int = 0) -> str:
    base = project(tmp_path, files)
    real = replace(services(), runner=SubprocessToolRunner())
    assert main([*base, "startup", "--measure", *options], real) == code
    return capsys.readouterr().out


def test_modules_loaded_by_importlib_are_counted_though_they_have_no_time(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    files = {
        "app/__init__.py": "",
        "app/cli.py": "import importlib\n\nimportlib.import_module('app.dynamic')\n",
        "app/dynamic.py": "",
        "app/__main__.py": "",
    }
    out = real_run(tmp_path, capsys, files)
    assert "app.cli: 2 project modules" in out  # static: app and app.cli
    assert "3 project modules loaded" in out


@pytest.mark.parametrize(
    ("source", "loaded"),
    [
        ("raise SystemExit(0)\n", 1),  # `app` only: a module whose import raised is no longer in `sys.modules`
        ("import sys\n\nsys.stderr.write('no newline')\n", 2),
        ("import sys\n\nsys.stderr.close()\n", 2),
        ("import io\nimport sys\n\nsys.stderr = io.StringIO()\n", 2),
    ],
)
def test_an_import_that_exits_cleanly_or_takes_over_stderr_is_still_measured(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], source: str, loaded: int
) -> None:
    assert f"{loaded} project modules loaded" in real_run(tmp_path, capsys, {"app/cli.py": source})


@pytest.mark.parametrize(
    ("source", "options", "expected"),
    [
        ("raise SystemExit(3)\n", (), "exit code 3"),
        ("raise RuntimeError('broken')\n", (), "RuntimeError: broken"),
        ("import os\n\nos._exit(0)\n", (), "no list of loaded modules"),
        ("import time\n\ntime.sleep(60)\n", ("--timeout", "3"), "timed out"),
    ],
)
def test_a_real_failed_or_timed_out_run_is_still_a_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], source: str, options: tuple[str, ...], expected: str
) -> None:
    out = real_run(tmp_path, capsys, {"app/cli.py": source}, *options, code=1)
    assert "app.cli: measurement failed" in out and expected in out and "project modules loaded" not in out
