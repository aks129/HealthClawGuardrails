#!/usr/bin/env python3
"""Build a varied FHIR R4 cohort from the Synthetic Hospital released data.

    python scripts/synthetic_hospital_cohort.py \\
        --data ~/synthetic_hospital --out /tmp/sh-cohort

Reads the two static files the Synthetic Hospital v1.3 release ships
(github.com/sparkcpark/synthetic_hospital, MIT): `patient_profiles.jsonl` and
`benchmark_v1.3.db`. It runs none of the simulator's code. Every record is
synthetic.

Why this exists
---------------
`tests/test_synthetic_hospital_live.py` needs the simulator running. This
script gives the same patients to any HealthClaw or CareAgents instance as
ordinary FHIR bundles, so a shakeout can upload them through the paths a
person uses (CareAgents file upload, the engine's ingest endpoint).

What each bundle carries, and why
---------------------------------
- Patient: a placeholder name built from CANARY_PREFIX. Nothing that leaves
  HealthClaw may contain it.
- Encounter: the attending clinician in participant.individual.display, as
  the simulator serves it. Clinician names are the realistic names in this
  data, so they are canaries too.
- Condition: primary diagnoses and comorbidities, ICD-10-CM coded when the
  release's knowledge graph has the name, text only when it does not.
- AllergyIntolerance and MedicationStatement: content in text and notes
  only, with no codes, as in the simulator.
- Observation: lab lines parsed from the note's labs section, LOINC coded
  when the release maps the name. Blood pressure, heart rate, respiratory
  rate and oxygen saturation parsed from the vitals prose.
- DocumentReference: the full visit note as base64 attachment data.

Selection
---------
Patients are picked to vary on purpose: the oldest and youngest, the most
and fewest visits, the most allergies and medications, ICU, ED and
telehealth visits, notes with non-ASCII text, a patient with no lab lines,
and the most lab lines. A seeded random draw fills the rest. Same data and
seed, same cohort.
"""
from __future__ import annotations

import argparse
import base64
import json
import random
import re
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

CANARY_PREFIX = "Shcanary"
SOURCE_SYSTEM = "urn:synthetic-hospital:v1.3"

ICD10 = "http://hl7.org/fhir/sid/icd-10-cm"
LOINC = "http://loinc.org"
UCUM = "http://unitsofmeasure.org"
OBS_CAT = "http://terminology.hl7.org/CodeSystem/observation-category"
COND_CLIN = "http://terminology.hl7.org/CodeSystem/condition-clinical"
COND_CAT = "http://terminology.hl7.org/CodeSystem/condition-category"
ACT_CODE = "http://terminology.hl7.org/CodeSystem/v3-ActCode"

ENCOUNTER_CLASS = {
    "outpatient": ("AMB", "ambulatory"),
    "follow_up": ("AMB", "ambulatory"),
    "procedure": ("AMB", "ambulatory"),
    "ed": ("EMER", "emergency"),
    "inpatient": ("IMP", "inpatient encounter"),
    "icu": ("ACUTE", "inpatient acute"),
    "telehealth": ("VR", "virtual"),
}

# Vitals in the notes are prose. These patterns cover the common phrasings;
# anything they miss stays in the DocumentReference, which is the honest
# place for unparsed text.
_VITALS = [
    ("85354-9", "Blood pressure panel",
     re.compile(r"(?:blood pressure|BP)[^0-9]{0,20}(\d{2,3})\s*/\s*(\d{2,3})", re.I)),
    ("8867-4", "Heart rate",
     re.compile(r"(?:heart rate|pulse|HR)[^0-9]{0,20}(\d{2,3})", re.I)),
    ("9279-1", "Respiratory rate",
     re.compile(r"(?:respiratory rate|RR)[^0-9]{0,20}(\d{1,2})", re.I)),
    ("59408-5", "Oxygen saturation",
     re.compile(r"(?:oxygen saturation|SpO2|O2 sat)[^0-9]{0,20}(\d{2,3})\s*%", re.I)),
]
_VITAL_UNIT = {"8867-4": ("/min", "beats/minute"), "9279-1": ("/min", "breaths/minute"),
               "59408-5": ("%", "%")}

