"""The validator names what it examined, and does not say "passed" (#460).

Found by Dr. Magan on 2026-08-10, running the prompt sequence for the launch
video: the validator accepted an Observation with no effective[x], category,
performer or subject, and reported

    "Structural validation passed (R4/R6, external validator unavailable)"

_validate_observation checks exactly two fields. The parenthetical was true
and did no work — it hung off a clause that had already asserted the resource
passed, so it read as a footnote about thoroughness rather than "almost
nothing was examined".

This is the error_fidelity property the conformance grade claims, failing
inside the validator: a check that examined two fields printing the word a
full profile validation would print. docs/defect-catalogue.md §1.

ASSERTED ON EMITTED OUTPUT, NOT ON SOURCE TEXT. r6/validator.py quotes the old
sentence in a comment in order to explain it, and a source-level ban would
match that comment — the §4 trap this repo keeps stepping in. What must never
come back is the CLAIM reaching a caller, so that is what is checked.
"""

import pytest

from r6.validator import R6Validator, _checked_expressions, _coverage_note


@pytest.fixture
def validator():
    return R6Validator()


def _info(result):
    """The informational diagnostics from a clean validation."""
    return ' '.join(
        i.get('diagnostics', '')
        for i in result['operation_outcome']['issue']
        if i.get('severity') == 'information')


class TestTheClaimIsGone:

    def test_a_thin_observation_is_not_told_validation_passed(self, validator):
        """MUTATION: restore the old diagnostics string -> red.

        This is Dr. Magan's exact resource: status and code, nothing else. It
        is still accepted — that is a separate decision (#460 item 3) — but
        the caller is no longer told it passed validation.
        """
        result = validator.validate_resource({
            'resourceType': 'Observation',
            'status': 'final',
            'code': {'coding': [{'system': 'http://loinc.org', 'code': '2339-0'}]},
        })
        assert result['valid'] is True
        text = _info(result)
        assert 'validation passed' not in text.lower(), (
            'the caller is still told validation passed after two required-'
            'field checks')

    def test_it_names_the_fields_it_checked(self, validator):
        result = validator.validate_resource({
            'resourceType': 'Observation',
            'status': 'final',
            'code': {'coding': [{'code': 'x'}]},
        })
        text = _info(result)
        assert 'Observation.status' in text
        assert 'Observation.code' in text

    def test_it_names_what_it_did_not_check(self, validator):
        """The half a reader needs to decide whether this is enough."""
        result = validator.validate_resource({
            'resourceType': 'Observation',
            'status': 'final',
            'code': {'coding': [{'code': 'x'}]},
        })
        text = _info(result).lower()
        for missing in ('profile conformance', 'terminology binding',
                        'cardinality', 'structuredefinition'):
            assert missing in text, f'{missing!r} is not disclosed'


class TestTheListIsDerivedNotTyped:

    def test_expressions_come_from_the_validator_itself(self):
        """MUTATION: add a check for Observation.subject to
        _validate_observation -> this list grows without anyone editing it.

        A hand-kept list of "what we check" is a second source of truth, and
        it drifts in the direction of claiming coverage that is not there.
        """
        assert _checked_expressions('Observation') == (
            'Observation.code', 'Observation.status')
        # A type with more checks, to prove the extraction is not hardcoded.
        condition = _checked_expressions('Condition')
        assert 'Condition.subject' in condition
        assert len(condition) > 2

    def test_a_type_with_no_validator_says_so(self):
        """Silence about an unvalidated type is the original defect in
        miniature: nothing was checked, and the old message would still have
        said validation passed."""
        assert _checked_expressions('Basic') == ()
        note = _coverage_note('Basic').lower()
        assert 'no basic-specific checks' in note
        assert 'passed' not in note

    def test_every_dispatched_type_produces_a_usable_note(self):
        """Whatever the type, the note names something and discloses the
        gap — no resource type gets a blank reassurance."""
        from r6.validator import R6_RESOURCE_TYPES
        for rtype in R6_RESOURCE_TYPES:
            note = _coverage_note(rtype)
            assert 'NOT checked' in note, rtype
            assert 'validation passed' not in note.lower(), rtype
