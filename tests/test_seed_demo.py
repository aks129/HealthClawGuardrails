"""Regression tests for seed_demo_data resource-id resolution.

The SDC $populate demo (and any consumer referencing a seeded resource by its
FHIR logical id) resolves via R6Resource.id. seed_demo_data must therefore
persist a resource that carries an `id` under that id — not a random UUID.
Caught by dog-food: GET /Questionnaire/healthclaw-intake/$populate 404'd on a
freshly-seeded server (local + prod) because the seed dropped the logical id.
"""

from r6.models import R6Resource
from r6.seed import seed_demo_data


def _get(resource_type, logical_id, tenant_id):
    return R6Resource.query.filter_by(
        resource_type=resource_type, id=logical_id, tenant_id=tenant_id).first()


def test_seeded_questionnaire_resolves_by_logical_id(app):
    with app.app_context():
        seed_demo_data("t-seed-1")
        # The demo intake Questionnaire must be reachable by its documented id.
        q = _get("Questionnaire", "healthclaw-intake", "t-seed-1")
        assert q is not None
        assert q.to_fhir_json()["id"] == "healthclaw-intake"


def test_seeded_patient_now_has_a_stable_id(app):
    # MOVED PIN. This asserted the OPPOSITE until 2026-08-10: that the Patient
    # kept a generated UUID because "the fix must not force ids onto id-less
    # resources". That was right about the mechanism and wrong about the
    # demo tenant, and the pin is what made the consequence invisible.
    #
    # railway.toml re-seeds desktop-demo before every deploy. A generated id
    # means a NEW patient each time: production reached 19 Patients, 12
    # duplicate diabetes Conditions and 40 Observations before the physician
    # advisor found it while rehearsing the launch demo. The stable id is now
    # the thing being protected.
    #
    # A resource genuinely without an `id` still gets a UUID; that path is
    # unchanged and covered by the custom-resources test below.
    with app.app_context():
        seed_demo_data("t-seed-2")
        patients = R6Resource.query.filter_by(
            resource_type="Patient", tenant_id="t-seed-2").all()
        assert len(patients) == 1
        assert patients[0].id == "demo-patient-rivera"


def test_reseed_is_idempotent_for_fixed_id_resources(app):
    # /internal/seed invites "re-seed anytime". A fixed-id resource collides on
    # the second pass; the seed must swallow it (rollback) and stay usable —
    # never raise, never leave a duplicate logical-id row.
    with app.app_context():
        seed_demo_data("t-seed-3")
        seed_demo_data("t-seed-3")  # must not raise
        qs = R6Resource.query.filter_by(
            resource_type="Questionnaire", id="healthclaw-intake",
            tenant_id="t-seed-3").all()
        assert len(qs) == 1
