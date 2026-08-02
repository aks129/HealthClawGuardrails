"""Build provenance — which commit is this process actually running?

Both CareAgents deployments were once found serving code months older than
`main` while every production check reported green (#258). Nothing the monitor
asked about could tell a current build from a stale one, because nothing the
running process exposed said which build it was.

`careagents/BUILD_SHA` answers that. It is written at deploy time
(`deploy/careagents/deploy.sh`, or by hand before `railway up`) and travels
with the tree, so it cannot drift from the code the way a service variable
can. It is generated, never committed — a checked-in marker would go stale
silently, which is the exact failure this exists to catch.

Two lines:

    <short sha, 12 hex, optionally -dirty>
    <unix timestamp of that commit>

Telemetry, never a gate. Nothing branches on these values: a missing,
unreadable, or malformed marker degrades to ``("unknown", 0)`` and the app
boots and serves exactly as it did before.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

_MARKER = Path(__file__).with_name("BUILD_SHA")

# A deploy stamp, not free text: this value is echoed on a public endpoint, so
# anything that is not recognisably a commit is reported as "unknown" rather
# than repeated back to the world.
_SHA = re.compile(r"\A[0-9a-f]{7,40}(-dirty)?\Z")


def _read(marker: Path = _MARKER) -> tuple[str, int]:
    """Return (sha, unix commit time). Never raises.

    Falls back to the ``CARE_BUILD_SHA`` environment variable when the file is
    absent or unusable, and to ``("unknown", 0)`` when that is missing too.
    """
    try:
        lines = marker.read_text(encoding="utf-8").splitlines()
    except (OSError, ValueError):  # missing, unreadable, or not text
        lines = []

    sha = lines[0].strip() if lines else ""
    stamp = lines[1].strip() if len(lines) > 1 else ""
    if not _SHA.match(sha):
        sha, stamp = os.environ.get("CARE_BUILD_SHA", "").strip(), ""
        if not _SHA.match(sha):
            return "unknown", 0

    try:
        return sha, int(stamp)
    except ValueError:  # a good sha with a broken timestamp is still useful
        return sha, 0


# Read once, at import: the marker cannot change under a running process.
BUILD_SHA, BUILD_TIME = _read()
