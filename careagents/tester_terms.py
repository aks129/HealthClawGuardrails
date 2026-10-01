"""Tester terms for real-record testers (beta spec section 4.3).

The text waits on owner approval (#565). Until then the terms file carries
PENDING_MARKER, TERMS_VERSION is None, the consent card shows no terms, the
consent version stays at BASE_VERSION, and database invites are not honoured
(careagents/app.py, `_real_records_open`).

To approve: replace the whole of templates/_tester_terms.html with the
approved text (the marker goes with it), set TERMS_VERSION to the approval
date, and ship. tests/test_careagents_beta_terms.py fails if only one of the
two changes is made. Every tester then accepts the new wording once.
The approved file must keep `<section class="consent-terms"
id="{{ terms_id }}">` as its outer element: both consent cards include it,
each with its own id, and the card tests look for those ids.
"""

from __future__ import annotations

# The consent version before tester terms existed. Bump only through
# TERMS_VERSION below. Stored per connection so we always know which version
# a person agreed to: a later change never silently claims earlier consent.
# 2026-08-01: the "leaving" clause said to email support, while self-serve
# Disconnect and Delete sat on the same page (#203). Understating our own
# strongest privacy control in the one place people read carefully.
BASE_VERSION = "2026-08-01"

#: Set to the approval date ("YYYY-MM-DD") when #565 is approved.
TERMS_VERSION: str | None = None

TEMPLATE = "_tester_terms.html"
PENDING_MARKER = "TESTER-TERMS-PENDING-565"

#: The one line the "Our terms changed" card says about what changed.
#: Replace it together with the terms file when #565 is approved; the terms
#: test fails if the terms are approved and this line is still the pending
#: one. True in both states: no connection made before consent was recorded
#: has accepted the current wording.
PENDING_CHANGE_SUMMARY = ("We now keep a record of which version of our "
                          "terms you accepted, so we're asking you to accept "
                          "the current ones.")
CHANGE_SUMMARY: str = PENDING_CHANGE_SUMMARY

#: What a connection's consent_version must equal to be current. Read as
#: `tester_terms.CONSENT_VERSION` at call time, never copied at import.
CONSENT_VERSION: str = TERMS_VERSION or BASE_VERSION


def approved() -> bool:
    return TERMS_VERSION is not None