_LAB_LINE = re.compile(
    r"^\s*-?\s*(?P<name>[^:\n]{2,80}?)\s*:\s*(?P<value>[<>]?\s*-?[\d,]*\.?\d+)\s*"
    r"(?P<unit>[^\s(][^(\n]*?)?\s*(?:\((?:normal|ref(?:erence)?)\s*(?P<range>[^)]*)\))?\s*$",
    re.I | re.M)
_RANGE = re.compile(r"(-?[\d,]*\.?\d+)\s*[-–]\s*(-?[\d,]*\.?\d+)")


def _num(s: str) -> float | None:
    try:
        return float(s.replace(",", "").replace(" ", "").lstrip("<>"))
    except ValueError:
        return None


def _key(name: str) -> str:
    """Normalise a lab name for lookup: drop parentheticals and case."""
    return re.sub(r"\s+", " ", re.sub(r"\([^)]*\)", "", name)).strip().lower()


def load_codes(db_path: Path) -> tuple[dict[str, tuple[str, str]], dict[str, tuple[str, str]]]:
    con = sqlite3.connect(db_path)
    try:
        dx = {}
        for icd, desc, display in con.execute(
                "select icd10_code, icd10_desc, display_name from diagnoses "
                "where icd10_code is not null and icd10_code != ''"):
            dx.setdefault(_key(display), (icd, desc or display))
        labs = {}
        for display, snomed_desc, loinc, loinc_desc in con.execute(
                "select display_name, snomed_desc, loinc_code, loinc_desc "
                "from clinical_findings where loinc_code is not null and loinc_code != ''"):
            for name in (display, snomed_desc):
                if name:
                    labs.setdefault(_key(name), (loinc, loinc_desc or display))
        return dx, labs
    finally:
        con.close()


def load_sections(db_path: Path, encounter_ids: set[int]) -> dict[int, dict[str, str]]:
    con = sqlite3.connect(db_path)
    try:
        out: dict[int, dict[str, str]] = {}
        ids = sorted(encounter_ids)
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            q = ("select encounter_id, section_type, section_text from encounter_ehr_sections "
                 f"where section_type in ('labs','vitals') and encounter_id in ({','.join('?' * len(chunk))})")
            for eid, st, text in con.execute(q, chunk):
                out.setdefault(eid, {})[st] = text or ""
        return out
    finally:
        con.close()


def load_patients(jsonl: Path) -> list[dict]:
    patients = []
    with jsonl.open(encoding="utf-8") as f:
        for line in f:
            p = json.loads(line)
            p["profile"] = json.loads(p["profile"]) if isinstance(p["profile"], str) else p["profile"]
            for k in ("primary_diagnoses", "comorbidities"):
                if isinstance(p.get(k), str):
                    p[k] = json.loads(p[k])
            patients.append(p)
    return patients


