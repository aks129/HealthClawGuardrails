"""The security allowlist of endpoints permitted to answer unredacted.

`r6.access._UNREDACTED_EXITS` is the only way a FHIR payload leaves this
server without a redaction profile having produced it. Adding a name is
therefore a deliberate TWO-file change: the frozenset in `r6/access.py`, and
the literal expected set below with the reason written out.

Splitting it across two files is the whole control. A one-file allowlist is a
line someone adds while fixing something else; a two-file one has to be
argued for in a review.
"""

from r6.access import _UNREDACTED_EXITS

#: endpoint name -> why answering unredacted is correct there.
EXPECTED_EXITS = {
    'r6.subscription_topics': (
        'SubscriptionTopic is server metadata describing subscribable events '
        '(r6/routes.py:1671). It carries no stored patient data, so a '
        'redaction profile would be a no-op that misleads the next reader.'
    ),
    'r6.audit_search': (
        'AuditEventRecord is PHI-free by construction — the audit `detail` '
        'line is a stated invariant, not a field redaction could rescue.'
    ),
    'r6.sdc_populate': (
        'Record content is redacted in _redacted_for_populate before the '
        'engine sees it, and the subject is bounded by the D10 %patient '
        'projection. A strip applied to the populated response cannot tell '
        'an answer taken from the record from one the caller typed, and '
        'Profile.INTAKE now deletes every answer (#282, owner ruling '
        '2026-09-24, replacing D10\'s Profile.INTAKE exit).'
    ),
}


def test_the_unredacted_allowlist_is_exactly_the_expected_set():
    """MUTATION: add any endpoint to _UNREDACTED_EXITS without adding it here
    -> red. That is the two-file change this test exists to force.

    r6/routes.py:719-723 (update_resource echoing the stored resource) is
    deliberately NOT here. It is S-11, a defect, and it migrates to
    fhir_response(profile=Profile.STANDARD) with its own PR and its own pin —
    allowlisting it would launder a bug into a policy.
    """
    assert _UNREDACTED_EXITS == frozenset(EXPECTED_EXITS)


def test_every_allowlisted_exit_states_a_reason():
    """An entry with no written reason is an omission wearing a policy's
    clothes. The reason is for the reviewer; it never reaches a client.
    """
    for endpoint, reason in EXPECTED_EXITS.items():
        assert reason.strip(), f'{endpoint} is allowlisted with no reason'
        assert len(reason.split()) >= 8, (
            f'{endpoint} needs a reason a reviewer can disagree with')


def test_the_allowlist_is_immutable():
    """A frozenset so no import-time hook can widen it."""
    assert isinstance(_UNREDACTED_EXITS, frozenset)


def test_every_unredacted_exit_call_site_is_counted():
    """The allowlist names endpoints; this counts the calls. A new
    unredacted_response call anywhere in r6/ is red here until it is argued
    for, even if it reuses an endpoint name already on the list.

    MUTATION: add an unredacted_response call to any r6 module -> red.
    """
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / 'r6'
    sites = []
    for path in sorted(root.rglob('*.py')):
        tree = ast.parse(path.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(
                    node.func, 'id', getattr(node.func, 'attr', None)
            ) == 'unredacted_response':
                sites.append(str(path.relative_to(root.parent)))
    assert sites == ['r6/sdc/routes.py'], sites
