"""The FileSystem contract, run against the real implementation and the in-memory fake, so the fake cannot drift."""

from __future__ import annotations

from pathlib import Path

import pytest
from fs_helpers import KINDS, make_fs, put

from arborescent.ports import FileSystem


@pytest.fixture(params=KINDS)
def env(request: pytest.FixtureRequest, tmp_path: Path) -> tuple[FileSystem, Path]:
    fs, root = make_fs(request.param, tmp_path)
    fs.mkdir(root)
    return fs, root


def test_text_round_trip_keeps_every_newline_exactly(env: tuple[FileSystem, Path]) -> None:
    fs, root = env
    fs.write_text(root / "a.txt", "one\ntwo\r\nthree\n")
    assert fs.read_bytes(root / "a.txt") == b"one\ntwo\r\nthree\n"
    assert fs.read_text(root / "a.txt") == "one\ntwo\r\nthree\n"


def test_empty_and_non_utf8_files(env: tuple[FileSystem, Path]) -> None:
    fs, root = env
    fs.write_bytes(root / "e", b"")
    assert fs.read_text(root / "e") == "" and fs.is_file(root / "e")
    fs.write_bytes(root / "bad", b"\xff\xfe")
    with pytest.raises(UnicodeDecodeError):
        fs.read_text(root / "bad")
    assert fs.read_bytes(root / "bad") == b"\xff\xfe"


def test_missing_paths_and_directories_where_files_are_expected(env: tuple[FileSystem, Path]) -> None:
    fs, root = env
    with pytest.raises(FileNotFoundError):
        fs.read_text(root / "nope")
    with pytest.raises(FileNotFoundError):
        fs.write_text(root / "no" / "dir" / "f", "x")  # parents are not created by a write
    with pytest.raises(FileNotFoundError):
        fs.remove_file(root / "nope")
    fs.mkdir(root / "d")
    with pytest.raises(OSError):  # IsADirectoryError / PermissionError, depending on the OS
        fs.read_bytes(root / "d")
    with pytest.raises(OSError):
        fs.write_text(root / "d", "x")
    assert not fs.exists(root / "nope") and not fs.is_file(root / "d") and fs.is_dir(root / "d") and fs.exists(root / "d")


def test_write_overwrites_and_remove_file(env: tuple[FileSystem, Path]) -> None:
    fs, root = env
    fs.write_text(root / "f", "old")
    fs.write_text(root / "f", "new")
    assert fs.read_text(root / "f") == "new"
    fs.remove_file(root / "f")
    assert not fs.exists(root / "f")


def test_mkdir_makes_parents_and_tolerates_existing(env: tuple[FileSystem, Path]) -> None:
    fs, root = env
    fs.mkdir(root / "a" / "b" / "c")
    fs.mkdir(root / "a" / "b" / "c")
    assert fs.is_dir(root / "a" / "b") and fs.is_dir(root / "a" / "b" / "c")


def test_glob_and_rglob_are_sorted_and_relative_to_the_root(env: tuple[FileSystem, Path]) -> None:
    fs, root = env
    put(fs, root, {"p/b/pyproject.toml": "", "p/a/pyproject.toml": "", "p/a/deep/pyproject.toml": "", "x.sh": "", "p/y.sh": "", "p/a/n.txt": ""})
    rel = lambda found: [q.relative_to(root).as_posix() for q in found]
    assert rel(fs.glob(root, "p/*/pyproject.toml")) == ["p/a/pyproject.toml", "p/b/pyproject.toml"]
    assert rel(fs.glob(root, "**/pyproject.toml")) == ["p/a/deep/pyproject.toml", "p/a/pyproject.toml", "p/b/pyproject.toml"]
    assert rel(fs.glob(root, "nothing/*")) == []
    assert rel(fs.rglob(root, "*.sh")) == ["p/y.sh", "x.sh"]
    everything = rel(fs.rglob(root, "*"))
    assert "p/a" in everything and "p/a/n.txt" in everything and everything == sorted(everything, key=lambda p: p.split("/"))


def test_remove_tree_and_missing_is_fine(env: tuple[FileSystem, Path]) -> None:
    fs, root = env
    put(fs, root, {"t/a/b.txt": "x", "t/c.txt": "y", "keep.txt": "z"})
    fs.remove_tree(root / "t")
    fs.remove_tree(root / "t")
    assert not fs.exists(root / "t") and not fs.exists(root / "t" / "a") and fs.is_file(root / "keep.txt")