def select(patients: list[dict], sections: dict, size: int, seed: int) -> list[tuple[str, dict]]:
    """Pick patients that differ on purpose; return (reason, patient) pairs."""
    def types(p):
        return {e["encounter_type"] for e in p["encounters"]}

    def lab_lines(p):
        return sum(len(_LAB_LINE.findall(sections.get(e["encounter_id"], {}).get("labs", "")))
                   for e in p["encounters"])

    def non_ascii(p):
        return any(any(ord(ch) > 127 for ch in e["note_text"]) for e in p["encounters"])

    picks: list[tuple[str, dict]] = []
    seen: set[int] = set()

    def take(reason, candidates, key=None, reverse=False):
        pool = [p for p in candidates if p["patient_id"] not in seen]
        if key:
            pool.sort(key=key, reverse=reverse)
        if pool:
            seen.add(pool[0]["patient_id"])
            picks.append((reason, pool[0]))

    take("oldest", patients, key=lambda p: p["age"], reverse=True)
    take("youngest", patients, key=lambda p: p["age"])
    take("most visits", patients, key=lambda p: len(p["encounters"]), reverse=True)
    take("fewest visits", patients, key=lambda p: (len(p["encounters"]), p["patient_id"]))
    take("most allergies", patients, key=lambda p: len(p["profile"].get("allergies") or []), reverse=True)
    take("no allergies", [p for p in patients if not p["profile"].get("allergies")],
         key=lambda p: p["patient_id"])
    take("most medications", patients,
         key=lambda p: len(p["profile"].get("home_medications") or []), reverse=True)
    take("ICU visit", [p for p in patients if "icu" in types(p)], key=lambda p: p["patient_id"])
    take("ED visit", [p for p in patients if "ed" in types(p)], key=lambda p: p["patient_id"])
    take("telehealth visit", [p for p in patients if "telehealth" in types(p)],
         key=lambda p: p["patient_id"])
    take("non-ASCII note text", [p for p in patients if non_ascii(p)], key=lambda p: p["patient_id"])
    take("no lab lines", [p for p in patients if lab_lines(p) == 0 and len(p["encounters"]) > 2],
         key=lambda p: p["patient_id"])
    take("most lab lines", patients, key=lab_lines, reverse=True)
    take("male, psychiatry", [p for p in patients if p["sex"] == "M" and any(
        "psych" in (e["department"] or "").lower() for e in p["encounters"])],
         key=lambda p: p["patient_id"])

    rng = random.Random(seed)
    rest = [p for p in patients if p["patient_id"] not in seen]
    rng.shuffle(rest)
    for p in rest[:max(0, size - len(picks))]:
        picks.append(("seeded random", p))
    return picks[:size] if size < len(picks) else picks


def _cc(text: str, system: str | None = None, code: str | None = None, display: str | None = None):
    cc: dict = {"text": text}
    if system and code:
        cc["coding"] = [{"system": system, "code": code, "display": display or text}]
    return cc


