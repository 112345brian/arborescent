"""``ProcessPoolParallel``: runs independent jobs across OS processes, the only way pure-Python CPU-bound work uses
more than one core (the GIL rules threads out, and the application ring forbids the process effect outright)."""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from concurrent.futures import ProcessPoolExecutor
from typing import TypeVar

R = TypeVar("R")

__all__ = ["ProcessPoolParallel"]


class ProcessPoolParallel:
    """Below ``min_jobs`` a pool's startup cost outweighs what it saves, so those run in this process instead."""

    def __init__(self, min_jobs: int = 8) -> None:
        self._min_jobs = min_jobs

    def map(self, fn: Callable[..., R], jobs: Sequence[tuple[object, ...]]) -> list[R]:
        if len(jobs) < self._min_jobs:
            return [fn(*args) for args in jobs]
        max_workers = min(os.cpu_count() or 1, len(jobs))
        with ProcessPoolExecutor(max_workers=max_workers) as pool:
            futures = [pool.submit(fn, *args) for args in jobs]
            return [future.result() for future in futures]
