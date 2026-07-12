from datetime import date

from scripts.seed_quality_demo import build_quality_cohort, _demo_observation_date
from r6.smbp.monitoring import build_bp_observation  # noqa: F401  (namespace-package import check)
from r6.quality.measures import evaluate_population


def test_demo_observation_date_stays_in_current_year():
    assert _demo_observation_date(date(2027, 1, 15)) == "2027-01-01"
    assert _demo_observation_date(date(2027, 7, 15)).startswith("2027-")


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
    current_year = date.today().year
    for item in bundle:
        assert item["observations"][0]["effectiveDateTime"].startswith(
            f"{current_year}-")
    pop = evaluate_population(bundle, f"{current_year}-01-01",
                              f"{current_year}-12-31")
    assert pop["denominator"] == 11   # gross (pre-exclusion) per HL7 convention
    assert pop["exclusions"] == 1     # 1 pregnancy exclusion
    assert pop["numerator"] == 7      # 7 controlled
    # scored rate = numerator / (denominator - exclusions) = 7/10
    assert pop["performance_rate"] == 0.7
