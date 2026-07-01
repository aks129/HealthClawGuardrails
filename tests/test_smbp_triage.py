from r6.smbp.triage import classify, SYMPTOMS


# --- BP axis (no symptoms), 2025 AHA/ACC bands against 130/80 ---

def test_at_goal_below_threshold():
    r = classify(128, 78)
    assert r["band"] == "at_goal"
    assert r["emergency"] is False
    assert r["action"] == "log_encourage"


def test_at_goal_upper_edge():
    # 129/79 is still below 130/80
    assert classify(129, 79)["band"] == "at_goal"


def test_stage1_lower_edge():
    # exactly 130/80 -> Stage 1 (systolic OR diastolic at threshold)
    assert classify(130, 70)["band"] == "stage1"
    assert classify(120, 80)["band"] == "stage1"


def test_stage1_band():
    r = classify(138, 88)
    assert r["band"] == "stage1"
    assert r["action"] == "log_flag"
    assert r["emergency"] is False


def test_stage2_starts_at_140_90():
    # Stage 2 begins at 140/90 (not 160/100)
    assert classify(140, 85)["band"] == "stage2"
    assert classify(120, 90)["band"] == "stage2"
    r = classify(164, 98)
    assert r["band"] == "stage2"
    assert r["action"] == "symptom_screen_then_visit"
    assert r["emergency"] is False


def test_stage2_upper_edge():
    assert classify(179, 119)["band"] == "stage2"


def test_crisis_urgency_no_symptoms():
    r = classify(184, 118)
    assert r["band"] == "crisis"
    assert r["action"] == "recheck_5min_then_careteam"
    assert r["emergency"] is False


def test_crisis_diastolic_alone_no_symptoms():
    # >=180/120 with no symptoms is crisis/urgency (recheck), not emergency
    assert classify(150, 122)["band"] == "crisis"
    assert classify(150, 122)["emergency"] is False


# --- Symptom axis: ANY red-flag symptom -> emergency, regardless of reading ---

def test_symptom_at_any_reading_is_emergency():
    # 2025 safety rule: a red-flag symptom is a stroke/heart-attack sign that
    # warrants 911 regardless of the BP number.
    for s in SYMPTOMS:
        r = classify(118, 76, symptoms=[s])  # normal BP + symptom
        assert r["band"] == "emergency", s
        assert r["emergency"] is True
        assert r["action"] == "call_911"


def test_symptom_at_high_reading_is_emergency():
    r = classify(190, 122, symptoms=["chest_pain"])
    assert r["band"] == "emergency"
    assert r["emergency"] is True


def test_symptoms_are_the_seven_red_flags():
    assert set(SYMPTOMS) == {
        "chest_pain", "shortness_of_breath", "one_sided_weakness",
        "trouble_speaking", "vision_change", "severe_headache", "confusion"}


def test_unknown_symptom_is_ignored():
    # a non-red-flag string does not trigger the emergency pathway
    assert classify(120, 80, symptoms=["dizzy"])["emergency"] is False


def test_threshold_reported_is_130_80():
    assert classify(120, 80)["threshold"] == {"systolic": 130, "diastolic": 80}
