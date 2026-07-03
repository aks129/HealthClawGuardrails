from scripts.seed_quality_demo import build_quality_cohort
from r6.smbp.monitoring import build_bp_observation  # noqa: F401  (namespace-package import check)
from r6.quality.measures import evaluate_population


def test_cohort_yields_believable_rate():
    c = build_quality_cohort()
    assert c["denominator"] == 10   # 11 patients - 1 pregnancy exclusion
    assert c["numerator"] == 7      # 7 controlled < 140/90
    assert c["exclusions"] == 1
    # feed the cohort through the real engine and confirm the rate
    bundle = []
    for g in c["groups"]:
        patient = {"resourceType": "Patient", "id": g["label"],
                   "birthDate": g["birth_date"]}
        conds = [{**cd, "subject": {"reference": f"Patient/{g['label']}"}}
                 for cd in g["conditions"]]
        obs = [{**g["observation"], "subject": {"reference": f"Patient/{g['label']}"}}]
        bundle.append({"patient": patient, "conditions": conds, "observations": obs})
    pop = evaluate_population(bundle, "2026-01-01", "2026-12-31")
    assert pop["denominator"] == 11   # gross (pre-exclusion) per HL7 convention
    assert pop["exclusions"] == 1     # 1 pregnancy exclusion
    assert pop["numerator"] == 7      # 7 controlled
    # scored rate = numerator / (denominator - exclusions) = 7/10
    assert pop["performance_rate"] == 0.7
