#!/usr/bin/env python3
"""
scripts/export_healthex_mcp.py

Pull all HealthEx health records via MCP, map to US Core R4 FHIR, apply
patient-controlled de-identification, pre-tag curatr quality issues, and
write a transaction bundle ready for import_healthex.py.

Usage:
    python scripts/export_healthex_mcp.py \\
        --patient-id ev-2026 \\
        --tenant-id ev-personal \\
        --output exports/healthex-$(date +%Y-%m-%d).json \\
        [--import] \\
        [--step-up-secret $STEP_UP_SECRET] \\
        [--years 15] \\
        [--verbose]

Environment variables:
    HEALTHEX_AUTH_TOKEN   OAuth Bearer token for HealthEx MCP
                          (get from Claude.ai \u2192 Settings \u2192 Integrations \u2192 HealthEx)
    HEALTHEX_MCP_URL      Override HealthEx MCP endpoint
                          (default: https://api.healthex.io/mcp)
    ANTHROPIC_API_KEY     Required when --use-claude mode is enabled
    STEP_UP_SECRET        Required when --import is set

Re-runs are idempotent: resource IDs are deterministic hashes of key fields.
"""

import argparse
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger("export_healthex")


# ---------------------------------------------------------------------------
# HealthEx MCP client
# ---------------------------------------------------------------------------

