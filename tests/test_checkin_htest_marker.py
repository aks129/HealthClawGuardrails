"""The `checkin` export keeps a record's own HL7 v3 ActReason HTEST marker.

`R6Resource.to_fhir_json` replaces `meta` wholesale, and the check-in
profile rebuilds it again, so a synthetic record used to reach a clinic with
nothing saying it was test data. Since the wallet stopped stamping its own
Bundle (#973), the record's marker is the only one there is.

Exactly one coding crosses: system
`http://terminology.hl7.org/CodeSystem/v3-ActReason`, code `HTEST`, matched
by both. Its `display` is ours (r6/terminology.py), never the upstream's. It
stays in the slot the record carried it in, `meta.tag` or `meta.security`;
every other tag is dropped as before. Each test names the mutation that
turns it red.
"""

import pytest

from r6 import terminology
from r6.access import _checkin_profile
from tests import checkin_fixtures as f

V3_ACT_REASON = 'http://terminology.hl7.org/CodeSystem/v3-ActReason'
HTEST = {'system': V3_ACT_REASON, 'code': 'HTEST'}
OUR_LABEL = 'test health data'
SPOOF = 'CANARY-Rivera-Marisol test data'

ALL = ['AllergyIntolerance', 'Condition', 'Coverage', 'Patient']


@pytest.fixture(autouse=True)
def _mint_secret(monkeypatch):
    monkeypatch.setenv('INTERNAL_TOKEN_MINT_SECRET', f.MINT_SECRET)


def _marked(res, *codings, slot='tag'):
    """`res` with `codings` added to its stored meta under `slot`."""
    meta = res.setdefault('meta', {})
    meta[slot] = list(codings)
    return res


def _marker(**over):
    coding = dict(HTEST, display=SPOOF)
    coding.update(over)
    return coding


def _markers(res, slot='tag'):
    return [c for c in (res.get('meta') or {}).get(slot) or []
            if c.get('system') == V3_ACT_REASON]


def _export_all(app, client, *records):
    f.seed(app, *records)
    resp = f.export(client, f.token(client, ALL), ALL)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return resp


def _marked_set(slot='tag', coding=None):
    """Every exportable type, plus the payor Organization, each marked."""
    coding = coding or _marker()
    return [_marked(r, dict(coding), slot=slot) for r in (
        f.patient(), f.condition(), f.nka_allergy(), f.coverage(),
        f.organization())]


def test_our_table_names_the_marker():
    """The label is ours, keyed by system and code."""
    assert terminology.lookup(V3_ACT_REASON, 'HTEST') == OUR_LABEL


def test_the_marker_survives_on_every_exported_type(app, client):
    """Organization included: it is rebuilt from scratch by
    _checkin_organization, and Coverage by _checkin_coverage.

    MUTATION: drop the _test_markers block in _checkin_profile -> red."""
    resp = _export_all(app, client, *_marked_set())
    out = f.resources(resp)
    assert {r['resourceType'] for r in out} == {
        'Patient', 'Condition', 'AllergyIntolerance', 'Coverage',
        'Organization'}
    for res in out:
        assert _markers(res) == [dict(HTEST, display=OUR_LABEL)], \
            res['resourceType']


def test_the_display_is_ours_not_the_upstreams(app, client):
    """A spoofed upstream display is replaced, never echoed.

    MUTATION: copy the stored coding instead of building it -> red."""
    resp = _export_all(app, client, *_marked_set())
    assert SPOOF not in resp.get_data(as_text=True)
    assert 'CANARY' not in resp.get_data(as_text=True)


def test_a_marker_without_a_display_gets_ours(app, client):
    resp = _export_all(app, client,
                       *_marked_set(coding=dict(HTEST)))
    for res in f.resources(resp):
        assert _markers(res) == [dict(HTEST, display=OUR_LABEL)]


def test_a_marker_on_security_stays_on_security(app, client):
    """MUTATION: write every marker to meta.tag -> red."""
    resp = _export_all(app, client, *_marked_set(slot='security'))
    for res in f.resources(resp):
        assert _markers(res, 'security') == [dict(HTEST, display=OUR_LABEL)]
        assert _markers(res, 'tag') == []


def test_an_unmarked_record_gains_no_marker(app, client):
    """A real record is never called test data.

    MUTATION: stamp the marker unconditionally -> red."""
    resp = _export_all(app, client, f.patient(), f.condition(),
                       f.coverage(), f.organization())
    assert V3_ACT_REASON not in resp.get_data(as_text=True)
    for res in f.resources(resp):
        assert 'security' not in res['meta']


@pytest.mark.parametrize('near_miss', [
    # Same code, another system.
    {'system': 'http://terminology.hl7.org/CodeSystem/v3-ObservationValue',
     'code': 'HTEST'},
    {'system': 'https://ehr.example/tags', 'code': 'HTEST',
     'display': 'test health data'},
    # The system spelt another way: matched exactly, never normalised.
    {'system': V3_ACT_REASON + '/', 'code': 'HTEST'},
    {'system': ' ' + V3_ACT_REASON, 'code': 'HTEST'},
    {'system': V3_ACT_REASON.replace('http:', 'https:'), 'code': 'HTEST'},
    # Same system, another code.
    {'system': V3_ACT_REASON, 'code': 'htest'},
    {'system': V3_ACT_REASON, 'code': 'HTEST '},
    {'system': V3_ACT_REASON, 'code': 'PATADMIN'},
    # Malformed.
    {'system': [V3_ACT_REASON], 'code': 'HTEST'},
    {'system': V3_ACT_REASON, 'code': ['HTEST']},
    {'code': 'HTEST', 'display': 'test health data'},
])
def test_a_near_miss_is_dropped(app, client, near_miss):
    """MUTATION: compare canonical_system(...) or code.upper() -> red."""
    resp = _export_all(app, client, *_marked_set(coding=near_miss))
    text = resp.get_data(as_text=True)
    assert 'HTEST' not in text
    assert 'htest' not in text
    assert 'PATADMIN' not in text


