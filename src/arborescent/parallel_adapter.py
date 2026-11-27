"""How a run gets through its work faster: ``ProcessPoolParallel`` runs independent jobs across OS processes, the only way
pure-Python CPU-bound work uses more than one core (the GIL rules threads out, and the application ring forbids the process
effect outright); ``raised_gc_threshold`` makes the garbage collector run less often while a run is in progress."""

from __future__ import annotations

import gc
import os
from collections.abc import Callable, Generator, Sequence
from concurrent.futures import ProcessPoolExecutor
from contextlib import contextmanager
from typing import TypeVar

R = TypeVar("R")

__all__ = ["GC_THRESHOLD", "ProcessPoolParallel", "raised_gc_threshold"]

# Allocations between young-generation collections during a run (CPython's default is 700, or 2000 from 3.13). Parsing
# allocates millions of objects that nearly all live to the end of the run, so collecting them early is wasted work.
GC_THRESHOLD = 50_000


@contextmanager
def raised_gc_threshold() -> Generator[None]:
    """Raise the process-wide collector threshold to ``GC_THRESHOLD`` and put the previous one back on exit.

    Nothing changes when the collector is disabled, its threshold is 0 or already at least ``GC_THRESHOLD``. There is
    no shared state or lock: only an entry that saw a lower threshold restores, so nested and overlapping threads can
    only ever restore the caller's own value (a run that outlives the one that raised it finishes at the caller's value). A
    threshold somebody else set during the run is left alone."""
    previous = gc.get_threshold()
    if not gc.isenabled() or not 0 < previous[0] < GC_THRESHOLD:
        yield
        return
    gc.set_threshold(GC_THRESHOLD, *previous[1:])
    raised = gc.get_threshold()
    try:
        yield
    finally:
        if gc.get_threshold() == raised:
            gc.set_threshold(*previous)


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