def build_bundle(p: dict, sections: dict, dx_codes: dict, lab_codes: dict,
                 stats: dict) -> dict:
    pid = f"sh-{p['patient_id']}"
    prof = p["profile"]
    encs = sorted(p["encounters"], key=lambda e: e["encounter_order"])
    first = date.fromisoformat(encs[0]["encounter_date"])
    birth = first - timedelta(days=int(p["age"]) * 365 + 120)
    ref = {"reference": f"Patient/{pid}"}
    out: list[dict] = []

    out.append({
        "resourceType": "Patient", "id": pid,
        "identifier": [{"system": SOURCE_SYSTEM, "value": str(p["patient_id"])}],
        "name": [{"family": f"{CANARY_PREFIX}{p['patient_id']}", "given": ["Synthia"]}],
        "gender": {"F": "female", "M": "male"}.get(p["sex"], "unknown"),
        "birthDate": birth.isoformat(),
        "telecom": [{"system": "phone", "value": f"555-01{p['patient_id'] % 100:02d}"}],
    })

    for e in encs:
        eid = f"sh-enc-{e['encounter_id']}"
        cls = ENCOUNTER_CLASS.get(e["encounter_type"], ("AMB", "ambulatory"))
        out.append({
            "resourceType": "Encounter", "id": eid, "status": "finished",
            "class": {"system": ACT_CODE, "code": cls[0], "display": cls[1]},
            "type": [{"text": e["encounter_type"]}],
            "subject": ref,
            "period": {"start": e["encounter_date"], "end": e["encounter_date"]},
            "reasonCode": [{"text": e["chief_complaint"]}],
            "participant": [{"individual": {"display": e["attending_name"]}}],
            "serviceProvider": {"display": e["department"]},
        })
        out.append({
            "resourceType": "DocumentReference", "id": f"sh-doc-{e['encounter_id']}",
            "status": "current",
            "type": _cc("Progress note", LOINC, "11506-3", "Progress note"),
            "subject": ref, "date": f"{e['encounter_date']}T12:00:00Z",
            "author": [{"display": e["attending_name"]}],
            "context": {"encounter": [{"reference": f"Encounter/{eid}"}]},
            "content": [{"attachment": {
                "contentType": "text/plain; charset=utf-8",
                "data": base64.b64encode(e["note_text"].encode("utf-8")).decode("ascii"),
                "title": f"{e['department']} note"}}],
        })
        sec = sections.get(e["encounter_id"], {})
        n = 0
        for m in _LAB_LINE.finditer(sec.get("labs", "")):
            name = m.group("name").strip()
            value = _num(m.group("value"))
            if value is None:
                continue
            code = lab_codes.get(_key(name))
            stats["lab_lines"] += 1
            stats["lab_coded"] += bool(code)
            unit = (m.group("unit") or "").strip()
            obs = {
                "resourceType": "Observation", "id": f"sh-lab-{e['encounter_id']}-{n}",
                "status": "final",
                "category": [{"coding": [{"system": OBS_CAT, "code": "laboratory"}]}],
                "code": _cc(name, LOINC, code[0], code[1]) if code else _cc(name),
                "subject": ref,
                "encounter": {"reference": f"Encounter/{eid}"},
                "effectiveDateTime": f"{e['encounter_date']}T08:00:00Z",
                "valueQuantity": {"value": value, "unit": unit} if unit else {"value": value},
            }
            rng = _RANGE.search(m.group("range") or "")
            if rng and _num(rng.group(1)) is not None and _num(rng.group(2)) is not None:
                low, high = _num(rng.group(1)), _num(rng.group(2))
                obs["referenceRange"] = [{"low": {"value": low, "unit": unit},
                                          "high": {"value": high, "unit": unit}}]
                if value < low or value > high:
                    obs["interpretation"] = [{"coding": [{
                        "system": "http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation",
                        "code": "L" if value < low else "H"}]}]
            out.append(obs)
            n += 1
        vitals = sec.get("vitals", "")
        for code, label, rx in _VITALS:
            m = rx.search(vitals)
            if not m:
                continue
            stats["vitals"] += 1
            obs = {
                "resourceType": "Observation", "id": f"sh-vit-{e['encounter_id']}-{code}",
                "status": "final",
                "category": [{"coding": [{"system": OBS_CAT, "code": "vital-signs"}]}],
                "code": _cc(label, LOINC, code, label), "subject": ref,
                "encounter": {"reference": f"Encounter/{eid}"},
                "effectiveDateTime": f"{e['encounter_date']}T08:00:00Z",
            }
            if code == "85354-9":
                obs["component"] = [
                    {"code": _cc("Systolic blood pressure", LOINC, "8480-6"),
                     "valueQuantity": {"value": int(m.group(1)), "unit": "mmHg",
                                       "system": UCUM, "code": "mm[Hg]"}},
                    {"code": _cc("Diastolic blood pressure", LOINC, "8462-4"),
                     "valueQuantity": {"value": int(m.group(2)), "unit": "mmHg",
                                       "system": UCUM, "code": "mm[Hg]"}}]
            else:
                ucum, unit = _VITAL_UNIT[code]
                obs["valueQuantity"] = {"value": int(m.group(1)), "unit": unit,
                                        "system": UCUM, "code": ucum}
            out.append(obs)

    onset = encs[0]["encounter_date"]
    diagnoses = [(d, "encounter-diagnosis") for d in p.get("primary_diagnoses") or []]
    diagnoses += [(d, "problem-list-item") for d in p.get("comorbidities") or []]
    diagnoses += [(d, "problem-list-item") for d in prof.get("chronic_conditions") or []]
    seen_dx: set[str] = set()
    for i, (name, cat) in enumerate(diagnoses):
        if _key(name) in seen_dx:
            continue
        seen_dx.add(_key(name))
        code = dx_codes.get(_key(name))
        stats["conditions"] += 1
        stats["conditions_coded"] += bool(code)
        out.append({
            "resourceType": "Condition", "id": f"{pid}-cond-{i}",
            "clinicalStatus": {"coding": [{"system": COND_CLIN, "code": "active"}]},
            "category": [{"coding": [{"system": COND_CAT, "code": cat}]}],
            "code": _cc(name, ICD10, code[0], code[1]) if code else _cc(name),
            "subject": ref, "onsetDateTime": onset, "recordedDate": onset,
        })

    for i, allergy in enumerate(prof.get("allergies") or []):
        out.append({
            "resourceType": "AllergyIntolerance", "id": f"{pid}-alg-{i}",
            "clinicalStatus": {"coding": [{
                "system": "http://terminology.hl7.org/CodeSystem/allergyintolerance-clinical",
                "code": "active"}]},
            "patient": ref, "recordedDate": onset,
            "code": {"text": allergy}, "note": [{"text": f"Reported allergy: {allergy}"}],
        })

    for i, med in enumerate(prof.get("home_medications") or []):
        name = med.get("name") if isinstance(med, dict) else str(med)
        dose = med.get("dose", "") if isinstance(med, dict) else ""
        out.append({
            "resourceType": "MedicationStatement", "id": f"{pid}-med-{i}",
            "status": "active", "subject": ref, "effectiveDateTime": onset,
            "medicationCodeableConcept": {"text": name},
            "dosage": [{"text": dose}] if dose else [],
            "note": [{"text": f"Home medication: {name} {dose}".strip()}],
        })

    return {"resourceType": "Bundle", "type": "collection",
            "meta": {"tag": [{"system": SOURCE_SYSTEM, "code": "synthetic"}]},
            "entry": [{"fullUrl": f"urn:uuid:{r['resourceType']}-{r['id']}", "resource": r}
                      for r in out]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--data", required=True, type=Path,
                    help="folder holding patient_profiles.jsonl and benchmark_v1.3.db")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--size", type=int, default=20)
    ap.add_argument("--seed", type=int, default=20261001)
    args = ap.parse_args(argv)

    jsonl, db = args.data / "patient_profiles.jsonl", args.data / "benchmark_v1.3.db"
    for f in (jsonl, db):
        if not f.exists():
            print(f"missing {f}", file=sys.stderr)
            return 2
    patients = load_patients(jsonl)
    dx_codes, lab_codes = load_codes(db)
    sections = load_sections(db, {e["encounter_id"] for p in patients for e in p["encounters"]})
    picks = select(patients, sections, args.size, args.seed)

    args.out.mkdir(parents=True, exist_ok=True)
    manifest = []
    for reason, p in picks:
        stats = dict.fromkeys(("lab_lines", "lab_coded", "vitals", "conditions",
                               "conditions_coded"), 0)
        bundle = build_bundle(p, sections, dx_codes, lab_codes, stats)
        path = args.out / f"sh-{p['patient_id']}.json"
        path.write_text(json.dumps(bundle, ensure_ascii=False), encoding="utf-8")
        counts: dict[str, int] = {}
        for ent in bundle["entry"]:
            rt = ent["resource"]["resourceType"]
            counts[rt] = counts.get(rt, 0) + 1
        manifest.append({
            "file": path.name, "patient_id": p["patient_id"], "reason": reason,
            "age": p["age"], "sex": p["sex"], "encounters": len(p["encounters"]),
            "canary_family": f"{CANARY_PREFIX}{p['patient_id']}",
            "clinicians": sorted({e["attending_name"] for e in p["encounters"]}),
            "counts": counts, **stats})
    (args.out / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False),
                                             encoding="utf-8")
    for m in manifest:
        print(f"{m['file']:<14} {m['reason']:<22} "
              f"visits {m['encounters']:>2} entries {sum(m['counts'].values()):>4} "
              f"labs {m['lab_coded']}/{m['lab_lines']} coded, "
              f"conditions {m['conditions_coded']}/{m['conditions']} coded")
    return 0


if __name__ == "__main__":
    sys.exit(main())
