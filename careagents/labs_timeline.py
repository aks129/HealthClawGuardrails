"""Group interpreted Observations into per-analyte time series.

Reported live 2026-08-04: "give me a timeline of my cholesterol results"
produced eight tool calls and then an error. Prose is the wrong shape for the
question — four numbers across four dates is a picture — so the chat answers
it with a chart, and this builds the series behind it.

Input is the engine's `Observation/$interpret` response, so every reading has
already been redacted, audited, tenant-scoped, and interpreted against the
reference ranges in `r6/labs/interpret.py`. Nothing here re-derives a clinical
verdict; `flag` is carried through verbatim.

## The duplication, named

`templates/mcp_apps/lab_trends.html` groups the same way in JavaScript,
because it talks to the engine directly from a browser and cannot import this.
Two implementations of one concept is a drift risk, so the ANALYTES table is
pinned across both by `tests/test_labs_timeline.py` — if either side gains or
loses a code, that test fails. A shared constant we cannot share in code is at
least a shared constant we refuse to let diverge.
"""

from __future__ import annotations

import datetime as _dt
import re

LOINC = "http://loinc.org"

# An analyte is a SET of codes. The same test arrives under different LOINCs
# from different labs, and plotting only one of them draws a confident line
# through part of the data — the shape of #343, one level up. Order is display
# order.
ANALYTES = [
    {"key": "total-cholesterol", "name": "Total cholesterol",
     "codes": ["2093-3"], "unit": "mg/dL"},
    {"key": "ldl", "name": "LDL cholesterol", "codes": ["13457-7", "18262-6"],
     "unit": "mg/dL"},
    {"key": "hdl", "name": "HDL cholesterol", "codes": ["2085-9"], "unit": "mg/dL"},
    {"key": "triglycerides", "name": "Triglycerides", "codes": ["2571-8"],
     "unit": "mg/dL"},
    {"key": "a1c", "name": "Hemoglobin A1c", "codes": ["4548-4", "17856-6"],
     "unit": "%"},
    {"key": "glucose", "name": "Glucose", "codes": ["2345-7"], "unit": "mg/dL"},
]

#: Blood pressure, charted as its two parts. A reading arrives either as a
#: panel whose components carry the parts, or as one Observation per part.
#: Kept out of ANALYTES because the MCP App reads only top-level values and
#: cannot draw a panel; pinning BP there would claim a parity that is not
#: real (tests/test_labs_timeline.py).
BP_PANELS = frozenset({"85354-9", "55284-4"})
BP_PARTS = [
    {"key": "systolic", "name": "Systolic blood pressure",
     "codes": ["8480-6"], "unit": "mm[Hg]"},
    {"key": "diastolic", "name": "Diastolic blood pressure",
     "codes": ["8462-4"], "unit": "mm[Hg]"},
]

#: Each analyte's usual UCUM unit, for when a reading's own coded unit is
#: missing. Never the reading's free-text `unit`.
KNOWN_UNITS = {a["key"]: a["unit"] for a in ANALYTES + BP_PARTS}

