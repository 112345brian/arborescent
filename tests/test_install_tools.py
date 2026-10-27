from __future__ import annotations

import os
import shlex
import sys
import tomllib
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest
from arborescent_helpers import mini_policy

from arborescent.adapters import (
    FsScanner,
    LocalFileSystem,
    SubprocessToolRunner,
    SystemClock,
    resolve_executable,
)
from arborescent.cli import (
    _check,
    _install_tools,
    _parser,
    _setup_install_command,
    main,
)
from arborescent.domain import ToolResult
from arborescent.engine import Services
from arborescent.engines import default_engines
from arborescent.install_tools import (
    TOOL_REQUIREMENTS,
)
from arborescent.installer_adapter import (
    _shell_join,
    include_poetry_arborescent_group_in_dev,
)
from arborescent.testing import FakeToolRunner
from arborescent.typecheckers import all_checkers


def test_tool_requirements_match_declared_optional_dependencies() -> None:
    project = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8"))
    extras = project["project"]["optional-dependencies"]
    declared = {requirement.split(">", 1)[0].split("=", 1)[0]: requirement for values in extras.values() for requirement in values}
    for requirement in TOOL_REQUIREMENTS.values():
        assert declared[requirement.split(">", 1)[0]] == requirement


def test_tool_requirements_are_real_pip_distributions() -> None:
    root = Path(__file__).parents[1]
    runner = SubprocessToolRunner()
    pip = runner.run([sys.executable, "-m", "pip", "--version"], root)
    if pip.returncode == 0:
        command = [sys.executable, "-m", "pip", "install", "--dry-run", "--no-deps", "--no-index", *TOOL_REQUIREMENTS.values()]
    elif runner.available("uv"):
        command = ["uv", "pip", "install", "--dry-run", "--no-deps", "--offline", "--python", sys.executable, *TOOL_REQUIREMENTS.values()]
    else:
        pytest.skip("pip or uv is required for the package metadata dry-run")
    result = runner.run(command, root)
    assert result.returncode == 0, result.stderr or result.stdout


def test_windows_install_hint_is_invokable_from_powershell(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        "arborescent.installer_adapter.os",
        SimpleNamespace(name="nt"),
    )
    monkeypatch.setattr(
        "arborescent.installer_adapter.sys",
        SimpleNamespace(executable=r"C:\Program Files\Python\python.exe"),
    )

    command = _setup_install_command(mini_policy(), tmp_path, _services(FakeToolRunner()), None)

    assert command == "& 'C:\\Program Files\\Python\\python.exe' -m pip install 'import-linter>=2.0' 'ruff>=0.16.3' 'tach>=0.30' 'ty>=0.0.50'"


def test_script_resolution_prefers_the_active_interpreters_scripts_directory(tmp_path: Path) -> None:
    scripts = tmp_path / "Scripts" if os.name == "nt" else tmp_path / "bin"
    scripts.mkdir()
    name = "ruff.exe" if os.name == "nt" else "ruff"
    candidate = scripts / name
    candidate.write_text("tool", encoding="utf-8")
    if os.name != "nt":
        candidate.chmod(0o755)

    resolved = resolve_executable("ruff", scripts_dir=scripts, path_lookup=lambda _: None)

    assert resolved == str(candidate)


def test_script_resolution_falls_back_to_path() -> None:
    assert resolve_executable("ruff", scripts_dir=Path("/missing"), path_lookup=lambda _: "/path/ruff") == "/path/ruff"


def test_script_resolution_checks_poetry_environment_before_path(tmp_path: Path) -> None:
    scripts = tmp_path / "poetry-venv" / ("Scripts" if os.name == "nt" else "bin")
    scripts.mkdir(parents=True)
    name = "ruff.exe" if os.name == "nt" else "ruff"
    candidate = scripts / name
    candidate.write_text("tool", encoding="utf-8")
    if os.name != "nt":
        candidate.chmod(0o755)

    assert resolve_executable("ruff", scripts_dirs=(Path("/missing"), scripts), path_lookup=lambda _: None) == str(candidate)


