from scripts.seed_winters_demo import build_demo_dataset


def test_demo_dataset_has_both_patients_and_readings():
    data = build_demo_dataset()
    labels = {p["label"] for p in data["patients"]}
    assert labels == {"Marisol", "Mr. Ray"}

    marisol = next(p for p in data["patients"] if p["label"] == "Marisol")
    assert marisol["language"] == "es"
    assert len(marisol["readings"]) >= 24
    from r6.smbp.monitoring import averages, build_bp_observation
    obs = [build_bp_observation("Patient/marisol", s, d, w)
           for (s, d, w) in marisol["readings"]]
    avg = averages(obs)
    assert 134 <= avg["overall"]["systolic"] <= 145

    ray = next(p for p in data["patients"] if p["label"] == "Mr. Ray")
    assert ray["language"] == "en"
    assert any(s >= 160 for (s, d, w) in ray["readings"])
