"""The visit brief can word its sentences about a made-up sample person.

A patient tester on CareAgents' sample records opened "Get me ready for my
visit" and read, in a red box, "Between Oct 2 and Oct 8, your creatinine
rose ... Contact your doctor promptly." The engine does not know which
tenant is a sample; CareAgents does. So the brief takes an explicit,
optional `voice=sample`, and words its own sentences about "the sample
person" instead of "you".

What `voice` may never do: change which records are read, what is redacted,
or which items the brief lists. Only the wording of the engine's own
sentences moves. With no voice, or any value but "sample", the brief is
exactly what it was. Synthetic data only.
"""

from __future__ import annotations

import json
import re

import pytest

from r6.brief.engine import DOSE_NOT_LISTED
from r6.seed import seed_demo_data
from tests.test_brief_routes import _URL, _store

SECOND_PERSON = re.compile(r"\b(you|your|yours|yourself)\b", re.IGNORECASE)


def _strings(body):
    """Every sentence-bearing string the brief sends: field labels and
    values, and the care-gaps reason."""
    out = []
    for section in body.get("extension", []):
        for e in section.get("extension", []):
            if e.get("url") == "field":
                f = json.loads(e["valueString"])
                out += [f["label"], f["value"]]
            elif e.get("url") == "reason":
                out.append(e["valueString"])
    return out


def _items(body):
    """What the brief lists, without its wording: section, label, and the
    record each item cites."""
    out = []
    for section in body.get("extension", []):
        for e in section.get("extension", []):
            if e.get("url") == "field":
                f = json.loads(e["valueString"])
                out.append((section["url"], f["label"], f["sourceType"],
                            f["sourceId"]))
            else:
                out.append((section["url"], e["url"]))
    return sorted(out)


def _scenario(app, tenant_id, name):
    """Four shapes of record, so every sentence family is reached: the seeded
    sample, no Patient, two Patients, and a Patient with no sex recorded."""
    if name == "seeded":
        with app.app_context():
            seed_demo_data(tenant_id=tenant_id)
    elif name == "two-patients":
        for pid, sex in (("a", "female"), ("b", "male")):
            _store(app, {"resourceType": "Patient", "id": pid,
                         "birthDate": "1960-01-01", "gender": sex}, tenant_id)
    elif name == "no-sex":
        _store(app, {"resourceType": "Patient", "id": "p-nosex",
                     "birthDate": "1960-01-01"}, tenant_id)


SCENARIOS = ("seeded", "no-patient", "two-patients", "no-sex")


#: The internal secret CareAgents holds. The engine honours the sample voice
#: only with it (#908 security review; tests/test_brief_sample_voice_security.py
#: pins the refusals).
SECRET = "brief-voice-test-secret"


@pytest.fixture(autouse=True)
def _internal_secret(monkeypatch):
    monkeypatch.setenv("INTERNAL_TOKEN_MINT_SECRET", SECRET)


def _get(client, headers, query=""):
    """The brief as CareAgents asks for it: a voice travels with the
    internal secret."""
    if query:
        headers = {**headers, "X-Internal-Secret": SECRET}
    r = client.get(_URL + query, headers=headers)
    assert r.status_code == 200
    return r.get_json()


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_the_sample_voice_never_says_you(
        app, client, tenant_id, tenant_headers, scenario):
    _scenario(app, tenant_id, scenario)
    body = _get(client, tenant_headers, "?voice=sample")
    strings = _strings(body)
    assert strings, "the scenario produced no sentences to check"
    leaks = [s for s in strings if SECOND_PERSON.search(s)]
    assert leaks == []


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_the_voice_changes_wording_and_nothing_else(
        app, client, tenant_id, tenant_headers, scenario):
    _scenario(app, tenant_id, scenario)
    real = _get(client, tenant_headers)
    sample = _get(client, tenant_headers, "?voice=sample")
    assert _items(sample) == _items(real)


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_without_the_sample_voice_the_brief_is_unchanged(
        app, client, tenant_id, tenant_headers, scenario):
    """No voice, voice=patient and an unknown voice are byte-equal."""
    _scenario(app, tenant_id, scenario)
    real = _get(client, tenant_headers)
    assert _get(client, tenant_headers, "?voice=patient") == real
    assert _get(client, tenant_headers, "?voice=SAMPLE ") == real
    assert _get(client, tenant_headers, "?voice=nonsense") == real


def test_the_sample_trend_names_the_sample_person(
        app, client, tenant_id, tenant_headers):
    _scenario(app, tenant_id, "seeded")
    body = _get(client, tenant_headers, "?voice=sample")
    trend = [s for s in _strings(body) if "creatinine rose" in s]
    [line] = trend
    assert "the sample person's creatinine rose from 0.8 to 1.3 mg/dL" in line
    # The advice survives, about the sample person.
    assert "contact their doctor promptly" in line


# --- today's wording, pinned -------------------------------------------------
# With no voice every sentence reads exactly as before this change.

