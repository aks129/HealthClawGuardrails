from r6.smbp.outreach import reminder_action, voice_reading_script


def test_reminder_action_is_valid_sms_payload():
    act = reminder_action(patient_ref="Patient/p1", phone="+15551234567",
                          lang="es", completed=9, prescribed=28)
    assert act["kind"] == "sms"
    assert isinstance(act["payload"]["body"], str) and act["payload"]["body"]
    # The executor reads the number from `phone` — not `to`.
    assert act["payload"]["phone"] == "+15551234567"
    assert "presión" in act["payload"]["body"].lower()


def test_reminder_number_is_not_in_the_audit_safe_label():
    # `to` is the recipient LABEL surfaced by ProposedAction.summary() into
    # audit + notify_tenant. The phone number is PHI and must never land there.
    act = reminder_action("Patient/p1", "+15559998888", "en", 1, 28)
    assert act["payload"]["to"] != "+15559998888"
    assert "+15559998888" not in act["payload"]["to"]


def test_reminder_action_english():
    act = reminder_action("Patient/p1", "+1", "en", 1, 28)
    assert "blood pressure" in act["payload"]["body"].lower()


def test_voice_script_includes_readback_and_symptom_screen():
    script = voice_reading_script(lang="en")
    joined = " ".join(script["steps"]).lower()
    assert "read" in joined  # read-back step present
    assert script["keypad_fallback"] is True
    assert len(script["symptom_screen"]) == 7  # 2025 red-flag screen
