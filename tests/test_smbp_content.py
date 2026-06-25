from r6.smbp.content import msg, SYMPTOM_PROMPTS


def test_returns_spanish_and_english():
    assert "presión" in msg("reading_prompt", "es").lower()
    assert "blood pressure" in msg("reading_prompt", "en").lower()


def test_unknown_language_falls_back_to_english():
    assert msg("reading_prompt", "fr") == msg("reading_prompt", "en")


def test_readback_formats_values():
    out = msg("reading_readback", "en", systolic=142, diastolic=88, pulse=76)
    assert "142" in out and "88" in out and "76" in out


def test_symptom_prompts_cover_all_six():
    assert set(SYMPTOM_PROMPTS["en"].keys()) == {
        "chest_pain", "trouble_breathing", "vision_change",
        "one_sided_weakness", "trouble_speaking", "severe_headache"}
    assert set(SYMPTOM_PROMPTS["es"].keys()) == set(SYMPTOM_PROMPTS["en"].keys())


def test_unknown_key_raises():
    import pytest
    with pytest.raises(KeyError):
        msg("nonexistent_key", "en")
