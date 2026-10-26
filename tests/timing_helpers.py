"""A linear-time check that a busy machine does not fail.

An absolute wall-clock limit (well under a second) failed half the time
with other processes on the CPU. What the check exists for is
catastrophic regex backtracking, which shows as growth, not as a slow
constant: input 16 times longer costs about 16 times as much when
linear, about 256 times when quadratic. So compare the two sizes, each
the best of a few runs of CPU time used by this process (time spent
waiting for the CPU does not count), and keep an absolute ceiling only
real blow-up crosses.
"""
from __future__ import annotations

import time
from typing import Callable

GROWTH = 16
#: Linear is GROWTH, quadratic GROWTH ** 2; three times linear separates them.
MAX_RATIO = 3 * GROWTH
CEILING_SECONDS = 2.0


def _best_cpu_seconds(fn: Callable[[str], object], text: str,
                      repeats: int) -> float:
    best = float("inf")
    for _ in range(repeats):
        start = time.process_time()
        fn(text)
        best = min(best, time.process_time() - start)
    return best


def assert_linear(fn: Callable[[str], object], unit: str,
                  chars: int = 128_000, repeats: int = 3) -> None:
    """`fn` on `unit` repeated to about `chars` characters costs about
    GROWTH times what it costs on 1/GROWTH of that."""
    count = max(1, chars // GROWTH // len(unit))
    small, large = unit * count, unit * (count * GROWTH)
    t_small = _best_cpu_seconds(fn, small, repeats)
    t_large = _best_cpu_seconds(fn, large, repeats)
    # A floor so a too-fast small run cannot divide by nothing.
    ratio = t_large / max(t_small, 1e-4)
    assert ratio < MAX_RATIO, (
        f"{unit!r}: {GROWTH}x the input took {ratio:.0f}x the time "
        f"({t_small * 1000:.1f} ms -> {t_large * 1000:.1f} ms)")
    assert t_large < CEILING_SECONDS, (
        f"{unit!r} x{count * GROWTH}: {t_large:.3f}s CPU")
