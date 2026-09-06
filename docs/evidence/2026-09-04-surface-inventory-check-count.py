#!/usr/bin/env python3
"""Reproduction for finding 5a of the 2026-09-04 deployed-surface inventory.

    uv run python docs/evidence/2026-09-04-surface-inventory-check-count.py

`scripts/prod_watch.py` closes a passing run with `all {len(results)} checks
passing`. `results` is appended to at run time, and the build check is appended
only when `--expect-sha` is given. So the same fully-healthy production reports
two different totals depending on how it was called.

This stubs both HTTP helpers with one healthy response and runs the monitor
twice, changing nothing but the expected-sha argument. It asserts nothing and
gates nothing; it prints the two counts so the claim in the evidence document
can be re-checked rather than trusted.

Not a test on purpose. A test asserting the current behaviour would pin the
defect as a specification, and a test asserting the fix would fail until the
fix lands. QA reports this one; the change belongs to whoever owns the monitor.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import prod_watch  # noqa: E402  (the path insert above must come first)


class HealthyEverything:
    """One response that satisfies every check the monitor makes.

    Grade A, an 8-digit code input, a sign-in link, a stamped build, and an
    empty bundle. The Patient check wants an exact set and gets an empty one,
    so it fails; that does not matter here, because the count is taken from
    `results`, which records passes and failures alike.
    """
    status_code = 200
    text = '<a href="/auth"></a> <input maxlength="8">'

    def json(self) -> dict:
        return {"grade": "A", "accounts": True, "build": "deadbeefcafe",
                "built_at": 1788143223, "result": {}, "entry": []}


def count(expect_sha: list[str]) -> int:
    prod_watch.results.clear()
    prod_watch.get = lambda url, timeout, **kw: HealthyEverything()
    prod_watch.post = lambda url, timeout, **kw: HealthyEverything()
    prod_watch.run(1.0, expect_sha)
    return len(prod_watch.results)


def main() -> int:
    informational = count([])
    asserted = count(["deadbeefcafe0000000000000000000000000000"])
    print()
    print("prod_watch.run(), same stubbed healthy deployment both times:")
    print(f"  no --expect-sha     -> 'all {informational} checks passing'")
    print(f"  --expect-sha given  -> 'all {asserted} checks passing'")
    print()
    print("The total is a property of the invocation, not of the surface.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