def test_other_tags_beside_the_marker_are_dropped(app, client):
    """MUTATION: keep the stored tag list and only relabel HTEST -> red."""
    other = {'system': 'https://ehr.example/tags', 'code': 'VIP',
             'display': f.CANARY_DISPLAY}
    lookalike = {'system': 'https://ehr.example/tags', 'code': 'HTEST'}
    records = [_marked(r, dict(other), lookalike, _marker(), slot=slot)
               for r, slot in ((f.patient(), 'tag'),
                               (f.condition(), 'security'),
                               (f.coverage(), 'tag'),
                               (f.organization(), 'security'))]
    resp = _export_all(app, client, *records)
    text = resp.get_data(as_text=True)
    assert 'VIP' not in text and 'ehr.example/tags' not in text
    assert f.CANARY_DISPLAY not in text
    for res in f.resources(resp):
        meta = res['meta']
        markers = [c for c in meta.get('tag', []) + meta.get('security', [])
                   if c.get('system') == V3_ACT_REASON]
        assert markers == [dict(HTEST, display=OUR_LABEL)]


def test_a_repeated_marker_goes_out_once(app, client):
    resp = _export_all(app, client, _marked(
        f.patient(), _marker(), _marker(display='x'), dict(HTEST)))
    pt, = f.resources(resp, 'Patient')
    assert _markers(pt) == [dict(HTEST, display=OUR_LABEL)]


def test_a_single_marker_written_as_an_object_is_kept(app, client):
    """`strip_meta` already accepts a lone tag dict; so does this."""
    pt = f.patient()
    pt['meta']['tag'] = _marker()
    resp = _export_all(app, client, pt)
    out, = f.resources(resp, 'Patient')
    assert _markers(out) == [dict(HTEST, display=OUR_LABEL)]


def test_the_marker_is_the_only_display_in_the_output(app, client):
    """Ruling B (no coding.display in check-in output) still holds for
    everything else; this one display is the allow-listed exception, and
    it is the code system's own display, so a validator agrees with it."""
    resp = _export_all(app, client, *_marked_set())

    def displays(obj):
        if isinstance(obj, dict):
            for key, value in obj.items():
                if key == 'display':
                    yield value
                yield from displays(value)
        elif isinstance(obj, list):
            for value in obj:
                yield from displays(value)
    assert set(displays(resp.get_json())) == {OUR_LABEL}


def test_the_marker_sits_beside_the_record_source_stamp(app, client):
    """Our record-source tag is unchanged; the marker is added to it."""
    from models import db
    from r6.models import R6Resource
    from r6.record_source import RECORD_SOURCE_SYSTEM
    f.seed(app, _marked(f.patient(), _marker()))
    with app.app_context():
        R6Resource.query.filter_by(tenant_id=f.TENANT,
                                   id=f.PT).one().source = 'sample'
        db.session.commit()
    resp = f.export(client, f.token(client, ['Patient']), ['Patient'])
    pt, = f.resources(resp, 'Patient')
    assert {(t['system'], t['code']) for t in pt['meta']['tag']} == {
        (RECORD_SOURCE_SYSTEM, 'sample'), (V3_ACT_REASON, 'HTEST')}


def test_the_profile_reads_the_marker_only_from_the_stored_meta():
    """Stateless: the caller hands over the meta as stored, because
    to_fhir_json has already replaced it on the resource itself."""
    cond = f.condition()
    out = _checkin_profile(cond, patient_id=f.PT,
                           stored_meta={'tag': [_marker()]})
    assert _markers(out) == [dict(HTEST, display=OUR_LABEL)]
    assert _markers(_checkin_profile(cond, patient_id=f.PT)) == []
    for bad in (None, 'HTEST', [HTEST], {'tag': 'HTEST'}, {'tag': [None]}):
        assert _markers(_checkin_profile(
            cond, patient_id=f.PT, stored_meta=bad)) == []


def test_the_shipped_test_patient_keeps_its_marker_end_to_end(app, client):
    """The #959 fixture, stored the way scripts/seed_demo_tenant.py stores
    it (r6.seed.seed_demo_data), then exported: every entry carries the
    marker. Proves the write path keeps meta.tag in the stored JSON, not
    only that the export reads it back."""
    import json
    from pathlib import Path

    from r6.seed import seed_demo_data
    fixture = (Path(__file__).resolve().parents[1] / 'careagents'
               / 'checkin' / 'us_core_test_patient.json')
    bundle = json.loads(fixture.read_text(encoding='utf-8'))
    with app.app_context():
        seed_demo_data(f.TENANT, resources=[
            e['resource'] for e in bundle['entry']])
    types = ['AllergyIntolerance', 'Condition', 'Coverage', 'Immunization',
             'MedicationRequest', 'Patient']
    resp = f.export(client, f.token(client, types), types,
                    patient_id='checkin-test-patient')
    assert resp.status_code == 200, resp.get_data(as_text=True)
    out = f.resources(resp)
    assert {r['resourceType'] for r in out} == set(types) | {'Organization'}
    for res in out:
        assert _markers(res) == [dict(HTEST, display=OUR_LABEL)], res['id']