class HealthExClient:
    """Thin httpx wrapper around the HealthEx MCP Streamable HTTP transport."""

    def __init__(self, auth_token: str, base_url: str = "https://api.healthex.io/mcp"):
        self.auth_token = auth_token
        self.base_url = base_url.rstrip("/")
        self._req_id = 0

    def _call(self, tool: str, arguments: dict) -> str:
        """Call one MCP tool. Returns the text content of the result."""
        self._req_id += 1
        payload = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
            "id": self._req_id,
        }
        headers = {
            "Authorization": f"Bearer {self.auth_token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        logger.debug("\u2192 %s %s", tool, arguments)
        try:
            resp = httpx.post(
                self.base_url,
                json=payload,
                headers=headers,
                timeout=30,
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            logger.error("HealthEx MCP %s %s \u2192 HTTP %s", tool, arguments, e.response.status_code)
            raise

        data = resp.json()
        if "error" in data:
            raise RuntimeError(f"HealthEx MCP error: {data['error']}")

        content = data.get("result", {}).get("content", [])
        texts = [c["text"] for c in content if c.get("type") == "text"]
        result = "\n".join(texts)
        logger.debug("\u2190 %d chars", len(result))
        return result

    # --- paginated helpers ---------------------------------------------------

    def get_all(self, tool: str, years: int = 15) -> list[str]:
        """
        Paginate a HealthEx tool until no more data is available.
        Returns list of raw text responses (one per page).
        """
        pages: list[str] = []
        before: str | None = None
        remaining = years

        while remaining > 0:
            chunk = min(remaining, 12)
            args: dict[str, Any] = {"years": chunk}
            if before:
                args["beforeDate"] = before

            text = self._call(tool, args)
            pages.append(text)

            # Parse pagination hint from the response
            next_before = _parse_next_before(text)
            if not next_before:
                break

            before = next_before
            remaining = years - _years_consumed(pages)
            if remaining <= 0:
                break

        return pages

    def get_health_summary(self) -> str:
        return self._call("HealthEx_get_health_summary", {})

    def get_conditions(self, years: int = 15) -> list[str]:
        return self.get_all("HealthEx_get_conditions", years)

    def get_labs(self, years: int = 15) -> list[str]:
        return self.get_all("HealthEx_get_labs", years)

    def get_immunizations(self, years: int = 15) -> list[str]:
        return self.get_all("HealthEx_get_immunizations", years)

    def get_allergies(self, years: int = 15) -> list[str]:
        return self.get_all("HealthEx_get_allergies", years)

    def get_vitals(self, years: int = 15) -> list[str]:
        return self.get_all("HealthEx_get_vitals", years)

    def get_medications(self, years: int = 15) -> list[str]:
        return self.get_all("HealthEx_get_medications", years)

    def get_procedures(self, years: int = 15) -> list[str]:
        return self.get_all("HealthEx_get_procedures", years)


def _parse_next_before(text: str) -> str | None:
    """Extract the beforeDate from a HealthEx pagination hint."""
    m = re.search(r"beforeDate:\s*[\"']?(\d{4}-\d{2}-\d{2})[\"']?", text)
    return m.group(1) if m else None


def _years_consumed(pages: list[str]) -> int:
    """Rough estimate of years already fetched based on page count."""
    return len(pages) * 3


# ---------------------------------------------------------------------------
# HealthEx tabular format parser
# ---------------------------------------------------------------------------

class TabularParser:
    """
    Parse HealthEx compressed tabular format.

    Format:
        D:1=2025-07-17|2=2025-06-21    \u2192 dictionary for column D
        S:1=active                      \u2192 dictionary for column S
        Date|Condition|ClinicalStatus|... \u2192 header row
        2025-07-17|Psoriasis|@1|...     \u2192 data row (@1 resolves via dict)
    """

    @staticmethod
    def parse(pages: list[str]) -> list[dict]:
        rows: list[dict] = []
        for page in pages:
            rows.extend(TabularParser._parse_page(page))
        return rows

    @staticmethod
    def _parse_page(text: str) -> list[dict]:
        lines = [ln.strip() for ln in text.splitlines()]
        dicts: dict[str, dict[str, str]] = {}
        headers: list[str] = []
        rows: list[dict] = []
        prev: dict = {}

        for line in lines:
            if not line or line.startswith("#") or line.startswith("---") or line.startswith("*") or line.startswith("\u26a0") or line.startswith("To retrieve"):
                continue

            # Dictionary definition line: "D:1=2025-07-17|2=2025-06-21"
            dict_match = re.match(r"^([A-Za-z][a-z]*):((?:\d+=.*?)(?:\|(?:\d+=.*?))*)$", line)
            if dict_match:
                col = dict_match.group(1)
                dicts[col] = {}
                for entry in dict_match.group(2).split("|"):
                    if "=" in entry:
                        k, v = entry.split("=", 1)
                        dicts[col][k.strip()] = v.strip()
                continue

            # Note lines
            if line.startswith("Note:") or line.startswith("Pagination"):
                continue

            # Header row (no @-references, contains pipe-separated capitalized words)
            parts = line.split("|")
            if not headers and all(re.match(r"^[A-Za-z][A-Za-z0-9\s\[\]/\(\)_\-]*$", p.strip()) for p in parts if p.strip()):
                headers = [p.strip() for p in parts]
                continue

            if not headers or "|" not in line:
                continue

            # Data row
            values = line.split("|")
            row: dict = {}
            for i, h in enumerate(headers):
                raw = values[i].strip() if i < len(values) else ""
                # Resolve @N references
                if raw.startswith("@") and raw[1:].isdigit():
                    ref = raw[1:]
                    # Find which dict column this header maps to
                    col_key = TabularParser._col_key(h)
                    resolved = dicts.get(col_key, {}).get(ref, raw)
                    row[h] = resolved
                elif raw == "":
                    # Empty = same as previous row for this column
                    row[h] = prev.get(h, "")
                else:
                    row[h] = raw

            prev = {**prev, **row}
            rows.append(row)

        return rows

    @staticmethod
    def _col_key(header: str) -> str:
        """Map column header to its dictionary abbreviation."""
        mapping = {
            "Date": "D", "ClinicalStatus": "S", "Status": "S",
            "PreferredSystem": "Sys", "Condition": "C",
            "Immunization": "I", "Allergy": "A", "Criticality": "Cr",
            "Type": "T", "Vital": "V",
        }
        return mapping.get(header, header[:1].upper())


# ---------------------------------------------------------------------------
# Deterministic ID generation
# ---------------------------------------------------------------------------

def stable_id(*parts: str) -> str:
    """Generate a short deterministic ID from key fields."""
    key = "|".join(str(p) for p in parts)
    return hashlib.sha256(key.encode()).hexdigest()[:12]


# ---------------------------------------------------------------------------
# FHIR mappers
# ---------------------------------------------------------------------------

PATIENT_REF = "Patient/pt-ev-hclaw"
SNOMED_SYS = "http://snomed.info/sct"
ICD10_SYS = "http://hl7.org/fhir/sid/icd-10-cm"
LOINC_SYS = "http://loinc.org"
CVX_SYS = "http://hl7.org/fhir/sid/cvx"
ALLERGY_CLINICAL_SYS = "http://terminology.hl7.org/CodeSystem/allergyintolerance-clinical"
ALLERGY_VERIFY_SYS = "http://terminology.hl7.org/CodeSystem/allergyintolerance-verification"
CONDITION_CLINICAL_SYS = "http://terminology.hl7.org/CodeSystem/condition-clinical"
CONDITION_VERIFY_SYS = "http://terminology.hl7.org/CodeSystem/condition-ver-status"
OBS_CAT_SYS = "http://terminology.hl7.org/CodeSystem/observation-category"
HL7_INTERP_SYS = "http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation"
DATA_ABSENT_SYS = "http://terminology.hl7.org/CodeSystem/data-absent-reason"
ROUTE_SYS = "http://terminology.hl7.org/CodeSystem/v3-RouteOfAdministration"


def map_patient(patient_id: str, gender: str, dob: str) -> dict:
    return {
        "resourceType": "Patient",
        "id": "pt-ev-hclaw",
        "meta": {
            "profile": ["http://hl7.org/fhir/us/core/StructureDefinition/us-core-patient"],
            "tag": [
                {"system": "https://healthclaw.io/tags", "code": "patient-controlled"},
                {"system": "https://healthclaw.io/tags", "code": "deidentified"},
                {"system": "https://healthclaw.io/export", "code": f"healthex-{date.today().isoformat()}"},
            ],
        },
        "identifier": [{"system": "https://healthclaw.io/patients", "value": patient_id}],
        "gender": gender.lower(),
        "birthDate": dob,
        "extension": [{
            "url": "https://healthclaw.io/redaction",
            "valueString": "name/address/telecom/photo removed; birthDate preserved (patient-controlled); MRN removed",
        }],
    }


def map_condition(row: dict) -> dict | None:
    name = row.get("Condition", "").strip()
    if not name:
        return None

    snomed = row.get("SNOMED", "").strip()
    icd10 = row.get("ICD10", "").strip()
    date_str = row.get("Date", "").strip() or row.get("OnsetDate", "").strip()
    onset = row.get("OnsetDate", "").strip()
    status = (row.get("ClinicalStatus", "") or "active").strip()
    abatement = row.get("AbatementDate", "").strip()

    if not snomed and not icd10:
        return None

    rid = "cond-" + stable_id(name, snomed, icd10, onset or date_str)

    coding = []
    if snomed:
        coding.append({"system": SNOMED_SYS, "code": snomed, "display": name})
    if icd10:
        coding.append({"system": ICD10_SYS, "code": icd10})

    resource: dict = {
        "resourceType": "Condition",
        "id": rid,
        "meta": {"profile": ["http://hl7.org/fhir/us/core/StructureDefinition/us-core-condition"]},
        "clinicalStatus": {"coding": [{"system": CONDITION_CLINICAL_SYS, "code": status or "active"}]},
        "code": {"coding": coding, "text": name},
        "subject": {"reference": PATIENT_REF},
    }

    if onset:
        resource["onsetDateTime"] = onset
    if date_str:
        resource["recordedDate"] = date_str
    if abatement:
        resource["abatementDateTime"] = abatement

    enc = row.get("Encounter", "").strip()
    if enc:
        resource["encounter"] = {"identifier": {"value": enc}}

    return resource


def map_observation_lab(row: dict) -> dict | None:
    code_str = row.get("Code", "").strip()
    test = row.get("Test", "").strip()
    result = row.get("Result", "").strip()
    date_str = row.get("Date", "").strip()
    flag = row.get("Flag", "").strip()

    if not test or not date_str:
        return None

    rid = "obs-lab-" + stable_id(code_str or test, date_str)

    coding = []
    if code_str and code_str != "-":
        coding.append({"system": LOINC_SYS, "code": code_str, "display": test})

    resource: dict = {
        "resourceType": "Observation",
        "id": rid,
        "status": "final",
        "category": [{"coding": [{"system": OBS_CAT_SYS, "code": "laboratory"}]}],
        "code": {"coding": coding, "text": test} if coding else {"text": test},
        "subject": {"reference": PATIENT_REF},
        "effectiveDateTime": date_str,
    }

    # Try to interpret result as quantity
    qty_match = re.match(r"^([\d.]+)\s*(.*)$", result)
    if qty_match and result not in ("POSITIVE", "NEGATIVE", "Yes", "No", "Never", "Former", "Current"):
        resource["valueQuantity"] = {
            "value": float(qty_match.group(1)),
            "unit": qty_match.group(2).strip() or "units",
        }
    elif result:
        if result in ("POSITIVE", "NEGATIVE"):
            snomed_code = "10828004" if result == "POSITIVE" else "260385009"
            resource["valueCodeableConcept"] = {
                "coding": [{"system": SNOMED_SYS, "code": snomed_code, "display": result}],
                "text": result,
            }
        else:
            resource["valueString"] = result

    if flag:
        resource["interpretation"] = [{"coding": [{"system": HL7_INTERP_SYS, "code": flag}]}]

    # Social history reclassification
    social_loincs = {"72166-2", "11331-6", "11343-1", "63586-2", "72109-2"}
    if code_str in social_loincs:
        resource["category"] = [{"coding": [{"system": OBS_CAT_SYS, "code": "social-history"}]}]
        # Map smoking status text to SNOMED
        if code_str == "72166-2":
            smoking_map = {
                "Never": ("266919005", "Never smoked tobacco (finding)"),
                "Former": ("8517006", "Ex-smoker (finding)"),
                "Current": ("77176002", "Current smoker"),
            }
            mapped = smoking_map.get(result)
            if mapped:
                resource["valueCodeableConcept"] = {
                    "coding": [{"system": SNOMED_SYS, "code": mapped[0], "display": mapped[1]}],
                    "text": result,
                }
                resource.pop("valueString", None)

    # Handle narrative/imaging observations
    if test in ("Narrative", "Impression") and result:
        resource["category"] = [{"coding": [{"system": OBS_CAT_SYS, "code": "imaging"}]}]
        resource["valueString"] = result[:500]

    return resource


def map_immunization(row: dict) -> dict | None:
    name = row.get("Immunization", "").strip()
    cvx = row.get("CVX", "").strip()
    occurrence = row.get("OccurrenceDate", row.get("Date", "")).strip()
    status = (row.get("Status", "completed")).strip()
    lot = row.get("LotNumber", "").strip()
    expiry = row.get("ExpirationDate", "").strip()
    dose = row.get("Dose", "").strip()
    route = row.get("Route", "").strip()
    site = row.get("Site", "").strip()
    performer = row.get("Performers", "").strip()

    if not occurrence or not (name or cvx):
        return None

    rid = "imm-" + stable_id(cvx or name, occurrence)

    coding = []
    if cvx:
        coding.append({"system": CVX_SYS, "code": cvx, "display": name})

    resource: dict = {
        "resourceType": "Immunization",
        "id": rid,
        "meta": {"profile": ["http://hl7.org/fhir/us/core/StructureDefinition/us-core-immunization"]},
        "status": status,
        "vaccineCode": {"coding": coding, "text": name} if coding else {"text": name},
        "patient": {"reference": PATIENT_REF},
        "occurrenceDateTime": occurrence,
    }

    if lot:
        resource["lotNumber"] = lot
    if expiry:
        resource["expirationDate"] = expiry
    if dose:
        qty_m = re.match(r"([\d.]+)\s*(mL|mg|mcg)?", dose)
        if qty_m:
            resource["doseQuantity"] = {
                "value": float(qty_m.group(1)),
                "unit": qty_m.group(2) or "mL",
            }
    if route:
        resource["route"] = {"coding": [{"system": ROUTE_SYS, "code": route}]}
    if site:
        resource["site"] = {"text": site}
    if performer:
        resource["performer"] = [{"actor": {"display": performer}}]

    return resource


def map_allergy(row: dict) -> dict | None:
    name = row.get("Allergy", "").strip()
    snomed = row.get("SNOMED", row.get("PreferredCode", "")).strip()
    date_str = row.get("Date", "").strip()
    clinical = (row.get("ClinicalStatus", "active")).strip()
    verify = (row.get("VerificationStatus", "confirmed")).strip()

    if not name:
        return None

    rid = "allergy-" + stable_id(snomed or name, date_str)

    coding = []
    if snomed:
        coding.append({"system": SNOMED_SYS, "code": snomed, "display": name})

    resource: dict = {
        "resourceType": "AllergyIntolerance",
        "id": rid,
        "meta": {"profile": ["http://hl7.org/fhir/us/core/StructureDefinition/us-core-allergyintolerance"]},
        "clinicalStatus": {"coding": [{"system": ALLERGY_CLINICAL_SYS, "code": clinical}]},
        "verificationStatus": {"coding": [{"system": ALLERGY_VERIFY_SYS, "code": verify}]},
        "code": {"coding": coding, "text": name} if coding else {"text": name},
        "patient": {"reference": PATIENT_REF},
    }

    if date_str:
        resource["recordedDate"] = date_str

    crit = row.get("Criticality", "").strip().lower()
    if crit:
        resource["criticality"] = crit

    return resource


def map_vital(row: dict) -> dict | None:
    vital_type = row.get("Type", "").strip()
    value = row.get("Value", "").strip()
    unit = row.get("Unit", "").strip()
    date_str = row.get("Date", "").strip()
    flag = row.get("Flag", "").strip()

    if not vital_type or not date_str:
        return None

    vital_loinc = {
        "Blood Pressure": ("85354-9", "Blood pressure panel"),
        "Heart Rate": ("8867-4", "Heart rate"),
        "Body Temperature": ("8310-5", "Body temperature"),
        "Respiratory Rate": ("9279-1", "Respiratory rate"),
        "Oxygen Saturation": ("2708-6", "Oxygen saturation in Arterial blood"),
        "Weight": ("29463-7", "Body weight"),
        "Height": ("8302-2", "Body height"),
        "Body Mass Index": ("39156-5", "Body mass index"),
        "Pain Score": ("72514-3", "Pain severity - 0-10 verbal numeric rating"),
    }

    loinc = vital_loinc.get(vital_type)
    rid = "obs-vital-" + stable_id(vital_type, date_str)

    coding = []
    if loinc:
        coding.append({"system": LOINC_SYS, "code": loinc[0], "display": loinc[1]})

    resource: dict = {
        "resourceType": "Observation",
        "id": rid,
        "status": "final",
        "category": [{"coding": [{"system": OBS_CAT_SYS, "code": "vital-signs"}]}],
        "code": {"coding": coding, "text": vital_type} if coding else {"text": vital_type},
        "subject": {"reference": PATIENT_REF},
        "effectiveDateTime": date_str,
    }

    qty_m = re.match(r"([\d.]+(?:/[\d.]+)?)", value)
    if qty_m and vital_type != "Pain Score":
        resource["valueQuantity"] = {
            "value": float(qty_m.group(1).split("/")[0]),
            "unit": unit or "units",
        }
    elif value:
        resource["valueString"] = value

    if flag:
        resource["interpretation"] = [{"coding": [{"system": HL7_INTERP_SYS, "code": flag}]}]

    return resource


# ---------------------------------------------------------------------------
# Curatr pre-tagger
# ---------------------------------------------------------------------------

CURATR_TAG_SYS = "https://healthclaw.io/curatr"


class CuratrPreTagger:
    """
    Detect known quality issue patterns in mapped FHIR resources and
    annotate them with curatr meta tags and notes before import.
    """

    def __init__(self, resources: list[dict]):
        self.resources = resources
        self._issues: list[dict] = []

    def run(self) -> list[dict]:
        self._detect_smoking_contradiction()
        self._detect_misleading_ab_flags()
        self._detect_missing_results()
        self._detect_icd9_codes()
        self._detect_care_gaps()
        return self.resources

    @property
    def issues(self) -> list[dict]:
        return self._issues

    def _get_observations(self) -> list[dict]:
        return [r for r in self.resources if r.get("resourceType") == "Observation"]

    def _get_conditions(self) -> list[dict]:
        return [r for r in self.resources if r.get("resourceType") == "Condition"]

    def _tag(self, resource: dict, code: str, note: str, severity: str = "warning"):
        if "meta" not in resource:
            resource["meta"] = {}
        tags = resource["meta"].setdefault("tag", [])
        tags.append({"system": CURATR_TAG_SYS, "code": f"flag:{code}"})
        notes = resource.setdefault("note", [])
        notes.append({"text": f"CURATR {severity.upper()}: {note}"})
        self._issues.append({
            "resource_id": resource["id"],
            "resource_type": resource["resourceType"],
            "code": code,
            "severity": severity,
            "note": note,
        })

    def _detect_smoking_contradiction(self):
        """LOINC 72166-2 with different values across encounters = contradiction."""
        smoking_obs = [
            o for o in self._get_observations()
            if any(
                c.get("code") == "72166-2"
                for c in o.get("code", {}).get("coding", [])
            )
        ]
        if len(smoking_obs) < 2:
            return

        values = set()
        for o in smoking_obs:
            v = o.get("valueCodeableConcept", {})
            for c in v.get("coding", []):
                values.add(c.get("code", ""))
            if o.get("valueString"):
                values.add(o["valueString"])

        if len(values) > 1:
            # Sort by date, oldest is the likely incorrect one
            dated = sorted(
                smoking_obs,
                key=lambda o: o.get("effectiveDateTime", "0000"),
            )
            oldest = dated[0]
            newest = dated[-1]
            for o in smoking_obs:
                o_date = o.get("effectiveDateTime", "")
                other_dates = [x.get("effectiveDateTime") for x in smoking_obs if x["id"] != o["id"]]
                self._tag(
                    o,
                    "contradiction",
                    f"LOINC 72166-2 tobacco status contradicts {other_dates}. "
                    f"{'This older entry is likely incorrect \u2014 patient attestation required.' if o is oldest else 'This newer entry is likely authoritative.'}",
                    severity="HIGH",
                )

    def _detect_misleading_ab_flags(self):
        """
        Antibody titer results flagged H (high) are clinically misleading \u2014
        any positive antibody titer indicates immunity, not pathology.
        """
        ab_loincs = {
            "22322-2": "Hepatitis B Surface Ab",
            "5403-1": "Varicella IgG",
            "7966-5": "Mumps IgG",
            "6801007": "Rubella IgG",
        }
        for obs in self._get_observations():
            codes = {c.get("code") for c in obs.get("code", {}).get("coding", [])}
            interp = obs.get("interpretation", [])
            interp_codes = {
                c.get("code")
                for block in interp
                for c in block.get("coding", [])
            }
            matched = codes & set(ab_loincs.keys())
            if matched and "H" in interp_codes:
                loinc = next(iter(matched))
                name = ab_loincs[loinc]
                val = obs.get("valueQuantity", {}).get("value", "")
                self._tag(
                    obs,
                    "misleading-interpretation",
                    f"{name} value {val} flagged H (High). For antibody titers, "
                    f"any positive value indicates immunity \u2014 H flag reads as pathology. "
                    f"Recommend updating to N (Normal/Protective) with referenceRange annotation.",
                    severity="MEDIUM",
                )

    def _detect_missing_results(self):
        """Lab observations with no value of any kind."""
        for obs in self._get_observations():
            cat_codes = {
                c.get("code")
                for block in obs.get("category", [])
                for c in block.get("coding", [])
            }
            if "laboratory" not in cat_codes:
                continue
            has_value = any(
                k in obs for k in (
                    "valueQuantity", "valueCodeableConcept", "valueString",
                    "valueBoolean", "valueInteger", "valueRange",
                )
            )
            if not has_value and "dataAbsentReason" not in obs:
                obs["status"] = "unknown"
                obs["dataAbsentReason"] = {
                    "coding": [{"system": "http://terminology.hl7.org/CodeSystem/data-absent-reason", "code": "unknown"}]
                }
                self._tag(
                    obs,
                    "missing-result",
                    f"No result value in imported record for {obs.get('code', {}).get('text', obs['id'])}. "
                    f"Status set to unknown with dataAbsentReason. "
                    f"Likely incomplete C-CDA parse from source system.",
                    severity="LOW",
                )

    def _detect_icd9_codes(self):
        """Conditions still using ICD-9-CM code systems."""
        icd9_systems = {
            "http://hl7.org/fhir/sid/icd-9-cm",
            "http://hl7.org/fhir/sid/icd-9",
            "2.16.840.1.113883.6.103",
        }
        # Also detect by code pattern: ICD-9 numeric codes like 250.00, 401.9
        icd9_pattern = re.compile(r"^\d{3}(\.\d{1,2})?$")

        for cond in self._get_conditions():
            for coding in cond.get("code", {}).get("coding", []):
                sys = coding.get("system", "")
                code = coding.get("code", "")
                if sys in icd9_systems or (icd9_pattern.match(code) and sys == ICD10_SYS):
                    self._tag(
                        cond,
                        "icd9-deprecated",
                        f"Condition code {code} is ICD-9-CM. Retired October 2015. "
                        f"Not accepted by US payers or quality measures. "
                        f"Map to ICD-10-CM equivalent using CMS GEMs crosswalk.",
                        severity="CRITICAL",
                    )
                    break

    def _detect_care_gaps(self):
        """Flag active conditions with no linked treatment."""
        cond_codes = {
            coding.get("code", "")
            for cond in self._get_conditions()
            if cond.get("clinicalStatus", {}).get("coding", [{}])[0].get("code") == "active"
            for coding in cond.get("code", {}).get("coding", [])
        }
        med_requests = [r for r in self.resources if r.get("resourceType") == "MedicationRequest"]
        procedures = [r for r in self.resources if r.get("resourceType") == "Procedure"]

        # Psoriasis without treatment
        psoriasis_snomedcodes = {"9014002"}
        has_psoriasis = bool(cond_codes & psoriasis_snomedcodes)
        if has_psoriasis and not med_requests and not procedures:
            for cond in self._get_conditions():
                for coding in cond.get("code", {}).get("coding", []):
                    if coding.get("code") in psoriasis_snomedcodes:
                        self._tag(
                            cond,
                            "care-gap:no-treatment",
                            "Active psoriasis with no linked MedicationRequest or Procedure "
                            "(phototherapy, biologics, topical corticosteroids) in imported record. "
                            "May be incomplete AHN C-CDA import. Verify current treatment with provider.",
                            severity="MEDIUM",
                        )


# ---------------------------------------------------------------------------
# De-identification
# ---------------------------------------------------------------------------

def deidentify_patient_controlled(resource: dict, patient_id: str) -> dict:
    """
    Patient-controlled de-identification:
    - Remove: name, telecom, address, photo, facility MRNs
    - Preserve: birthDate, gender, coded clinical elements
    - Inject: healthclaw.io canonical identifier
    """
    if resource.get("resourceType") != "Patient":
        return resource

    keep_keys = {"resourceType", "id", "meta", "gender", "birthDate",
                 "generalPractitioner", "extension"}
    cleaned = {k: v for k, v in resource.items() if k in keep_keys}

    cleaned["identifier"] = [{"system": "https://healthclaw.io/patients", "value": patient_id}]

    return cleaned


# ---------------------------------------------------------------------------
# Bundle builder
# ---------------------------------------------------------------------------

def build_bundle(resources: list[dict], patient_id: str) -> dict:
    """Wrap resources in a FHIR R4 transaction bundle."""
    timestamp = datetime.now(timezone.utc).isoformat()

    entries = []
    for res in resources:
        rtype = res["resourceType"]
        rid = res.get("id", "")

        # Use PUT for patient (idempotent upsert), POST for everything else
        if rtype == "Patient":
            method, url = "PUT", f"Patient/{rid}"
        else:
            method, url = "POST", rtype

        entries.append({
            "fullUrl": f"urn:uuid:{rid}",
            "resource": res,
            "request": {"method": method, "url": url},
        })

    return {
        "resourceType": "Bundle",
        "id": f"healthex-export-{date.today().isoformat()}",
        "type": "transaction",
        "timestamp": timestamp,
        "meta": {
            "tag": [
                {"system": "https://healthclaw.io/tags", "code": "patient-controlled"},
                {"system": "https://healthclaw.io/tags", "code": "deidentified"},
                {"system": "https://healthclaw.io/source", "code": f"healthex-pull-{date.today().isoformat()}"},
            ]
        },
        "entry": entries,
    }


# ---------------------------------------------------------------------------
# Summary report
# ---------------------------------------------------------------------------

def print_summary(resources: list[dict], issues: list[dict], output_path: str):
    counts: dict[str, int] = {}
    for r in resources:
        rt = r["resourceType"]
        counts[rt] = counts.get(rt, 0) + 1

    print("\n=== HealthEx export summary ===")
    print(f"  Output: {output_path}")
    print(f"  Total resources: {len(resources)}")
    for rt, n in sorted(counts.items()):
        print(f"    {rt}: {n}")

    if issues:
        print(f"\n  Curatr pre-tagged issues: {len(issues)}")
        sev_order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
        for issue in sorted(issues, key=lambda i: sev_order.get(i["severity"], 9)):
            print(f"    [{issue['severity']}] {issue['resource_type']}/{issue['resource_id']} \u2192 {issue['code']}")
    else:
        print("\n  Curatr: no issues detected")

    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Export HealthEx records to a FHIR R4 transaction bundle for HealthClaw import",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--patient-id", default="ev-2026",
                   help="HealthClaw patient identifier (injected as canonical identifier)")
    p.add_argument("--tenant-id", default="ev-personal",
                   help="HealthClaw tenant to import into")
    p.add_argument("--output", default=None,
                   help="Output bundle path (default: exports/healthex-YYYY-MM-DD.json)")
    p.add_argument("--years", type=int, default=15,
                   help="Years of history to pull (default: 15)")
    p.add_argument("--import", dest="do_import", action="store_true",
                   help="Automatically call import_healthex.py after export")
    p.add_argument("--step-up-secret",
                   default=os.environ.get("STEP_UP_SECRET", ""),
                   help="Step-up secret for import (required when --import is set)")
    p.add_argument("--auth-token",
                   default=os.environ.get("HEALTHEX_AUTH_TOKEN", ""),
                   help="HealthEx OAuth Bearer token (or set HEALTHEX_AUTH_TOKEN)")
    p.add_argument("--mcp-url",
                   default=os.environ.get("HEALTHEX_MCP_URL", "https://api.healthex.io/mcp"),
                   help="HealthEx MCP endpoint URL")
    p.add_argument("--dry-run", action="store_true",
                   help="Build bundle from cached/test data without calling HealthEx")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="Debug logging")
    return p.parse_args()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    args = parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
        stream=sys.stderr,
    )

    # --- output path -------------------------------------------------------
    output = args.output or f"exports/healthex-{date.today().isoformat()}.json"
    Path(output).parent.mkdir(parents=True, exist_ok=True)

    # --- pull data ----------------------------------------------------------
    if args.dry_run:
        logger.warning("--dry-run: building empty bundle, skipping HealthEx pull")
        all_resources = [map_patient(args.patient_id, "male", "1981-05-29")]
    else:
        if not args.auth_token:
            logger.error(
                "No HealthEx auth token. Set HEALTHEX_AUTH_TOKEN env var or pass --auth-token.\n"
                "Get the token from: Claude.ai \u2192 Settings \u2192 Integrations \u2192 HealthEx \u2192 copy OAuth token"
            )
            sys.exit(1)

        client = HealthExClient(auth_token=args.auth_token, base_url=args.mcp_url)

        # Health summary to confirm connection and get gender/DOB
        logger.info("Pulling health summary...")
        summary_text = client.get_health_summary()
        gender = "male"
        dob = "1981-05-29"
        dob_m = re.search(r"DOB\s+([\d]{4}-[\d]{2}-[\d]{2})", summary_text)
        if dob_m:
            dob = dob_m.group(1)

        # Build patient resource
        patient = map_patient(args.patient_id, gender, dob)
        patient = deidentify_patient_controlled(patient, args.patient_id)

        all_resources: list[dict] = [patient]

        # Conditions
        logger.info("Pulling conditions (up to %d years)...", args.years)
        cond_pages = client.get_conditions(args.years)
        cond_rows = TabularParser.parse(cond_pages)
        seen_cond: set[str] = set()
        for row in cond_rows:
            r = map_condition(row)
            if r and r["id"] not in seen_cond:
                seen_cond.add(r["id"])
                all_resources.append(r)
        logger.info("  \u2192 %d conditions", len(seen_cond))

        # Labs
        logger.info("Pulling labs (up to %d years)...", args.years)
        lab_pages = client.get_labs(args.years)
        lab_rows = TabularParser.parse(lab_pages)
        seen_labs: set[str] = set()
        for row in lab_rows:
            r = map_observation_lab(row)
            if r and r["id"] not in seen_labs:
                seen_labs.add(r["id"])
                all_resources.append(r)
        logger.info("  \u2192 %d observations", len(seen_labs))

        # Immunizations
        logger.info("Pulling immunizations (up to %d years)...", args.years)
        imm_pages = client.get_immunizations(args.years)
        imm_rows = TabularParser.parse(imm_pages)
        seen_imm: set[str] = set()
        for row in imm_rows:
            r = map_immunization(row)
            if r and r["id"] not in seen_imm:
                seen_imm.add(r["id"])
                all_resources.append(r)
        logger.info("  \u2192 %d immunizations", len(seen_imm))

        # Allergies
        logger.info("Pulling allergies (up to %d years)...", args.years)
        allergy_pages = client.get_allergies(args.years)
        allergy_rows = TabularParser.parse(allergy_pages)
        seen_allergy: set[str] = set()
        for row in allergy_rows:
            r = map_allergy(row)
            if r and r["id"] not in seen_allergy:
                seen_allergy.add(r["id"])
                all_resources.append(r)
        logger.info("  \u2192 %d allergies", len(seen_allergy))

        # Vitals
        logger.info("Pulling vitals (up to %d years)...", args.years)
        vitals_pages = client.get_vitals(args.years)
        vitals_rows = TabularParser.parse(vitals_pages)
        seen_vitals: set[str] = set()
        for row in vitals_rows:
            r = map_vital(row)
            if r and r["id"] not in seen_vitals:
                seen_vitals.add(r["id"])
                all_resources.append(r)
        logger.info("  \u2192 %d vitals", len(seen_vitals))

        # Medications (optional \u2014 may return empty)
        logger.info("Pulling medications (up to %d years)...", args.years)
        try:
            med_pages = client.get_medications(args.years)
            logger.info("  \u2192 medication data available (mapper not yet implemented, skipping)")
        except Exception as e:
            logger.debug("Medications pull failed (likely no data): %s", e)

        # Procedures (optional)
        logger.info("Pulling procedures (up to %d years)...", args.years)
        try:
            proc_pages = client.get_procedures(args.years)
            logger.info("  \u2192 procedure data available (mapper not yet implemented, skipping)")
        except Exception as e:
            logger.debug("Procedures pull failed (likely no data): %s", e)

    # --- curatr pre-tag -----------------------------------------------------
    logger.info("Running curatr pre-tagger...")
    tagger = CuratrPreTagger(all_resources)
    tagged_resources = tagger.run()
    issues = tagger.issues
    logger.info("  \u2192 %d issues detected", len(issues))

    # --- build bundle -------------------------------------------------------
    bundle = build_bundle(tagged_resources, args.patient_id)

    # --- write --------------------------------------------------------------
    with open(output, "w") as f:
        json.dump(bundle, f, indent=2)
    logger.info("Bundle written to %s (%d resources, %.1f KB)",
                output, len(tagged_resources), Path(output).stat().st_size / 1024)

    # --- print summary ------------------------------------------------------
    print_summary(tagged_resources, issues, output)

    # --- optional import ----------------------------------------------------
    if args.do_import:
        if not args.step_up_secret:
            logger.error("--import requires --step-up-secret or STEP_UP_SECRET env var")
            sys.exit(1)

        cmd = [
            sys.executable,
            "scripts/import_healthex.py",
            "--bundle-file", output,
            "--tenant-id", args.tenant_id,
            "--step-up-secret", args.step_up_secret,
        ]
        logger.info("Running import: %s", " ".join(cmd))
        result = subprocess.run(cmd)
        sys.exit(result.returncode)


if __name__ == "__main__":
    main()
