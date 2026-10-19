"""The privacy policy cannot change without its date changing (#565).

A policy page that gains or loses a sentence while still saying "Last
updated: <old date>" tells a reader the terms have not moved. This pins
the body (everything but the date line) to the date it was last updated.

To change the policy: edit templates/privacy.html, set a new "Last
updated" date and revision note, then set PINNED_DATE and PINNED_BODY_SHA
below. Changing the body alone fails here; that is the point.

MUTATION: add a word anywhere in privacy.html without touching the date
-> test_body_matches_its_date fails with "the date did not change".
"""

import hashlib
import re
from pathlib import Path

PRIVACY = Path(__file__).resolve().parent.parent / "templates" / "privacy.html"
DATE_RE = re.compile(r"Last updated: ([A-Z][a-z]+ \d{1,2}, \d{4})")

PINNED_DATE = "October 19, 2026"
PINNED_BODY_SHA = "e59df7e53d7fa42c78206cc8aa271c72402b2adf4b2daef93e320d555dcc113b"


def _date_and_body_sha():
    text = PRIVACY.read_text(encoding="utf-8")
    dates = DATE_RE.findall(text)
    assert len(dates) == 1, "privacy.html must carry exactly one 'Last updated' date"
    body = DATE_RE.sub("Last updated: <date>", text)
    # Whitespace-insensitive, so re-indenting is not a policy change.
    body = " ".join(body.split())
    return dates[0], hashlib.sha256(body.encode("utf-8")).hexdigest()


def test_body_matches_its_date():
    date, sha = _date_and_body_sha()
    if sha == PINNED_BODY_SHA:
        assert date == PINNED_DATE, (
            "privacy.html's date changed but its body did not; update "
            "PINNED_DATE to match")
        return
    assert date != PINNED_DATE, (
        "privacy.html changed but its 'Last updated' date did not. Set a "
        "new date and revision note, then update PINNED_DATE and "
        f"PINNED_BODY_SHA (now {sha}) in this test.")
    raise AssertionError(
        f"privacy.html has a new date ({date}); update PINNED_DATE to it and "
        f"PINNED_BODY_SHA to {sha} in this test.")


def test_policy_page_shows_the_pinned_date(client):
    page = client.get("/privacy").get_data(as_text=True)
    assert f"Last updated: {PINNED_DATE}" in page
