from r6.smbp.triage import classify, SYMPTOMS


def test_normal_below_threshold():
    r = classify(128, 80)
    assert r["band"] == "normal"
    assert r["emergency"] is False
    assert r["action"] == "log_encourage"


def test_elevated_flag_no_alarm():
    r = classify(150, 92)
    assert r["band"] == "elevated"
    assert r["action"] == "log_flag"
    assert r["emergency"] is False


def test_stage_followup_within_week():
    r = classify(168, 104)
    assert r["band"] == "followup"
    assert r["action"] == "symptom_screen_then_visit"
    assert r["emergency"] is False


def test_urgency_recheck_no_symptoms():
    r = classify(184, 118)
    assert r["band"] == "urgent"
    assert r["action"] == "recheck_5min_then_careteam"
    assert r["emergency"] is False


def test_emergency_high_with_symptom():
    r = classify(184, 118, symptoms=["chest_pain"])
    assert r["band"] == "emergency"
    assert r["emergency"] is True
    assert r["action"] == "call_911"


def test_emergency_diastolic_alone_triggers_high_band():
    # >= 180/120 with no symptoms is urgent (recheck), not emergency
    assert classify(150, 122)["band"] == "urgent"


def test_any_symptom_at_high_reading_is_emergency():
    for s in SYMPTOMS:
        assert classify(190, 100, symptoms=[s])["emergency"] is True


def test_symptom_at_normal_reading_not_emergency():
    # symptoms only escalate at >=180/120 per spec table
    assert classify(120, 80, symptoms=["severe_headache"])["emergency"] is False