#: Units a lab reading may carry to the model or the browser: exact,
#: case-sensitive tokens. A closed list: any other string, however
#: unit-shaped, could be a word (R875-3). The same list the visit brief keeps
#: (r6/brief/engine.py); CareAgents imports nothing from the engine, so it is
#: repeated. It lives here rather than in agent.py because the chart's series
#: are built here and agent.py imports this module, not the other way round.
UCUM_ALLOWED = frozenset({
    "mg/dL", "g/dL", "ng/mL", "pg/mL", "ng/dL", "ug/dL", "mEq/L",
    "mmol/L", "umol/L", "µmol/L", "nmol/L", "pmol/L", "mmol/mol",
    "g/L", "mg/L", "ug/L", "10*9/L", "10^9/L", "10*12/L", "10^12/L",
    "%", "U/L", "[IU]/L", "IU/L", "mm[Hg]", "mmHg",
    "mL/min/{1.73_m2}", "mL/min/1.73m2", "10*3/uL", "10^3/uL",
    "10*6/uL", "10^6/uL", "fL", "pg", "mg/mmol", "mg/g", "[pH]", "pH",
    # Common lab units real feeds send, found refused by #922 QA (F3).
    "u[IU]/mL", "m[IU]/mL", "m[IU]/L", "k[IU]/L", "[iU]/L", "mIU/mL",
    "uIU/mL", "mIU/L", "meq/L", "mosm/kg", "mOsm/kg", "{INR}", "INR",
    "{ratio}", "{score}", "{titer}", "{index}", "{copies}/mL",
    "/[HPF]", "/[LPF]", "s", "ms", "mm/h", "mm/[Hg]", "cm[H2O]",
    "mg/(24.h)", "ng/mL{FEU}", "mg/g{creat}", "K/uL", "x10E3/uL",
    "x10E6/uL", "fl",
})


def coded_unit(reading: dict, key: str | None,
               allowed: frozenset = UCUM_ALLOWED) -> str | None:
    """The unit the reading stated, or None when it stated one we do not
    recognise; then its number is not passed on either (#884 QA F1).

    - its code (any system, or none) or its unit string is an exact
      allowlist token: that token. Only a token from the list ever leaves.
    - neither is present: the analyte's known unit, else "".
    - anything else: None. The analyte's usual unit used to stand in, which
      put a number beside a unit it was not measured in.
    """
    stated = [v for v in (reading.get("code"), reading.get("unit"))
              if v is not None and v != ""]
    for value in stated:
        if isinstance(value, str) and value in allowed:
            return value
    if stated:
        return None
    return KNOWN_UNITS.get(key, "")

# Free-text search terms -> analyte keys, so "how's my cholesterol?" narrows to
# the lipid panel instead of dumping every series into the chat.
_TOPICS = {
    "cholesterol": ["total-cholesterol", "ldl", "hdl", "triglycerides"],
    "lipid": ["total-cholesterol", "ldl", "hdl", "triglycerides"],
    "ldl": ["ldl"],
    "hdl": ["hdl"],
    "triglyceride": ["triglycerides"],
    "a1c": ["a1c"],
    "hemoglobin a1c": ["a1c"],
    "diabetes": ["a1c", "glucose"],
    "glucose": ["glucose"],
    "sugar": ["glucose", "a1c"],
    "blood pressure": ["systolic", "diastolic"],
    "systolic": ["systolic"],
    "diastolic": ["diastolic"],
}


def keys_for_topic(topic: str | None) -> list[str] | None:
    """Analyte keys a free-text topic names, or None for 'everything'.

    None is deliberately distinct from []: "no topic given" means show what
    there is, while "a topic that matches nothing" must not silently widen
    into every series the person has.
    """
    if not topic:
        return None
    needle = str(topic).strip().lower()
    if not needle:
        return None
    hits: list[str] = []
    for term, keys in _TOPICS.items():
        if term in needle:
            for key in keys:
                if key not in hits:
                    hits.append(key)
    return hits


def _dicts(value: object) -> list[dict]:
    """The objects in a list, or none. A feed can put any JSON where FHIR
    says list-of-objects; that row is skipped, never raised on (#1021)."""
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, dict)]


def _loinc_of(resource: dict) -> str | None:
    code = resource.get("code")
    if not isinstance(code, dict):
        return None
    for coding in _dicts(code.get("coding")):
        if coding.get("system") == LOINC and coding.get("code"):
            return str(coding["code"])
    return None


#: v3 ObservationInterpretation flags a chart may show.
_FLAGS = frozenset({"N", "L", "H", "LL", "HH", "A", "AA"})


