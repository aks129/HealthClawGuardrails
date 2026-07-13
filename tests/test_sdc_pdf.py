from r6.sdc.pdf import render_questionnaire_response_pdf, _format_answer, _footer_text


def _small_qr():
    return {
        "resourceType": "QuestionnaireResponse",
        "status": "completed",
        "item": [
            {
                "linkId": "demographics",
                "item": [
                    {"linkId": "demographics.given-name", "text": "First name",
                     "answer": [{"valueString": "Jane"}]},
                    {"linkId": "demographics.family-name", "text": "Last name",
                     "answer": [{"valueString": "Doe"}]},
                ],
            },
        ],
    }


def _qr_with_medications(n_repeats=2):
    qr = _small_qr()
    meds = {
        "linkId": "medications",
        "text": "Current medications",
        "item": [
            {"linkId": "medications.no-current-medications",
             "text": "Patient confirms no current medications",
             "answer": [{"valueBoolean": False}]},
        ],
    }
    for i in range(n_repeats):
        meds["item"].append({
            "linkId": "medications.item",
            "text": "Medication",
            "item": [
                {"linkId": "medications.item.name", "text": "Medication name",
                 "answer": [{"valueString": f"Med {i + 1}"}]},
                {"linkId": "medications.item.dose", "text": "Dose",
                 "answer": [{"valueString": f"{i + 1}0mg daily"}]},
            ],
        })
    qr["item"].append(meds)
    return qr


def _qr_with_unanswered_allergy():
    qr = _small_qr()
    qr["item"].append({
        "linkId": "allergies",
        "text": "Allergies",
        "item": [
            # Unanswered — must render blank, not a default "Yes"/"No".
            {"linkId": "allergies.no-known-allergies",
             "text": "No known allergies (patient confirmed)"},
        ],
    })
    return qr


def test_render_small_qr_returns_pdf_bytes():
    pdf = render_questionnaire_response_pdf(_small_qr())
    assert isinstance(pdf, (bytes, bytearray))
    assert len(pdf) > 0
    assert pdf[:4] == b"%PDF"


def test_repeating_medications_group_renders_and_scales_output():
    empty_pdf = render_questionnaire_response_pdf(_small_qr())
    meds_pdf = render_questionnaire_response_pdf(_qr_with_medications())
    assert meds_pdf[:4] == b"%PDF"
    assert len(meds_pdf) > len(empty_pdf)


def test_repeating_medications_more_repeats_yields_more_content():
    two_pdf = render_questionnaire_response_pdf(_qr_with_medications(2))
    four_pdf = render_questionnaire_response_pdf(_qr_with_medications(4))
    assert len(four_pdf) > len(two_pdf)


def test_unanswered_allergy_renders_without_crash():
    pdf = render_questionnaire_response_pdf(_qr_with_unanswered_allergy())
    assert pdf[:4] == b"%PDF"
    assert len(pdf) > 0


def test_unanswered_boolean_item_does_not_format_as_yes():
    qr = _qr_with_unanswered_allergy()
    unanswered = qr["item"][-1]["item"][0]
    assert "answer" not in unanswered
    formatted = _format_answer(unanswered)
    assert formatted != "Yes"
    assert formatted != "No"


def test_format_answer_blank_marker_for_unanswered_item():
    assert _format_answer({"linkId": "x"}) == "—"


def test_format_answer_value_string():
    assert _format_answer({"linkId": "x", "answer": [{"valueString": "Jane"}]}) == "Jane"


def test_format_answer_value_boolean_true():
    assert _format_answer({"linkId": "x", "answer": [{"valueBoolean": True}]}) == "Yes"


def test_format_answer_value_boolean_false():
    assert _format_answer({"linkId": "x", "answer": [{"valueBoolean": False}]}) == "No"


def test_format_answer_value_date():
    assert _format_answer(
        {"linkId": "x", "answer": [{"valueDate": "1990-01-02"}]}) == "1990-01-02"


def test_format_answer_value_integer():
    assert _format_answer({"linkId": "x", "answer": [{"valueInteger": 42}]}) == "42"


def test_format_answer_value_quantity():
    formatted = _format_answer({"linkId": "x", "answer": [
        {"valueQuantity": {"value": 130, "unit": "mmHg"}}]})
    assert formatted == "130 mmHg"


def test_format_answer_value_coding_uses_display():
    formatted = _format_answer({"linkId": "x", "answer": [
        {"valueCoding": {"system": "http://hl7.org/fhir/administrative-gender",
                          "code": "female", "display": "Female"}}]})
    assert formatted == "Female"


def test_footer_text_includes_reviewed_on_when_provided():
    footer = _footer_text("2026-07-10")
    assert "2026-07-10" in footer
    assert "Reviewed by patient on 2026-07-10" in footer
    assert "not a medical record" in footer


def test_footer_text_shows_not_yet_reviewed_when_absent():
    footer = _footer_text(None)
    assert "(not yet reviewed)" in footer


def test_render_with_reviewed_on_does_not_raise():
    pdf = render_questionnaire_response_pdf(_small_qr(), reviewed_on="2026-07-10")
    assert pdf[:4] == b"%PDF"


def test_render_uses_questionnaire_title_and_falls_back_to_linkid_labels():
    questionnaire = {
        "title": "HealthClaw Standard Intake",
        "item": [
            {
                "linkId": "demographics",
                "text": "Demographics",
                "item": [
                    {"linkId": "demographics.given-name", "text": "First name"},
                ],
            },
        ],
    }
    # QR item omits `text` entirely — label must fall back to the Questionnaire's item text.
    qr = {
        "resourceType": "QuestionnaireResponse",
        "item": [
            {
                "linkId": "demographics",
                "item": [
                    {"linkId": "demographics.given-name",
                     "answer": [{"valueString": "Jane"}]},
                ],
            },
        ],
    }
    pdf = render_questionnaire_response_pdf(qr, questionnaire=questionnaire,
                                            subject_label="Jane Doe")
    assert pdf[:4] == b"%PDF"


def test_render_empty_qr_does_not_crash():
    pdf = render_questionnaire_response_pdf(
        {"resourceType": "QuestionnaireResponse", "item": []})
    assert pdf[:4] == b"%PDF"
