"""Stop counting DocumentReferences the patient cannot open (#226).

MEDENT ingest brings in DocumentReferences and `COUNTED_TYPES` counted them,
so the patient was told "247 records synced" while the notes and discharge
summaries dominating that count could not be opened by anything:

  * `careagents/agent.py` `search_records` omits DocumentReference from its
    resource enum entirely, so the agent cannot ask for one.
  * Gate G2, measured against the live demo tenant on 2026-08-01, found the
    attachments come back EMPTY anyway — `r6/redaction.py::_redact_recursive`
    strips `data`, `url` and `title` from any object carrying a
    `contentType`, which is every attachment.

So the number counted rows nothing could read. Same shape as the retro's
recurring defect (docs/2026-08-02-retro.md): a measure of one thing
presented as a measure of another.

Council ruling D2 (clinical) picks **option 3** from #226 — the honest
stopgap. Documents stay OUT of the number until a tool can open them, and
where the number is shown it says so. This changes what the number CLAIMS,
never what is stored: ingest is untouched, the DocumentReferences are still
there, and the day a read path exists they can be counted again.

This file covers the client: the counted set and the two sums over it. The
patient-visible half — the connection poll that carries the clause — is in
tests/test_careagents.py, beside the other poll cases and the app fixtures
they need.
"""

from __future__ import annotations

import pytest

from careagents.healthclaw import HealthClawClient, HealthClawError


# --- the counted set ---------------------------------------------------------

def test_documentreference_is_not_in_the_counted_set():
    """MUTATION: put "DocumentReference" back in COUNTED_TYPES -> red.

    Pinned by name rather than by count: a set that merely has six entries
    again would pass a length check while re-counting the unreadable type.
    """
    assert "DocumentReference" not in HealthClawClient.COUNTED_TYPES, (
        "DocumentReference is back in the synced count while nothing can "
        "open one (#226)")
    # The clinically meaningful types the patient CAN ask about stay counted;
    # emptying the set would also satisfy the assertion above.
    for readable in ("Condition", "Observation", "MedicationRequest",
                     "AllergyIntolerance", "Immunization"):
        assert readable in HealthClawClient.COUNTED_TYPES, readable


class _Counts(HealthClawClient):
    """A client whose only faked seam is the count search itself.

    Everything above `search` — COUNTED_TYPES, the summing, the uncounted
    probe — is the real code under test.
    """

    def __init__(self, totals):
        super().__init__("http://127.0.0.1:9", "unused-secret", timeout=0.1)
        self.totals = totals
        self.asked = []

    def search(self, tenant, resource_type, params=None):
        self.asked.append(resource_type)
        return {"resourceType": "Bundle", "type": "searchset",
                "total": self.totals.get(resource_type, 0)}


# 5 Observations + 2 DocumentReferences — the issue's own shape.
_SYNCED = {"Observation": 5, "DocumentReference": 2}


def test_a_bundle_of_five_observations_and_two_documents_counts_five():
    """MUTATION: restore DocumentReference to COUNTED_TYPES -> red at 7.

    7 was the old answer, and 7 is what the patient was told they had.
    """
    hc = _Counts(_SYNCED)
    assert hc.record_count("t") == 5
    assert "DocumentReference" not in hc.asked, (
        "the counted sum still asks for DocumentReference")


def test_the_documents_are_still_reported_separately_not_forgotten():
    """The clause needs a source. MUTATION: return 0 unconditionally -> red.

    This is what makes the difference between "not counted" and "not
    mentioned" — the second would be a quieter version of the same
    dishonesty.
    """
    assert _Counts(_SYNCED).uncounted_record_count("t") == 2
    assert _Counts({"Observation": 5}).uncounted_record_count("t") == 0


def test_an_outage_during_the_uncounted_probe_is_not_reported_as_zero():
    """`record_count` raises rather than under-reporting (#403); the probe
    beside it must not quietly answer 0, which reads as "no documents".

    MUTATION: swallow the error and return 0 -> red.
    """
    class _Down(_Counts):
        def search(self, tenant, resource_type, params=None):
            raise HealthClawError("search failed (503)", 503)

    with pytest.raises(HealthClawError):
        _Down({}).uncounted_record_count("t")
