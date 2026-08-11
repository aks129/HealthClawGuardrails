"""A step-up refusal tells the caller why (owner ruling, 2026-08-10).

require_grant used to answer every rejected token with 'Invalid step-up
token'. A caller holding an expired token, a read-scoped token, or a token
minted for a different operation got the same four words, and the real reason
went to the server log where they could not see it. Four of this project's
recorded defects are a control that refuses without saying what it refused;
this was one of them wearing an HTTP status.

The ruling is implemented as an allowlist with one documented carve-out. The
line is not sensitivity — it is ownership. A reason describing the caller's
own token tells them nothing they could not get by decoding it. 'Token tenant
mismatch' describes someone else's: it confirms the token is real and merely
issued elsewhere, which is what a caller probing with a token they should not
have wants to know.

MUTATION: add 'Token tenant mismatch' to _PUBLIC_REASONS ->
test_the_cross_tenant_reason_is_never_published reddens.
MUTATION: revert _public_reason to return _DENIED_REJECTED always ->
test_an_expired_token_says_it_expired and its siblings redden.
"""

import re
from pathlib import Path

import pytest

from r6 import access

ROOT = Path(__file__).resolve().parents[1]
STEPUP_SRC = ROOT / 'r6' / 'stepup.py'


def _validator_reasons():
    """Every refusal string r6/stepup.py can return.

    Read from the source rather than listed here, so this test cannot pass by
    describing a version of stepup.py that no longer exists.
    """
    text = STEPUP_SRC.read_text(encoding='utf-8')
    return set(re.findall(r"return False,\s*'([^']+)'", text))


class TestEveryReasonIsClassified:

    def test_no_reason_is_unclassified(self):
        """MUTATION: add `return False, 'Token issuer mismatch'` to
        r6/stepup.py without touching r6/access.py -> red.

        A reason in neither set is not a safe default in either direction. It
        would be withheld, silently, by a decision nobody made — which is how
        the original behaviour got there.
        """
        unclassified = (_validator_reasons()
                        - access._PUBLIC_REASONS
                        - set(access._WITHHELD_REASONS))
        assert not unclassified, (
            f'r6/stepup.py can refuse with {sorted(unclassified)}, which '
            'r6/access.py neither publishes nor documents as withheld. Add '
            'each to _PUBLIC_REASONS, or to _WITHHELD_REASONS with the '
            'reason it is withheld.')

    def test_the_lists_describe_reasons_that_exist(self):
        """The mirror image: a classified reason the validator cannot return
        is dead configuration, and dead configuration is read as coverage."""
        real = _validator_reasons()
        stale = ((access._PUBLIC_REASONS | set(access._WITHHELD_REASONS))
                 - real)
        assert not stale, (
            f'{sorted(stale)} are classified in r6/access.py but '
            'r6/stepup.py never returns them')

    def test_every_withheld_reason_records_why(self):
        for reason, why in access._WITHHELD_REASONS.items():
            assert why and len(why) > 30, (
                f'{reason!r} is withheld without a recorded reason')


class TestTheCallerIsTold:

    @pytest.mark.parametrize('error,expected', [
        ('Step-up token expired', 'Step-up token expired'),
        ('Token already used (replay)', 'Token already used (replay)'),
        ('Token audience mismatch', 'Token audience mismatch'),
        ('Token operation mismatch', 'Token operation mismatch'),
        ('Read-scoped token cannot authorize this operation',
         'Read-scoped token cannot authorize this operation'),
    ])
    def test_a_reason_about_the_callers_own_token_is_published(
            self, error, expected):
        assert access._public_reason(error) == expected

    def test_the_cross_tenant_reason_is_never_published(self):
        """The one carve-out. This is the oracle: it separates a valid token
        issued elsewhere from junk."""
        assert access._public_reason('Token tenant mismatch') == \
            access._DENIED_REJECTED

    def test_an_unknown_reason_defaults_to_silence(self):
        """Default-deny. A reason nobody classified must not leak on the day
        it is written."""
        assert access._public_reason('Token issuer is evil.example') == \
            access._DENIED_REJECTED
        assert access._public_reason('') == access._DENIED_REJECTED

    def test_the_absent_case_is_unchanged(self):
        """No token at all is not a validator refusal and has no reason to
        state beyond the one it already states."""
        assert access._DENIED_ABSENT == 'Step-up token required'