def test_copy_tree_copies_bytes_and_skips_ignored_names_with_their_contents(env: tuple[FileSystem, Path]) -> None:
    fs, root = env
    put(fs, root, {"s/a.py": b"A\r\n", "s/sub/b.py": "B", "s/.git/HEAD": "ref", "s/pkg/__pycache__/m.pyc": "c", "s/x.pyc": "d", "s/empty/.keep": ""})
    fs.mkdir(root / "s" / "emptydir")
    fs.copy_tree(root / "s", root / "d", ignore=[".git", "__pycache__", "*.pyc"])
    names = sorted(p.relative_to(root / "d").as_posix() for p in fs.rglob(root / "d", "*"))
    assert names == ["a.py", "empty", "empty/.keep", "emptydir", "pkg", "sub", "sub/b.py"]
    assert fs.read_bytes(root / "d" / "a.py") == b"A\r\n"
    fs.write_text(root / "d" / "a.py", "changed")
    assert fs.read_bytes(root / "s" / "a.py") == b"A\r\n"  # a copy, not a link
    with pytest.raises(FileExistsError):
        fs.copy_tree(root / "s", root / "d")
    with pytest.raises(FileNotFoundError):
        fs.copy_tree(root / "missing", root / "d2")


def test_temporary_directory_is_empty_usable_and_removed_even_after_an_error(env: tuple[FileSystem, Path]) -> None:
    fs, _ = env
    with fs.temporary_directory("arborescent-test-") as tmp:
        assert fs.is_dir(tmp) and tmp.name.startswith("arborescent-test-") and fs.rglob(tmp, "*") == []
        fs.write_text(tmp / "f", "x")
    assert not fs.exists(tmp)
    with pytest.raises(RuntimeError), fs.temporary_directory("arborescent-test-") as boom:
        fs.write_text(boom / "f", "x")
        raise RuntimeError
    assert not fs.exists(boom)


def test_application_and_composition_code_touches_files_only_through_the_port() -> None:
    """The ring's import list no longer allows shutil/tempfile, but ``Path.read_text()`` needs no import: scan the calls."""
    import ast

    from arborescent.adapters import LocalFileSystem
    from arborescent.config import load_policy

    io_methods = {
        "read_text", "read_bytes", "write_text", "write_bytes", "open", "mkdir", "unlink", "rmdir", "rename",
        "exists", "is_file", "is_dir", "iterdir", "glob", "rglob", "stat", "touch", "rmtree", "copytree",
    }  # fmt: skip  (not `replace`: str.replace; not `resolve`: path normalisation, no content is read or written)
    repo = Path(__file__).resolve().parents[1]
    policy, _ = load_policy(LocalFileSystem(), repo / "pyproject.toml")
    bad: list[str] = []
    for path in sorted((repo / "src" / "arborescent").glob("*.py")):
        module = "arborescent" if path.stem == "__init__" else f"arborescent.{path.stem}"
        if policy.ring_of(module) not in {"application", "composition"}:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in io_methods:
                owner = node.func.value
                via_port = (isinstance(owner, ast.Name) and owner.id == "fs") or (isinstance(owner, ast.Attribute) and owner.attr == "fs")
                if not via_port:
                    bad.append(f"{path.name}:{node.lineno} .{node.func.attr}() bypasses the FileSystem port")
    assert not bad, bad


def test_json_round_trip_and_errors(env: tuple[FileSystem, Path]) -> None:
    fs, root = env
    fs.write_json(root / "j.json", {"b": [1, 2], "a": "é"})
    raw = fs.read_bytes(root / "j.json")
    assert raw.startswith(b'{\n  "b": [') and raw.endswith(b"}\n") and b"\\u00e9" in raw  # indent 2, LF, ASCII-escaped
    assert fs.read_json(root / "j.json") == {"b": [1, 2], "a": "é"}
    fs.write_bytes(root / "empty.json", b"")
    with pytest.raises(ValueError):
        fs.read_json(root / "empty.json")
    with pytest.raises(FileNotFoundError):
        fs.read_json(root / "nope.json")


def test_clocks_only_move_forward() -> None:
    from arborescent.adapters import SystemClock
    from arborescent.testing import FakeClock

    for clock in (SystemClock(), FakeClock()):
        a, b = clock.now(), clock.now()
        assert b >= a
    fake = FakeClock(step=2.0)
    assert (fake.now(), fake.now()) == (2.0, 4.0)
