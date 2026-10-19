"""Every coded concept in the check-in test patient (#959) has our label.

The clinic received the penicillin allergy as the bare RxNorm code 7980,
while the patient saw a name: r6/terminology.py had no row for it, and the
check-in export strips the upstream display and puts back only our label.
A fixture we ship as the demo patient should read the same on both sides.

`meta` is not walked: its one coding, the HTEST marker, is labelled with
the check-in marker work (#975), not here. A v3-NullFlavor code is exempt:
null flavours get no label by ruling (#957 round 3, tests/
test_sec_957_round3.py), and the one here, a race extension's ASKU, is a
valueCoding the check-in profile names itself.
"""

import json
from pathlib import Path

import pytest

from r6 import terminology

NULL_FLAVOR = 'http://terminology.hl7.org/CodeSystem/v3-NullFlavor'
FIXTURE = (Path(__file__).resolve().parents[1]
           / 'careagents' / 'checkin' / 'us_core_test_patient.json')


def _codings(obj, path):
    if isinstance(obj, dict):
        if isinstance(obj.get('system'), str) and isinstance(
                obj.get('code'), str):
            yield path, obj['system'], obj['code']
        for key, value in obj.items():
            if key != 'meta':
                yield from _codings(value, f'{path}.{key}')
    elif isinstance(obj, list):
        for i, value in enumerate(obj):
            yield from _codings(value, f'{path}[{i}]')


def _fixture_codings():
    bundle = json.loads(FIXTURE.read_text(encoding='utf-8'))
    for entry in bundle['entry']:
        res = entry['resource']
        yield from _codings(res, res['resourceType'])


@pytest.fixture(autouse=True)
def _static_table_only(monkeypatch):
    """The static table answers, never the opt-in resolver."""
    from r6 import terminology_resolver
    monkeypatch.setattr(terminology_resolver, 'resolve', lambda *a: None)


def test_the_walk_sees_the_fixture():
    """A walk that finds nothing would pass forever."""
    found = {(s, c) for _, s, c in _fixture_codings()}
    assert len(found) >= 20
    assert ('http://www.nlm.nih.gov/research/umls/rxnorm', '7980') in found


def test_every_code_in_the_fixture_has_a_label():
    """MUTATION: delete the (RXNORM, "7980") row -> red."""
    missing = sorted({(path, s, c) for path, s, c in _fixture_codings()
                      if not terminology.lookup(s, c)
                      and s != NULL_FLAVOR})
    assert missing == [], missing


def test_the_penicillin_allergy_reads_as_a_name():
    assert terminology.lookup(
        'http://www.nlm.nih.gov/research/umls/rxnorm', '7980') == \
        'Penicillin'
