"""Clock adapter backed by the system's monotonic performance counter."""

from __future__ import annotations

import time


class SystemClock:
    """``Clock`` over ``time.perf_counter``."""

    def now(self) -> float:
        return time.perf_counter()