def test_subprocess_runner_discovers_tools_in_the_poetry_environment(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    (tmp_path / "pyproject.toml").write_text('[tool.poetry]\nname = "sample"\n', encoding="utf-8")
    scripts = tmp_path / "poetry-venv" / ("Scripts" if os.name == "nt" else "bin")
    scripts.mkdir(parents=True)
    executable = scripts / ("ruff.exe" if os.name == "nt" else "ruff")
    executable.write_text("tool", encoding="utf-8")
    if os.name != "nt":
        executable.chmod(0o755)
    calls: list[list[str]] = []

    def run(argv, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(argv))
        return SimpleNamespace(returncode=0, stdout=f"{scripts / ('python.exe' if os.name == 'nt' else 'python')}\n")

    monkeypatch.setattr("arborescent.tool_runner.shutil.which", lambda name: "/usr/bin/poetry" if name == "poetry" else None)
    monkeypatch.setattr("arborescent.tool_runner.subprocess.run", run)

    runner = SubprocessToolRunner(tmp_path)

    assert runner.available("ruff")
    assert calls == [["/usr/bin/poetry", "env", "info", "--executable"]]


class PipRunner(FakeToolRunner):
    def __init__(self) -> None:
        super().__init__()
        self.executed: list[list[str]] = []

    def available(self, executable: str) -> bool:
        return executable in self.installed

    def run(self, argv, cwd, timeout=600.0):  # type: ignore[no-untyped-def]
        self.calls.append(list(argv))
        self.executed.append(list(argv))
        if argv[0] == sys.executable and argv[1:3] == ["-m", "pip"]:
            if argv[3] == "--version" or argv[3] == "check":
                return ToolResult(0, "ok", "")
            if argv[3] == "install":
                self.installed.update({"lint-imports", "tach", "ruff", "ty"})
                return ToolResult(0, "installed", "")
        return ToolResult(1, "", "unexpected command")


def _services(runner: FakeToolRunner) -> Services:
    return Services(
        FsScanner(), runner, list(all_checkers().values()), LocalFileSystem(), SystemClock(), default_engines()
    )


def test_install_tools_installs_only_missing_selected_packages_and_checks_environment(tmp_path: Path) -> None:
    runner = PipRunner()
    services = _services(runner)

    result = _install_tools(Namespace(type_checker=None, yes=True), mini_policy(), tmp_path, services)

    assert result == 0
    install = next(call for call in runner.executed if call[0] == sys.executable and call[3:4] == ["install"])
    assert install[4:] == ["import-linter>=2.0", "ruff>=0.16.3", "tach>=0.30", "ty>=0.0.50"]
    assert "--upgrade" not in install
    assert [sys.executable, "-m", "pip", "check"] in runner.executed


def test_install_tools_decline_leaves_environment_unchanged(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    runner = PipRunner()
    services = _services(runner)
    monkeypatch.setattr("arborescent.cli_install._interactive_install_allowed", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "n")

    result = _install_tools(Namespace(type_checker=None, yes=False), mini_policy(), tmp_path, services)

    assert result == 1
    assert not any("install" in call for call in runner.executed)
    assert runner.installed == set()


class UvRunner(PipRunner):
    def __init__(self) -> None:
        super().__init__()
        self.installed.add("uv")

    def run(self, argv, cwd, timeout=600.0):  # type: ignore[no-untyped-def]
        if argv[0] == sys.executable:
            self.executed.append(list(argv))
            return ToolResult(1, "", "No module named pip")
        self.executed.append(list(argv))
        if argv[:3] == ["uv", "pip", "install"]:
            self.installed.update({"lint-imports", "tach", "ruff", "ty"})
            return ToolResult(0, "installed", "")
        if argv[:3] == ["uv", "pip", "check"]:
            return ToolResult(0, "ok", "")
        return ToolResult(1, "", "unexpected command")


def test_install_tools_uses_uv_for_the_active_interpreter_without_pip(tmp_path: Path) -> None:
    runner = UvRunner()
    services = _services(runner)

    result = _install_tools(Namespace(type_checker=None, yes=True), mini_policy(), tmp_path, services)

    assert result == 0
    install = next(call for call in runner.executed if call[:3] == ["uv", "pip", "install"])
    assert install[3:5] == ["--python", sys.executable]
    assert "--upgrade" not in install
    assert ["uv", "pip", "check", "--python", sys.executable] in runner.executed


def test_install_tools_adds_poetry_checkers_to_a_dev_included_arborescent_group(tmp_path: Path) -> None:
    project = tmp_path / "pyproject.toml"
    project.write_text(
        '[tool.poetry]\nname = "sample"\nversion = "0.1.0"\n\n'
        '[tool.poetry.dependencies]\npython = ">=3.11"\n\n'
        '[tool.poetry.group.dev]\ninclude-groups = ["docs"] # keep the existing group\n\n'
        '[tool.poetry.group.dev.dependencies]\npytest = "^8"\n',
        encoding="utf-8",
    )
    (tmp_path / "poetry.lock").write_text("", encoding="utf-8")

    class PoetryRunner(FakeToolRunner):
        def __init__(self) -> None:
            super().__init__()
            self.installed.add("poetry")

        def run(self, argv, cwd, timeout=600.0):  # type: ignore[no-untyped-def]
            self.calls.append(list(argv))
            if argv[:3] == ["poetry", "add", "--group"]:
                self.installed.update({"lint-imports", "tach", "ruff", "ty"})
                return ToolResult(0, "added", "")
            if argv == ["poetry", "check", "--lock"]:
                return ToolResult(0, "valid", "")
            return ToolResult(1, "", "unexpected command")

    runner = PoetryRunner()
    result = _install_tools(Namespace(type_checker=None, yes=True), mini_policy(), tmp_path, _services(runner))

    assert result == 0
    assert ["poetry", "add", "--group", "arborescent", "import-linter>=2.0", "ruff>=0.16.3", "tach>=0.30", "ty>=0.0.50"] in runner.calls
    assert ["poetry", "check", "--lock"] in runner.calls
    configured = tomllib.loads(project.read_text(encoding="utf-8"))
    assert configured["tool"]["poetry"]["group"]["dev"]["include-groups"] == ["docs", "arborescent"]
    assert "# keep the existing group" in project.read_text(encoding="utf-8")
    assert configured["tool"]["poetry"]["group"]["arborescent"]["optional"] is True


def test_poetry_pep735_dev_group_includes_arborescent_without_replacing_other_dependencies(tmp_path: Path) -> None:
    project = tmp_path / "pyproject.toml"
    project.write_text(
        '[tool.poetry]\nname = "sample"\nversion = "0.1.0"\n\n'
        '[dependency-groups]\ndev = [\n    "pytest>=8" # keep this comment\n]\n',
        encoding="utf-8",
    )
    (tmp_path / "poetry.lock").write_text("", encoding="utf-8")

    include_poetry_arborescent_group_in_dev(tmp_path, LocalFileSystem())

    configured = tomllib.loads(project.read_text(encoding="utf-8"))
    assert configured["dependency-groups"]["dev"] == [
        {"include-group": "arborescent"},
        "pytest>=8",
    ]
    assert "# keep this comment" in project.read_text(encoding="utf-8")


def test_install_tools_fails_if_installer_fails_or_dependencies_remain_broken(tmp_path: Path) -> None:
    class BrokenPip(PipRunner):
        def __init__(self, fail_on: str) -> None:
            super().__init__()
            self.fail_on = fail_on

        def run(self, argv, cwd, timeout=600.0):  # type: ignore[no-untyped-def]
            self.executed.append(list(argv))
            if argv[0] == sys.executable and argv[3:4] == ["--version"]:
                return ToolResult(0, "pip", "")
            if argv[0] == sys.executable and argv[3:4] == ["install"]:
                if self.fail_on == "install":
                    return ToolResult(1, "", "resolver error")
                self.installed.update({"lint-imports", "tach", "ruff", "ty"})
                return ToolResult(0, "installed", "")
            if argv[0] == sys.executable and argv[3:4] == ["check"]:
                return ToolResult(1, "", "broken dependency") if self.fail_on == "check" else ToolResult(0, "ok", "")
            return ToolResult(1, "", "unexpected")

    for fail_on in ("install", "check"):
        runner = BrokenPip(fail_on)
        result = _install_tools(Namespace(type_checker=None, yes=True), mini_policy(), tmp_path, _services(runner))
        assert result == 1


def test_install_tools_does_not_report_success_if_executable_is_still_missing(tmp_path: Path) -> None:
    class NoScripts(PipRunner):
        def run(self, argv, cwd, timeout=600.0):  # type: ignore[no-untyped-def]
            self.executed.append(list(argv))
            if argv[0] == sys.executable and argv[3:4] == ["--version"]:
                return ToolResult(0, "pip", "")
            if argv[0] == sys.executable and argv[3:4] == ["install"]:
                return ToolResult(0, "installed", "")
            if argv[0] == sys.executable and argv[3:4] == ["check"]:
                return ToolResult(0, "ok", "")
            return ToolResult(1, "", "unexpected")

    runner = NoScripts()
    result = _install_tools(Namespace(type_checker=None, yes=True), mini_policy(), tmp_path, _services(runner))

    assert result == 1


def test_install_tools_is_non_interactive_without_yes(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    runner = PipRunner()
    services = _services(runner)

    result = _install_tools(Namespace(type_checker=None, yes=False), mini_policy(), tmp_path, services)

    assert result == 1
    assert any(call[3:4] == ["--version"] for call in runner.executed)
    assert not any(call[3:4] == ["install"] for call in runner.executed)
    assert "Not installing in a non-interactive session" in capsys.readouterr().out


def test_install_tools_reports_when_neither_pip_nor_uv_can_target_the_environment(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    runner = FakeToolRunner()
    result = _install_tools(Namespace(type_checker=None, yes=True), mini_policy(), tmp_path, _services(runner))

    assert result == 1
    assert "No supported installer found" in capsys.readouterr().out


def test_poetry_project_without_poetry_prints_the_group_install_command(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    (tmp_path / "pyproject.toml").write_text('[tool.poetry]\nname = "sample"\n', encoding="utf-8")
    services = _services(FakeToolRunner())

    result = _install_tools(Namespace(type_checker=None, yes=True), mini_policy(), tmp_path, services)

    output = capsys.readouterr().out
    assert result == 1
    assert "poetry add --group arborescent" in output
    assert "No supported installer found" in output


@pytest.mark.parametrize("argv", [["check"], ["check", "--require-tools"]])
def test_check_reports_install_tools_without_running_an_installer(repo: Path, capsys, argv: list[str]) -> None:  # type: ignore[no-untyped-def]
    runner = FakeToolRunner()
    services = Services(
        FsScanner(), runner, list(all_checkers().values()), LocalFileSystem(), SystemClock(), default_engines()
    )

    result = _check(_parser().parse_args(argv), mini_policy(), repo, services)

    output = capsys.readouterr().out
    assert "SETUP  missing checker(s):" in output
    prefix = [sys.executable, "-m", "pip", "install"]
    assert _shell_join(prefix, shlex.join(prefix), windows=os.name == "nt") in output
    assert "arborescent check --install-tools" in output
    assert not any(call[3:4] == ["install"] or call[:3] == ["uv", "pip", "install"] for call in runner.calls)
    if "--require-tools" in argv:
        assert result == 1


def test_check_install_tools_flags_are_opt_in_and_install_before_running_checks(repo: Path) -> None:
    args = _parser().parse_args(["check", "--install-tools", "--yes"])
    runner = PipRunner()
    services = _services(runner)

    _check(args, mini_policy(), repo, services)

    install_index = next(index for index, call in enumerate(runner.executed) if call[3:4] == ["install"])
    assert any(call[0] in {"lint-imports", "tach", "ruff", "ty"} for call in runner.calls[install_index + 1 :])


def test_check_install_tools_without_yes_uses_default_no_prompt(repo: Path, monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    args = _parser().parse_args(["check", "--install-tools"])
    runner = PipRunner()
    prompts: list[str] = []
    monkeypatch.setattr("arborescent.cli_install._interactive_install_allowed", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or "")

    _check(args, mini_policy(), repo, _services(runner))

    assert runner.installed == set()
    assert not any(call[3:4] == ["install"] for call in runner.executed)
    assert prompts == ["Install these check tools into the active Python environment? [y/N] "]


def test_check_install_tools_fails_when_the_requested_install_fails(repo: Path) -> None:
    class FailedInstall(PipRunner):
        def run(self, argv, cwd, timeout=600.0):  # type: ignore[no-untyped-def]
            self.calls.append(list(argv))
            self.executed.append(list(argv))
            if argv[0] == sys.executable and argv[1:4] == ["-m", "pip", "--version"]:
                return ToolResult(0, "pip", "")
            if argv[0] == sys.executable and argv[1:4] == ["-m", "pip", "install"]:
                return ToolResult(1, "", "resolver failed")
            return super().run(argv, cwd, timeout)

    args = _parser().parse_args(["check", "--install-tools", "--yes"])
    result = _check(args, mini_policy(), repo, _services(FailedInstall()))

    assert result == 1


def test_policy_disabled_tools_do_not_trigger_install_or_setup_hint(repo: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    args = _parser().parse_args(["check", "--install-tools", "--yes"])
    runner = PipRunner()
    policy = mini_policy(disabled_tools=["import-linter", "tach", "ruff", "ty"])

    result = _check(args, policy, repo, _services(runner))

    output = capsys.readouterr().out
    assert result == 0
    assert "CONSISTENT configured Arborescent checks pass" in output
    assert "no check tools selected" in output
    assert "SETUP" not in output
    assert runner.executed == []


def test_profile_failure_can_still_report_consistency_with_disabled_tools(repo: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    args = _parser().parse_args(["check", "--require-profile"])
    policy = mini_policy(disabled_tools=["import-linter", "tach", "ruff", "ty"])

    result = _check(args, policy, repo, _services(FakeToolRunner()))

    output = capsys.readouterr().out
    assert result == 1
    assert "CONSISTENT configured Arborescent checks pass" in output
    assert "PROFILE FAIL required profile tool ruff is disabled by policy" in output
    assert "PROFILE FAIL required profile tool tach is disabled by policy" in output
    assert "CERTIFIED" not in output


def test_disabled_checker_is_omitted_from_install_prompt_and_command(
    tmp_path: Path, monkeypatch, capsys
) -> None:  # type: ignore[no-untyped-def]
    runner = PipRunner()
    monkeypatch.setattr("arborescent.cli_install._interactive_install_allowed", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "n")

    result = _install_tools(
        Namespace(type_checker=None, yes=False),
        mini_policy(disabled_tools=["ruff"]),
        tmp_path,
        _services(runner),
    )

    output = capsys.readouterr().out
    assert result == 1
    assert "missing: ruff" not in output
    assert "'ruff>=0.16.3'" not in output
    assert "missing: tach" in output


def test_check_yes_requires_install_tools_flag() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["check", "--yes"])

    assert exc.value.code == 2


def test_check_setup_hint_contains_the_active_environment_install_command(tmp_path: Path) -> None:
    runner = FakeToolRunner()

    command = _setup_install_command(mini_policy(), tmp_path, _services(runner), None)

    expected = [sys.executable, "-m", "pip", "install", "import-linter>=2.0", "ruff>=0.16.3", "tach>=0.30", "ty>=0.0.50"]
    assert command == _shell_join(expected, shlex.join(expected), windows=os.name == "nt")
    assert runner.calls == [[sys.executable, "-m", "pip", "--version"]]
