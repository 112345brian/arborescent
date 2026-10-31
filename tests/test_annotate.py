from __future__ import annotations

from pathlib import Path

from arborescent_helpers import mini_policy, write_files

from arborescent.adapters import FsScanner, LocalFileSystem, SystemClock
from arborescent.annotate import (
    AnnotatePlan,
    apply_annotations,
    insert_annotation,
    plan_annotations,
)
from arborescent.cli import main
from arborescent.config import policy_from_mapping
from arborescent.domain import ToolResult
from arborescent.engine import Services, run_own
from arborescent.engines import default_engines
from arborescent.facts import extract
from arborescent.testing import FakeFileSystem, FakeToolRunner
from arborescent.typecheckers import all_checkers


class GitFiles(FakeToolRunner):
    def __init__(self, files: list[str] | None = None, *, available: bool = True) -> None:
        super().__init__({"git"} if available else set())
        self.files = files or []

    def run(self, argv, cwd, timeout=600.0):  # type: ignore[no-untyped-def]
        self.calls.append(list(argv))
        if argv[0] == "git" and "ls-files" in argv:
            return ToolResult(0, "\n".join(self.files), "")
        return super().run(argv, cwd, timeout)


def make_services(runner: GitFiles) -> Services:
    return Services(FsScanner(runner=runner), runner, list(all_checkers().values()), LocalFileSystem(), SystemClock(), default_engines())


def test_insert_annotation_after_docstring_and_future_import() -> None:
    source = '"""Module doc."""\n\nfrom __future__ import annotations\n\nVALUE = 1\n'
    result = insert_annotation(source, "domain")
    assert result == (
        '"""Module doc."""\n\nfrom __future__ import annotations\n\n__arbor__ = {"ring": "domain"}\n\nVALUE = 1\n'
    )


def test_insert_annotation_after_docstring_only() -> None:
    source = '"""Module doc."""\n\nVALUE = 1\n'
    result = insert_annotation(source, "domain")
    assert result == '"""Module doc."""\n\n__arbor__ = {"ring": "domain"}\n\nVALUE = 1\n'


def test_insert_annotation_at_top_with_no_docstring_or_future_import() -> None:
    source = "VALUE = 1\n"
    result = insert_annotation(source, "domain")
    assert result == '__arbor__ = {"ring": "domain"}\n\nVALUE = 1\n'


def test_insert_annotation_result_parses_back_to_the_same_ring() -> None:
    source = '"""Doc."""\n\nfrom __future__ import annotations\n\nVALUE = 1\n'
    result = insert_annotation(source, "adapters")
    facts = extract(result)
    assert facts.arbor_ring == "adapters"
    assert facts.arbor_error is None


def test_plan_annotations_skips_already_annotated_files(repo: Path) -> None:
    write_files(repo, {"app/already.py": '__arbor__ = {"ring": "domain"}\n\nVALUE = 1\n'})
    runner = GitFiles(["app/__init__.py", "app/domain.py", "app/already.py"])
    services = make_services(runner)
    policy = mini_policy(classify={"app": "domain", "app.domain": "domain", "app.already": "domain"})

    _, scan = run_own(policy, repo, services, practices=False)
    plan = plan_annotations(policy, scan)

    assert "app/already.py" in plan.already_annotated
    assert not any(rel == "app/already.py" for _, rel, _ in plan.to_annotate)


def test_plan_annotations_reports_no_classify_match(repo: Path) -> None:
    write_files(repo, {"app/mystery.py": "VALUE = 1\n"})
    runner = GitFiles(["app/__init__.py", "app/domain.py", "app/mystery.py"])
    services = make_services(runner)
    policy = mini_policy(classify={"app": "domain", "app.domain": "domain"})

    _, scan = run_own(policy, repo, services, practices=False)
    plan = plan_annotations(policy, scan)

    assert "app/mystery.py" in plan.unmatched
    assert not any(rel == "app/mystery.py" for _, rel, _ in plan.to_annotate)


