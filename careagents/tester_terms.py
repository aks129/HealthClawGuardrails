"""Tester terms for real-record testers (beta spec section 4.3).

The terms in templates/_tester_terms.html were approved on TERMS_VERSION
(#565). Database invites are honoured only while approved() is true
(careagents/app.py, `_real_records_open`), and every real connection must
carry consent at CONSENT_VERSION before its assistant answers.

To change the terms: edit templates/_tester_terms.html, set TERMS_VERSION
to the new approval date and CHANGE_SUMMARY to one line saying what
changed, and ship. Every tester then accepts the new wording once.
The file must keep `<section class="consent-terms" id="{{ terms_id }}">`
as its outer element: both consent cards include it, each with its own
id, and the card tests look for those ids.
tests/test_careagents_beta_terms.py pins the file, the version and the
change line together.
"""

from __future__ import annotations

# The consent version before tester terms existed. Bump only through
# TERMS_VERSION below. Stored per connection so we always know which version
# a person agreed to: a later change never silently claims earlier consent.
# 2026-08-01: the "leaving" clause said to email support, while self-serve
# Disconnect and Delete sat on the same page (#203). Understating our own
# strongest privacy control in the one place people read carefully.
BASE_VERSION = "2026-08-01"

#: The approval date ("YYYY-MM-DD") of the terms in TEMPLATE (#565).
TERMS_VERSION: str | None = "2026-10-09"

TEMPLATE = "_tester_terms.html"
PENDING_MARKER = "TESTER-TERMS-PENDING-565"

#: The line the "Our terms changed" card showed before #565 was approved.
#: Kept so the terms test can check CHANGE_SUMMARY moved on from it.
PENDING_CHANGE_SUMMARY = ("We now keep a record of which version of our "
                          "terms you accepted, so we're asking you to accept "
                          "the current ones.")
#: The one line the "Our terms changed" card says about what changed.
#: Update it with every new TERMS_VERSION.
CHANGE_SUMMARY: str = ("We added tester terms for people who connect their "
                       "own records. Please read them and accept.")

#: What a connection's consent_version must equal to be current. Read as
#: `tester_terms.CONSENT_VERSION` at call time, never copied at import.
CONSENT_VERSION: str = TERMS_VERSION or BASE_VERSION


def approved() -> bool:
    return TERMS_VERSION is not None
