"""Guards on the deployed-surface inventory tool itself.

`scripts/surface_inventory.py` exists because #624 (an abandoned deployment
nobody watched) was found by accident. A tool that answers "what else is out
there?" is only worth having if its own claims hold, so these pin the three
that a reader would otherwise have to take on trust:

  * the reference scan really scans (it silently scanned nothing, once);
  * the retired VPS address never reaches an output stream;
  * the watched set comes from what the monitor requests, not from its
    constants.

Every test here runs offline. None of them makes a network request.
"""

from __future__ import annotations

import io
import contextlib
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

surface_inventory = pytest.importorskip(
    "surface_inventory", reason="requires requests")


def test_reference_scan_finds_a_host_it_should_find():
    """The scan returns real file:line hits, not an empty list.

    This failed silently and completely: `_SKIP_DIRS` was compared against the
    ABSOLUTE path parts, so a checkout living under any skipped name (a
    `.claude/worktrees/...` worktree, a `build/` directory) matched on every
    file and the scanner returned nothing for every host. The report still
    rendered, with the evidence column simply blank, which is the failure mode
    worth a test: a tool that reports nothing looks the same as a tool that
    found nothing.
    """
    hits = surface_inventory.references("careagents.cloud")

    assert hits, (
        "the reference scan found no file naming careagents.cloud, which "
        "cannot be true — careagents/config.py and README.md both do. The "
        "scanner is skipping everything."
    )
    for hit in hits:
        path, _, line = hit.rpartition(":")
        assert (REPO_ROOT / path).is_file(), f"{hit} is not a real file"
        assert line.isdigit(), f"{hit} has no line number"


def test_reference_scan_reports_paths_relative_to_the_repo():
    """A hit must be quotable into a document as-is.

    An absolute path leaks the operator's home directory, which this repo has
    published into a public evidence pack before.
    """
    for hit in surface_inventory.references("careagents.cloud"):
        assert not hit.startswith("/"), f"absolute path in a report: {hit}"
        assert "Users" not in hit, f"home directory in a report: {hit}"


def test_the_retired_vps_address_is_scrubbed():
    """The one string this tool must never print.

    `deploy/careagents/deploy.sh` addresses the retired box by IP. The report
    says whether a name resolves to it; it must not restate it.
    """
    address = surface_inventory.vps_address()
    if address is None:
        pytest.skip("the deploy script no longer carries a literal address")

    scrubbed = surface_inventory._scrub(f"connecting to {address}/healthz")

    assert address not in scrubbed
    assert surface_inventory.VPS_PLACEHOLDER in scrubbed


def test_scrub_survives_empty_and_missing_input():
    assert surface_inventory._scrub("") == ""
    assert surface_inventory._scrub(None) is None


def test_watched_set_is_observed_rather_than_read():
    """The watched URLs come from prod_watch's requests, not its constants.

    The distinction is the whole point. `prod_watch.CAREAGENTS` is named for a
    product and set to a platform hostname, so a set built by reading names
    would claim the consumer domain is watched when no check ever requests it.
    """
    urls = surface_inventory.watched_urls()

    assert urls, "prod_watch made no requests at all"
    hosts = {surface_inventory.host_of(u) for u in urls}
    assert "careagents-production.up.railway.app" in hosts, (
        "the observed set lost the CareAgents platform host")
    assert "careagents.cloud" not in hosts, (
        "prod_watch does not request careagents.cloud; if it now does, the "
        "inventory's finding 3 is fixed and this test should say so")


def test_the_stub_run_prints_nothing():
    """Deriving the watched set must not spray prod_watch's report at a reader.

    The stub gives every check a non-response, so an unguarded call prints
    eleven FAIL lines at the top of the inventory. They are an artefact of
    stubbing the network, and a reader reasonably reads them as the inventory
    failing.
    """
    captured_out, captured_err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(captured_out), \
            contextlib.redirect_stderr(captured_err):
        surface_inventory.watched_urls()

    assert captured_out.getvalue() == ""
    assert captured_err.getvalue() == ""


def test_every_surface_is_probed_over_https_with_a_reason():
    """No plaintext probe, and no entry without a stated reason for being here.

    The exclusion argument in the evidence document only holds if every
    inclusion is justified in the table itself.
    """
    for surface in surface_inventory.SURFACES:
        assert surface["probe"].startswith("https://"), surface
        assert surface["why"].strip(), surface
        assert surface["group"] in (surface_inventory.OURS,
                                    surface_inventory.UPSTREAM), surface


def test_a_platform_not_found_page_is_not_a_live_surface():
    """Railway and Vercel answer for names that host nothing.

    Counting those as live would hide exactly the stale reference this tool
    looks for: the host answers, the deployment does not exist.
    """
    not_found = {"reached": True, "status": 404, "body":
                 '{"status":"error","code":404,"message":"Application not found"}'}
    real_404 = {"reached": True, "status": 404, "body": '{"detail":"no route"}'}
    unreachable = {"reached": False, "status": None, "body": ""}

    assert surface_inventory.answering(not_found) is False
    assert surface_inventory.answering(real_404) is True
    assert surface_inventory.answering(unreachable) is False
