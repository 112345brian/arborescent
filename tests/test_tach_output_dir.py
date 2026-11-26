"""``tach.toml`` is written where tach can use it, whatever ``output_dir`` says.

Tach reads only the files below the folder that holds its config, so a ``tach.toml`` written into a nested ``output_dir``
(every source root spelled ``../...``) checked nothing and passed.
"""

from __future__ import annotations

import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest
from arborescent_helpers import MINI_FILES, mini_policy, services, write_files

from arborescent.engine import Options, drift_report, run_own, write_generated

NESTED = "cfg/tools"


def _repo(tmp_path: Path, **extra: str) -> Path:
    write_files(tmp_path, {**MINI_FILES, **extra})
    return tmp_path


def _notes(root: Path, output_dir: str) -> list[str]:
    policy, svc = mini_policy(output_dir=output_dir), services()
    _, scan = run_own(policy, root, svc, practices=False)
    return drift_report(policy, root, svc, scan, Options(tools=False))


def test_a_nested_output_dir_still_gets_tach_toml_at_the_root(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    written = write_generated(mini_policy(output_dir=NESTED), root, services())

    assert "tach.toml" in written
    assert f"{NESTED}/tach.toml" not in written and not (root / NESTED / "tach.toml").exists()
    assert f"{NESTED}/ruff.toml" in written  # everything else still goes where output_dir says
    roots = tomllib.loads((root / "tach.toml").read_text(encoding="utf-8"))["source_roots"]
    assert roots and not any(entry.startswith("..") for entry in roots)


def test_the_default_output_dir_is_unchanged(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    assert "tach.toml" in write_generated(mini_policy(), root, services())


def test_an_existing_root_tach_toml_is_never_changed(tmp_path: Path) -> None:
    root = _repo(tmp_path, **{"tach.toml": "source_roots = ['.']\n"})
    assert "tach.toml" not in write_generated(mini_policy(output_dir=NESTED), root, services())
    assert (root / "tach.toml").read_text(encoding="utf-8") == "source_roots = ['.']\n"


@pytest.mark.parametrize("output_dir", [".", NESTED])
def test_a_disabled_tach_gets_no_new_root_file_from_a_nested_output_dir(tmp_path: Path, output_dir: str) -> None:
    root = _repo(tmp_path)
    written = write_generated(mini_policy(output_dir=output_dir, disabled_tools=["tach"]), root, services())
    # the default folder behaves as before; a nested one must not start dropping a file at the root for an unused tool
    assert ("tach.toml" in written) is (output_dir == ".")


def test_a_project_configuring_tach_in_pyproject_is_not_shadowed(tmp_path: Path) -> None:
    root = _repo(tmp_path, **{"pyproject.toml": "[tool.tach]\nsource_roots = ['.']\n"})
    assert "tach.toml" not in write_generated(mini_policy(output_dir=NESTED), root, services())
    assert not (root / "tach.toml").exists()


@pytest.mark.parametrize("pyproject", ["", "[project]\nname = 'x'\n", "not [ toml", "tool = 1\n"])
def test_a_pyproject_without_a_tach_table_does_not_block_the_file(tmp_path: Path, pyproject: str) -> None:
    root = _repo(tmp_path, **{"pyproject.toml": pyproject})
    assert "tach.toml" in write_generated(mini_policy(output_dir=NESTED), root, services())


def test_a_leftover_generated_nested_tach_toml_is_reported(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    write_generated(mini_policy(output_dir=NESTED), root, services())
    shutil.copy(root / "tach.toml", root / NESTED / "tach.toml")

    assert any(note.startswith(f"{NESTED}/tach.toml:") and "delete" in note for note in _notes(root, NESTED))


def test_a_hand_written_nested_tach_toml_is_left_alone(tmp_path: Path) -> None:
    root = _repo(tmp_path, **{f"{NESTED}/tach.toml": "source_roots = ['.']\n"})
    assert not any("tach.toml" in note for note in _notes(root, NESTED))


def test_the_root_file_is_still_compared_for_drift(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    write_generated(mini_policy(output_dir=NESTED), root, services())
    path = root / "tach.toml"
    path.write_text(path.read_text(encoding="utf-8") + "# edited\n", encoding="utf-8")

    assert any(note.startswith("tach.toml:") for note in _notes(root, NESTED))


@pytest.mark.skipif(shutil.which("tach") is None, reason="tach is not installed")
def test_real_tach_rejects_a_forbidden_import_with_the_generated_file(tmp_path: Path) -> None:
    root = _repo(tmp_path, **{"app/domain.py": "import app.adapters\n"})
    write_generated(mini_policy(output_dir=NESTED), root, services())

    result = subprocess.run(["tach", "check", "--dependencies"], cwd=root, capture_output=True, text=True, check=False)
    assert result.returncode != 0 and "app.adapters" in result.stdout + result.stderr
