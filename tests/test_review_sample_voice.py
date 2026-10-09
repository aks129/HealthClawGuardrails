"""The intake review page on made-up records (patient-tester walk, 375px).

A tester on CareAgents' sample records who opened an intake review read
"About you" over the made-up person's name and address, and could approve
only by ticking "I have no known allergies" about someone who is not them.
The engine now words the page about "the sample person" when CareAgents asks
for the sample voice (r6/voice.py) with the internal secret, exactly as the
brief does (#908). The voice changes words only: which rows are listed, the
gate, and the attestation are the same, and the attestation is still an
explicit tick that is never pre-checked.

Its own file: the template tests use conftest's engine `app`, which
tests/test_careagents.py's fixture of the same name would shadow.
Synthetic data only.
"""

from __future__ import annotations

import re
from collections import Counter

import pytest
from flask import render_template

from r6 import voice as voices
from r6.models import AuditEventRecord
from tests.test_intake_attestation_gate import _committed_action

SECRET = "review-voice-test-secret"

#: Wording that may appear only on a sample review.
SAMPLE_ONLY = ("sample person", "made-up", "sample records",
               'id="sample-back-to-chat"', '#review-gate-msg.alert-secondary {',
               'aria-describedby')

ROWS = {
    "meds": [{"name": "Lisinopril", "dose": "10 mg"}],
    "allergies": [{"allergen": "Penicillin", "reaction": "Hives"}],
    "conditions": [{"name": "Hypertension"}],
    "demographics": [("Name", "Ada Example"), ("Date of birth", "1962")],
}


def _render(app, **ctx):
    base = {"action_id": "act-1", "demographics": [], "meds": [],
            "allergies": [], "conditions": [], "record_readable": True,
            "record_reason": ""}
    base.update(ctx)
    if ctx.get("voice") == voices.SAMPLE:
        # What review_form passes with the sample voice, and only then.
        base["sample_banner"] = voices.SAMPLE_BANNER
    with app.test_request_context():
        return render_template("action_review.html", **base)


def _nka_input(html):
    m = re.search(r'<input[^>]*\bid="nka"[^>]*>', html)
    assert m, "the page has no attestation box"
    return m.group(0)


def _nka_label(html):
    m = re.search(r'<label[^>]*for="nka"[^>]*>(.*?)</label>', html, re.S)
    return " ".join(m.group(1).split())


def _gate(html):
    m = re.search(r'id="review-gate-msg"[^>]*>(.*?)</div>', html, re.S)
    return " ".join(m.group(1).split())


# --- the template ------------------------------------------------------------

STATES = [
    pytest.param({}, id="nothing-listed"),
    pytest.param(ROWS, id="rows"),
    pytest.param({"record_readable": False,
                  "record_reason": "no patient found"}, id="unreadable"),
]


@pytest.mark.parametrize("ctx", STATES)
def test_a_sample_review_is_about_the_sample_person(app, ctx):
    html = _render(app, voice=voices.SAMPLE, **ctx)
    assert "About the sample person" in html
    assert _nka_label(html) == ("Tick to say the sample person has no known "
                                "allergies.")
    assert voices.SAMPLE_BANNER in html
    assert 'id="sample-back-to-chat"' in html
    # Nothing on it speaks to the tester as the person in the records.
    text = re.sub(r"<script.*?</script>|<style.*?</style>|<!--.*?-->", "",
                  html, flags=re.S)
    assert "About you" not in text
    assert "I have no known allergies" not in text
    assert "from your records" not in text
    assert "true for you" not in text


@pytest.mark.parametrize("ctx", STATES)
@pytest.mark.parametrize("voice", [voices.SAMPLE, voices.PATIENT, None])
def test_the_attestation_is_never_pre_checked(app, ctx, voice):
    """MUTATION: add `checked` to the nka input in either branch -> red."""
    kwargs = {} if voice is None else {"voice": voice}
    box = _nka_input(_render(app, **kwargs, **ctx))
    assert "checked" not in box
    assert 'type="checkbox"' in box


@pytest.mark.parametrize("ctx", STATES)
@pytest.mark.parametrize("voice", [voices.PATIENT, None, "Sample", "x"])
def test_sample_wording_never_appears_on_a_real_review(app, ctx, voice):
    kwargs = {} if voice is None else {"voice": voice}
    html = _render(app, **kwargs, **ctx)
    for phrase in SAMPLE_ONLY:
        assert phrase not in html, phrase
    assert "About you" in html
    assert _nka_label(html) == "I have no known allergies"


@pytest.mark.parametrize("ctx", STATES)
def test_a_real_review_renders_as_it_did(app, ctx):
    """A real-records review is the page it was: with the patient voice, the
    render is byte-identical to one that never heard of voices."""
    assert (_render(app, voice=voices.PATIENT, **ctx)
            == _render(app, **ctx))


@pytest.mark.parametrize("meds,allergies,says", [
    ([], [], "Tick the box if the sample person has no known allergies, "
             "to continue."),
    (ROWS["meds"], [], "Answer each medication. Then tick the box if the "
                       "sample person has no known allergies, to continue."),
    ([], ROWS["allergies"], "Answer each allergy. Then confirm one, or tick "
                            "the box if the sample person has no known "
                            "allergies, to continue."),
    (ROWS["meds"], ROWS["allergies"],
     "Answer each medication and each allergy. Then confirm an allergy, or "
     "tick the box if the sample person has no known allergies, to "
     "continue."),
])
def test_the_sample_gate_names_the_sample_person(app, meds, allergies, says):
    gate = _gate(_render(app, voice=voices.SAMPLE, meds=meds,
                         allergies=allergies))
    assert gate == says