def test_plan_annotations_finds_classify_matches_without_annotation(repo: Path) -> None:
    runner = GitFiles(["app/__init__.py", "app/domain.py"])
    services = make_services(runner)
    policy = mini_policy(classify={"app": "domain", "app.domain": "domain"})

    _, scan = run_own(policy, repo, services, practices=False)
    plan = plan_annotations(policy, scan)

    assert ("app.domain", "app/domain.py", "domain") in plan.to_annotate


def test_plan_annotations_resolves_classify_match_within_its_owning_scope(repo: Path) -> None:
    runner = GitFiles(["app/__init__.py", "app/domain.py"])
    services = make_services(runner)
    policy = policy_from_mapping({
        "scopes": {
            "app": {
                "sources": ["app"], "source_roots": ["."],
                "rings": ["domain"],
                "classify": {"app": "domain", "app.domain": "domain"},
            },
        },
    })

    _, scan = run_own(policy, repo, services, practices=False)
    plan = plan_annotations(policy, scan)

    assert ("app.domain", "app/domain.py", "domain") in plan.to_annotate
    assert not plan.unmatched


def test_apply_annotations_writes_the_file_and_is_idempotent(repo: Path) -> None:
    runner = GitFiles(["app/__init__.py", "app/domain.py"])
    services = make_services(runner)
    policy = mini_policy(classify={"app": "domain", "app.domain": "domain"})

    _, scan = run_own(policy, repo, services, practices=False)
    plan = plan_annotations(policy, scan)
    written = apply_annotations(LocalFileSystem(), repo, plan)

    assert written == len(plan.to_annotate)
    content = (repo / "app/domain.py").read_text(encoding="utf-8")
    assert '__arbor__ = {"ring": "domain"}' in content

    _, scan2 = run_own(policy, repo, services, practices=False)
    plan2 = plan_annotations(policy, scan2)
    assert "app/domain.py" in plan2.already_annotated
    assert not any(rel == "app/domain.py" for _, rel, _ in plan2.to_annotate)


def test_apply_annotations_with_fake_filesystem() -> None:
    fs = FakeFileSystem()
    fs.add(Path("/r/app/domain.py"), "VALUE = 1\n")
    plan = AnnotatePlan(to_annotate=(("app.domain", "app/domain.py", "domain"),))

    written = apply_annotations(fs, Path("/r"), plan)

    assert written == 1
    assert '__arbor__ = {"ring": "domain"}' in fs.read_text(Path("/r/app/domain.py"))


def test_cli_annotate_is_dry_run_by_default(repo: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    runner = GitFiles(["app/__init__.py", "app/domain.py"])
    services = make_services(runner)
    config = repo / "arborescent.toml"
    config.write_text(
        'sources = ["app"]\nrings = ["domain", "composition"]\n'
        '[ring.domain]\nexternal = []\n[ring.composition]\nmay_import = ["*"]\nexternal = ["*"]\n'
        '[classify]\n"app" = "domain"\n"app.*" = "domain"\n',
        encoding="utf-8",
    )

    assert main(["--config", str(config), "annotate"], services) == 0

    output = capsys.readouterr().out
    assert "would annotate" in output
    assert "app/domain.py" in output
    assert "__arbor__" not in (repo / "app/domain.py").read_text(encoding="utf-8")


def test_cli_annotate_writes_with_yes(repo: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    runner = GitFiles(["app/__init__.py", "app/domain.py"])
    services = make_services(runner)
    config = repo / "arborescent.toml"
    config.write_text(
        'sources = ["app"]\nrings = ["domain", "composition"]\n'
        '[ring.domain]\nexternal = []\n[ring.composition]\nmay_import = ["*"]\nexternal = ["*"]\n'
        '[classify]\n"app" = "domain"\n"app.*" = "domain"\n',
        encoding="utf-8",
    )

    assert main(["--config", str(config), "annotate", "--yes"], services) == 0

    content = (repo / "app/domain.py").read_text(encoding="utf-8")
    assert '__arbor__ = {"ring": "domain"}' in content
    output = capsys.readouterr().out
    assert "annotated" in output

    # second run is a no-op: everything is already annotated
    assert main(["--config", str(config), "annotate", "--yes"], services) == 0
    output2 = capsys.readouterr().out
    assert "0 annotated" in output2
    assert "2 already annotated" in output2