def test_the_real_trend_sentence_is_todays(
        app, client, tenant_id, tenant_headers):
    _scenario(app, tenant_id, "seeded")
    trend = [s for s in _strings(_get(client, tenant_headers))
             if "creatinine rose" in s]
    [line] = trend
    assert re.fullmatch(
        r"Between \w{3} \d{1,2} and \w{3} \d{1,2}, \d{4}, your creatinine "
        r"rose from 0\.8 to 1\.3 mg/dL in 6 days\. A rise like this can mean "
        r"the kidneys are under strain\. Contact your doctor promptly\.",
        line), line


def test_the_real_care_gap_lines_are_todays(
        app, client, tenant_id, tenant_headers):
    _scenario(app, tenant_id, "seeded")
    strings = _strings(_get(client, tenant_headers))
    assert ("You may be due for an influenza (flu) vaccine (yearly). We "
            "didn't find one in your records. You may already have had it "
            "elsewhere, so check with your doctor.") in strings


def test_the_real_caller_reasons_are_todays(
        app, client, tenant_id, tenant_headers):
    from r6.caregaps.report import _NOT_EVALUATED_NOTES
    assert _strings(_get(client, tenant_headers)) == [
        _NOT_EVALUATED_NOTES["no-patient"]]


def test_the_real_demographic_reason_is_todays(
        app, client, tenant_id, tenant_headers):
    _scenario(app, tenant_id, "no-sex")
    reason = _strings(_get(client, tenant_headers))[-1]
    assert reason.startswith(
        "1 screening could not be checked because your sex was not recorded "
        "in the records this check can read: ")


def test_a_missing_result_reads_as_today(app, client, tenant_id,
                                          tenant_headers):
    _store(app, {"resourceType": "Observation", "id": "o-empty",
                 "status": "final",
                 "code": {"coding": [{"system": "http://loinc.org",
                                      "code": "4548-4"}]},
                 "effectiveDateTime": "2026-09-01T00:00:00Z"}, tenant_id)
    real = _strings(_get(client, tenant_headers))
    assert "Result not listed in your records (Sep 1, 2026)" in real
    sample = _strings(_get(client, tenant_headers, "?voice=sample"))
    assert "Result not listed in these made-up records (Sep 1, 2026)" in sample


# --- F3: a medicine's name carries its strength -----------------------------

def test_the_medicine_row_does_not_contradict_its_name(
        app, client, tenant_id, tenant_headers):
    """RxNorm names a clinical drug with its strength ("Metformin 500 mg"),
    so "Dose not listed" beside it contradicted the name. What the brief
    leaves out is the directions (Dosage.text is free text, #884 QA F3)."""
    _scenario(app, tenant_id, "seeded")
    for query in ("", "?voice=sample"):
        body = _get(client, tenant_headers, query)
        meds = [json.loads(e["valueString"])
                for s in body["extension"]
                if s["url"].endswith("-medications")
                for e in s["extension"] if e["url"] == "field"]
        assert [(m["label"], m["value"]) for m in meds] == [
            ("Metformin 500 mg", DOSE_NOT_LISTED)]
    assert DOSE_NOT_LISTED == "How to take it isn't shown here."
    assert "dose" not in DOSE_NOT_LISTED.lower()


# --- the care-gap lines the brief does not list ---------------------------
# The brief lists due screenings only; up-to-date and could-not-check lines
# reach the chat's care-gaps tool. Same rule: the voice rewords, never adds
# or drops a line.

_RESULTS = [
    {"rule_id": "flu", "title": "Influenza (flu) vaccine", "cadence": "yearly",
     "status": "due", "applicable": True,
     "note": "We didn't find one in your records. You may already have had "
             "it elsewhere, so check with your doctor."},
    {"rule_id": "bp", "title": "Blood pressure check", "cadence": "yearly",
     "status": "up_to_date", "applicable": True, "last_done": "2026-09-01",
     "note": "recommended yearly"},
    {"rule_id": "crc", "title": "Colorectal cancer screening",
     "cadence": "every 10 years", "status": "indeterminate",
     "applicable": True, "indeterminate_reason": "evidence-not-read",
     "unread_evidence": "stool tests",
     "note": "We don't read stool tests yet, so we can't tell whether this "
             "is up to date. Ask your doctor about it."},
]


def _without(d, *keys):
    return {k: v for k, v in d.items() if k not in keys}


def test_care_gap_lines_in_the_sample_voice():
    from r6.caregaps.report import build_consumer_summary
    real = build_consumer_summary(_RESULTS)
    sample = build_consumer_summary(_RESULTS, voice="sample")
    assert [_without(x, "message") for x in real["lines"]] == [
        _without(x, "message") for x in sample["lines"]]
    assert _without(real, "lines", "unevaluated_note") == _without(
        sample, "lines", "unevaluated_note")
    for line in sample["lines"]:
        assert not SECOND_PERSON.search(line["message"]), line["message"]
        assert "sample person" in line["message"]
    assert not SECOND_PERSON.search(sample["unevaluated_note"])
    assert real == build_consumer_summary(_RESULTS, voice="patient")
    assert real["lines"][1]["message"].startswith(
        "Your blood pressure check is up to date on timing")