def _flag_of(resource: dict) -> str:
    """The ENGINE's interpretation code, or IND when it did not assign one.

    Never computed here. A second opinion on a reference range is how a
    patient ends up seeing a different verdict from the one we audited.
    """
    for concept in _dicts(resource.get("interpretation")):
        for coding in _dicts(concept.get("coding")):
            if coding.get("code"):
                # An unscored reading keeps the feed's own interpretation,
                # whose code can be any text (#922): only a flag passes.
                code = coding["code"]
                return code if isinstance(code, str) and code in _FLAGS \
                    else "IND"
    return "IND"


_DATE_PREFIX = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")


def parse_date(raw: object) -> str:
    """The YYYY-MM-DD a FHIR date or dateTime starts with, checked as a real
    calendar date, or "". Never a cut string: upstream can put anything in
    the field, and a cut string would pass it on as a date (R875-4)."""
    m = _DATE_PREFIX.match(raw) if isinstance(raw, str) else None
    if not m:
        return ""
    try:
        return _dt.date(*(int(g) for g in m.groups())).isoformat()
    except ValueError:
        return ""


def _date_of(resource: dict) -> str:
    return parse_date(resource.get("effectiveDateTime")
                      or resource.get("issued"))


def build_series(interpret_bundle: dict,
                 keys: list[str] | None = None,
                 keep_withheld: bool = False) -> list[dict]:
    """Per-analyte series from an $interpret return Bundle, oldest first.

    Only analytes with at least one numeric reading appear: an empty panel
    tells the person nothing and invites the model to narrate an absence it
    cannot support.

    keep_withheld: also keep an analyte whose every reading was dropped for
    an unrecognised unit, with no readings and a `withheld` count. The text
    surface needs it so the model says the readings exist but cannot be
    shown, instead of saying there are none (#884 QA F1). The chart never
    asks for it: it has nothing to draw.
    """
    entries = (interpret_bundle or {}).get("entry") or []
    wanted = None if keys is None else set(keys)

    series = []
    for analyte in ANALYTES + BP_PARTS:
        if wanted is not None and analyte["key"] not in wanted:
            continue
        codes = set(analyte["codes"])
        readings = []
        withheld = 0
        for entry in entries:
            resource = (entry or {}).get("resource") or {}
            # The reading's own values, or, inside a BP panel, the
            # component that is this part. The date is the panel's.
            if _loinc_of(resource) in codes:
                source = resource
            elif _loinc_of(resource) in BP_PANELS:
                source = next(
                    (c for c in _dicts(resource.get("component"))
                     if _loinc_of(c) in codes), None)
                if source is None:
                    continue
            else:
                continue
            quantity = source.get("valueQuantity") or {}
            if not isinstance(quantity, dict):
                continue
            value = quantity.get("value")
            # No number, no point. Defaulting a missing value to 0 would put
            # a clinical claim nobody made onto a chart.
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                continue
            # Only an allow-listed unit goes on: the series is the
            # /api/labs/timeline JSON, and upstream can write any text,
            # a name or an instruction, into `unit` or `code`. A unit stated
            # but not recognised drops the reading, as a missing number
            # does: blanking it would let a reader fall back to the usual
            # unit and put the number beside a unit it was not measured in
            # (#884 QA F1). With no unit stated it is the analyte's own.
            unit = coded_unit(quantity, analyte["key"])
            if unit is None:
                withheld += 1
                continue
            readings.append({
                "date": _date_of(resource),
                "value": value,
                "unit": unit,
                # A component's own flag, never its panel's: a panel
                # flagged high says nothing about which part was.
                "flag": _flag_of(source),
            })
        if not readings and not (keep_withheld and withheld):
            continue
        readings.sort(key=lambda r: r["date"])
        one = {
            "key": analyte["key"],
            "name": analyte["name"],
            "unit": next((r["unit"] for r in readings if r["unit"]), ""),
            "readings": readings,
            # One reading has no direction. The surface must not draw a line
            # through it, and the model must not narrate a trend from it.
            "trend_plottable": len([r for r in readings if r["date"]]) >= 2,
        }
        if keep_withheld and withheld:
            one["withheld"] = withheld
        series.append(one)
    return series
