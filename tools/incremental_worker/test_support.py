"""The built worker, shared by the opt-in prototype's test modules."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def worker_binary(tmp_path_factory: pytest.TempPathFactory) -> Path:
    built = os.environ.get("ARBORESCENT_WORKER")
    if built is not None:  # CI names the binary it built, so nothing here may skip
        binary = ROOT / built
        if not binary.is_file():
            pytest.fail(f"ARBORESCENT_WORKER is not a built worker: {binary}")
        return binary
    cargo = shutil.which("cargo")
    if cargo is None:
        pytest.skip("the optional Rust prototype needs Cargo")
    target = tmp_path_factory.mktemp("incremental-worker")
    subprocess.run(
        [
            cargo,
            "build",
            "--locked",
            "--quiet",
            "--manifest-path",
            str(ROOT / "tools/incremental_worker/Cargo.toml"),
            "--target-dir",
            str(target),
        ],
        check=True,
    )
    suffix = ".exe" if sys.platform == "win32" else ""
    return target / "debug" / f"arborescent-incremental-worker{suffix}"
