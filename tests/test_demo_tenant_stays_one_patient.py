"""Re-seeding the demo tenant must not create a second synthetic patient.

`railway.toml` runs `seed-demo --tenant-id desktop-demo` as a pre-deploy
command on EVERY deploy, and its own comment calls that seeding "idempotent".
It was not. Six of the seven built-in resources carry no fixed `id`, so each
one took a fresh UUID and was inserted again: one more Maria Rivera, one more
diabetes Condition, one more A1c, one more metformin order, per deploy.

By 2026-08-10 the production demo tenant held 19 Patients, 12 Conditions, 40
Observations and 11 MedicationRequests, against a seed set of one, one, three
and one. The physician advisor preparing the launch demo found it from the
outside — "/conditions shows about a dozen duplicate Type 2 diabetes mellitus
entries and the labs repeat too, so it seems the tenant may have been seeded
more than once" — which is exactly what had happened, about a dozen times.

The failure was silent because the only resource that DID carry a fixed id
collided on the primary key, and that collision was caught and logged as a
warning. The one resource that would have shouted was the one being handled
quietly.
"""

from __future__ import annotations

import pytest

from models import db
from r6.models import R6Resource
from r6.seed import _built_in_resources, seed_demo_data

_TENANT = "reseed-guard-tenant"


def _counts(tenant: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for row in R6Resource.query.filter_by(tenant_id=tenant).all():
        out[row.resource_type] = out.get(row.resource_type, 0) + 1
    return out


@pytest.fixture
def seeded(app):
    with app.app_context():
        R6Resource.query.filter_by(tenant_id=_TENANT).delete()
        db.session.commit()
        yield
        R6Resource.query.filter_by(tenant_id=_TENANT).delete()
        db.session.commit()


def test_the_seed_set_is_not_empty():
    """A re-seed guard that seeds nothing would pass every assertion below."""
    resources = _built_in_resources()
    assert len(resources) >= 6, f"only {len(resources)} built-in resources"
    assert any(r.get("resourceType") == "Patient" for r in resources)


def test_seeding_twice_leaves_one_patient(app, seeded):
    """MUTATION: drop the fixed ids from _built_in_resources -> red.

    This is the defect exactly: run the pre-deploy command twice, get two
    patients.
    """
    with app.app_context():
        seed_demo_data(tenant_id=_TENANT)
        first = _counts(_TENANT)
        seed_demo_data(tenant_id=_TENANT)
        second = _counts(_TENANT)

    assert first == second, (
        f"re-seeding changed the tenant.\n  after one seed:  {first}\n"
        f"  after two seeds: {second}\n"
        f"Every deploy runs this command, so a difference here is a duplicate "
        f"per deploy on the tenant the launch demo is recorded against.")
    assert second.get("Patient") == 1, (
        f"the demo tenant holds {second.get('Patient')} Patients; the demo "
        f"narrates one person")


def test_seeding_ten_times_still_leaves_one_patient(app, seeded):
    """Production reached 19 Patients, not 2. One repeat would not have caught it late."""
    with app.app_context():
        for _ in range(10):
            seed_demo_data(tenant_id=_TENANT)
        counts = _counts(_TENANT)

    assert counts.get("Patient") == 1, f"10 seeds produced {counts}"
    assert counts.get("Condition") == 1, (
        f"10 seeds produced {counts.get('Condition')} Conditions — this is the "
        f"'about a dozen duplicate Type 2 diabetes mellitus entries' the "
        f"advisor saw on camera")


def test_every_seeded_resource_carries_a_stable_id():
    """The mechanism, pinned so it cannot regress by omission.

    A resource without an `id` gets a generated UUID, which is what made
    re-seeding additive. Adding a new demo resource without an id would
    silently restore the bug for that resource alone.

    MUTATION: delete the `id` from any built-in resource -> red.
    """
    missing = [r.get("resourceType") for r in _built_in_resources()
               if not r.get("id")]
    assert not missing, (
        f"these seeded resources have no fixed id, so each deploy inserts "
        f"another copy: {missing}")


def test_reseeding_does_not_orphan_the_clinical_references(app, seeded):
    """The subject references must still point at the patient that exists.

    A dedupe that keeps the first Patient but re-points nothing would leave
    Conditions referencing a row that is no longer there, which reads on
    camera as an empty record rather than a duplicated one.

    MUTATION: give the Patient a stable id but leave __PATIENT_ID__
    unresolved -> red.
    """
    import json

    with app.app_context():
        seed_demo_data(tenant_id=_TENANT)
        seed_demo_data(tenant_id=_TENANT)

        patients = R6Resource.query.filter_by(
            tenant_id=_TENANT, resource_type="Patient").all()
        assert len(patients) == 1
        pid = str(patients[0].id)

        dangling = []
        for row in R6Resource.query.filter_by(tenant_id=_TENANT).all():
            if row.resource_type == "Patient":
                continue
            blob = row.resource_json or ""
            if "__PATIENT_ID__" in blob:
                dangling.append(f"{row.resource_type}/{row.id}: placeholder unresolved")
                continue
            subject = (json.loads(blob).get("subject") or {}).get("reference")
            if subject and subject != f"Patient/{pid}":
                dangling.append(f"{row.resource_type}/{row.id}: subject {subject}")

    assert not dangling, "clinical resources point at a patient that is not there:\n  " + "\n  ".join(dangling)
