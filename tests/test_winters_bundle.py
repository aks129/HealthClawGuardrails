"""Winters hypertension demo bundle — structural validation."""
import json
from pathlib import Path

BUNDLE = json.loads(
    (Path(__file__).parent.parent / 'scripts' / 'demo_bundles' /
     'winters_hypertension.json').read_text())


def test_bundle_is_transaction():
    assert BUNDLE['resourceType'] == 'Bundle'
    assert BUNDLE['type'] == 'transaction'


def test_all_resource_types_supported():
    from r6.models import R6Resource
    for entry in BUNDLE['entry']:
        rt = entry['resource']['resourceType']
        assert R6Resource.is_supported_type(rt), rt


def test_rosa_has_escalation_reading():
    systolics = []
    for entry in BUNDLE['entry']:
        r = entry['resource']
        if (r['resourceType'] == 'Observation'
                and r['subject']['reference'] == 'Patient/rosa-delgado'):
            for comp in r.get('component', []):
                if comp['code']['coding'][0]['code'] == '8480-6':
                    systolics.append(comp['valueQuantity']['value'])
    assert max(systolics) >= 160  # the demo's escalation trigger


def test_marcus_has_no_htn_condition():
    marcus_conditions = []
    for entry in BUNDLE['entry']:
        r = entry['resource']
        if (r['resourceType'] == 'Condition'
                and r['subject']['reference'] == 'Patient/marcus-webb'):
            marcus_conditions.append(r)
            codes = [c['code'] for c in r['code']['coding']]
            assert 'I10' not in codes
    # Marcus must have at least one Condition (diabetes) — prevents vacuous pass
    assert len(marcus_conditions) >= 1


def test_rosa_has_i10_condition():
    rosa_i10_conditions = []
    for entry in BUNDLE['entry']:
        r = entry['resource']
        if (r['resourceType'] == 'Condition'
                and r['subject']['reference'] == 'Patient/rosa-delgado'):
            codes = [c['code'] for c in r['code']['coding']]
            if 'I10' in codes:
                rosa_i10_conditions.append(r)
    assert len(rosa_i10_conditions) >= 1, "Rosa must have an I10 hypertension Condition"


def test_seed_db_mode_ingests(app):
    # seed_demo_data takes resources: list[dict], not a bundle directly.
    # Extract the resource from each bundle entry.
    resources = [entry['resource'] for entry in BUNDLE['entry']]

    from r6.seed import seed_demo_data
    with app.app_context():
        count = seed_demo_data(tenant_id='winters-demo', resources=resources)
        assert count == 15  # 2 patients + 2 conditions + 1 med + 6 obs + 3 orgs + 1 practitioner = 15
