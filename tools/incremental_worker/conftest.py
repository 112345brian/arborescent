"""Share the built worker between the test modules."""

from __future__ import annotations

from tools.incremental_worker.test_support import worker_binary

__all__ = ["worker_binary"]