def test_the_sample_gate_is_tied_to_the_button(app):
    """The reason Approve is off is read with the button, and stands out
    from the muted notes at 375px."""
    html = _render(app, voice=voices.SAMPLE)
    btn = re.search(r'<button[^>]*id="approve-btn"[^>]*>', html).group(0)
    assert 'aria-describedby="review-gate-msg"' in btn
    style = "".join(re.findall(r"<style>(.*?)</style>", html, re.S))
    assert re.search(r"#review-gate-msg\.alert-secondary\s*\{[^}]*color:\s*var\(--ink\)",
                     style)


def test_the_banner_is_careagents_own_sentence():
    """One sentence in two places; they may not drift apart."""
    from careagents import beta
    assert voices.SAMPLE_BANNER == beta.SAMPLE_FRAME


def test_the_back_link_only_follows_the_relay(app):
    """The engine does not know the assistant; the link is filled from the
    relay's rewritten form action, and stays hidden anywhere else."""
    html = _render(app, voice=voices.SAMPLE)
    link = re.search(r'<a[^>]*id="sample-back-to-chat"[^>]*>', html).group(0)
    assert "hidden" in link and "href" not in link


# --- the route ---------------------------------------------------------------

@pytest.fixture
def form_action(client, app, tenant_headers, auth_headers, action_registry,
                intake_ready, monkeypatch):
    monkeypatch.setenv("INTERNAL_TOKEN_MINT_SECRET", SECRET)
    return _committed_action(client, tenant_headers, auth_headers)


@pytest.fixture
def intake_ready(app, tenant_headers):
    from tests.test_intake_attestation_gate import (_allergy, _medication,
                                                    _patient, _store)
    with app.app_context():
        for resource in (_patient(), _medication(), _allergy(), _condition()):
            _store(resource, tenant_headers["X-Tenant-Id"])


def _condition():
    # A condition row too, so "the same rows" covers all three kinds
    # (#919 QA G3 gap 2: with no condition the comparison was vacuous).
    return {"resourceType": "Condition", "id": "voice-cond-1",
            "subject": {"reference": "Patient/gate-patient-1"},
            "clinicalStatus": {"coding": [{
                "system": "http://terminology.hl7.org/CodeSystem/condition-clinical",
                "code": "active"}]},
            "code": {"coding": [{"system": "http://snomed.info/sct",
                                 "code": "38341003"}]}}


def _get(client, auth_headers, action_id, query="", secret=None):
    headers = dict(auth_headers)
    if secret is not None:
        headers["X-Internal-Secret"] = secret
    r = client.get("/r6/actions/%s/review%s" % (action_id, query),
                   headers=headers)
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_data(as_text=True)


def test_careagents_with_the_secret_gets_the_sample_voice(
        client, auth_headers, form_action):
    html = _get(client, auth_headers, form_action, "?voice=sample", SECRET)
    assert "About the sample person" in html
    assert "Tick to say the sample person has no known allergies." in html
    assert "checked" not in _nka_input(html)


@pytest.mark.parametrize("secret", [None, "", "wrong", SECRET + "x"],
                         ids=["absent", "empty", "wrong", "longer"])
def test_without_the_secret_a_review_keeps_its_own_words(
        client, auth_headers, form_action, secret):
    """Any step-up holder could otherwise have a person's own attestation
    worded as "the sample person's" (the #908 brief finding, one route
    over). Without the secret the page is the plain one, byte for byte."""
    plain = _get(client, auth_headers, form_action)
    asked = _get(client, auth_headers, form_action, "?voice=sample", secret)
    assert asked == plain
    for phrase in SAMPLE_ONLY:
        assert phrase not in asked, phrase


def test_no_secret_configured_means_no_sample_voice(
        client, auth_headers, form_action, monkeypatch):
    monkeypatch.delenv("INTERNAL_TOKEN_MINT_SECRET", raising=False)
    html = _get(client, auth_headers, form_action, "?voice=sample", "")
    assert "sample person" not in html


def test_the_voice_changes_wording_and_not_the_audit(
        app, client, auth_headers, tenant_headers, form_action):
    tenant = tenant_headers["X-Tenant-Id"]

    def rows():
        # A multiset: audit ids are not insertion-ordered.
        with app.app_context():
            return Counter((r.event_type, r.resource_type, r.resource_id,
                            r.detail)
                           for r in AuditEventRecord.query.filter_by(
                               tenant_id=tenant))

    start = rows()
    _get(client, auth_headers, form_action)
    mid = rows()
    _get(client, auth_headers, form_action, "?voice=sample", SECRET)
    plain, voiced = mid - start, rows() - mid
    assert plain and voiced == plain
    assert all("sample" not in str(row[3] or "") for row in voiced)


def test_the_voice_lists_the_same_rows(client, auth_headers, form_action):
    """The same medication and allergy rows to answer, so the same gate."""
    plain = _get(client, auth_headers, form_action)
    voiced = _get(client, auth_headers, form_action, "?voice=sample", SECRET)
    names = re.compile(r'name="((?:med|allergy|condition)-\d+)"')
    assert names.findall(voiced) == names.findall(plain)
    found = names.findall(plain)
    for kind in ("med-", "allergy-", "condition-"):
        assert any(n.startswith(kind) for n in found), kind
