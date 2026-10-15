"""The consent body a real-record connect sends. Not a test module."""

from __future__ import annotations


def consented(**extra) -> dict:
    """`consent: true` plus the version on the card the person was shown.

    Read at call time, never copied at import: tests that act as if the
    terms changed (careagents_stage1_helpers.approve_terms) move
    CONSENT_VERSION, and an old copy would be refused with a 428.
    """
    from careagents import tester_terms
    return {"consent": True,
            "consent_version": tester_terms.CONSENT_VERSION, **extra}
