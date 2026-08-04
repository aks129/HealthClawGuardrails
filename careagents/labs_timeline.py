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

LOINC = "http://loinc.org"

# An analyte is a SET of codes. The same test arrives under different LOINCs
# from different labs, and plotting only one of them draws a confident line
# through part of the data — the shape of #343, one level up. Order is display
# order.
ANALYTES = [
    {"key": "total-cholesterol", "name": "Total cholesterol",
     "codes": ["2093-3"]},
    {"key": "ldl", "name": "LDL cholesterol", "codes": ["13457-7", "18262-6"]},
    {"key": "hdl", "name": "HDL cholesterol", "codes": ["2085-9"]},
    {"key": "triglycerides", "name": "Triglycerides", "codes": ["2571-8"]},
    {"key": "a1c", "name": "Hemoglobin A1c", "codes": ["4548-4", "17856-6"]},
    {"key": "glucose", "name": "Glucose", "codes": ["2345-7"]},
]

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


def _loinc_of(resource: dict) -> str | None:
    for coding in ((resource.get("code") or {}).get("coding") or []):
        if isinstance(coding, dict) and coding.get("system") == LOINC \
                and coding.get("code"):
            return str(coding["code"])
    return None


def _flag_of(resource: dict) -> str:
    """The ENGINE's interpretation code, or IND when it did not assign one.

    Never computed here. A second opinion on a reference range is how a
    patient ends up seeing a different verdict from the one we audited.
    """
    for concept in (resource.get("interpretation") or []):
        for coding in ((concept or {}).get("coding") or []):
            if isinstance(coding, dict) and coding.get("code"):
                return str(coding["code"])
    return "IND"


def _date_of(resource: dict) -> str:
    raw = resource.get("effectiveDateTime") or resource.get("issued") or ""
    return str(raw)[:10]


def build_series(interpret_bundle: dict,
                 keys: list[str] | None = None) -> list[dict]:
    """Per-analyte series from an $interpret return Bundle, oldest first.

    Only analytes with at least one numeric reading appear: an empty panel
    tells the person nothing and invites the model to narrate an absence it
    cannot support.
    """
    entries = (interpret_bundle or {}).get("entry") or []
    wanted = None if keys is None else set(keys)

    series = []
    for analyte in ANALYTES:
        if wanted is not None and analyte["key"] not in wanted:
            continue
        codes = set(analyte["codes"])
        readings = []
        for entry in entries:
            resource = (entry or {}).get("resource") or {}
            if _loinc_of(resource) not in codes:
                continue
            quantity = resource.get("valueQuantity") or {}
            value = quantity.get("value")
            # No number, no point. Defaulting a missing value to 0 would put
            # a clinical claim nobody made onto a chart.
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                continue
            readings.append({
                "date": _date_of(resource),
                "value": value,
                "unit": quantity.get("unit") or "",
                "flag": _flag_of(resource),
            })
        if not readings:
            continue
        readings.sort(key=lambda r: r["date"])
        series.append({
            "key": analyte["key"],
            "name": analyte["name"],
            "unit": next((r["unit"] for r in readings if r["unit"]), ""),
            "readings": readings,
            # One reading has no direction. The surface must not draw a line
            # through it, and the model must not narrate a trend from it.
            "trend_plottable": len([r for r in readings if r["date"]]) >= 2,
        })
    return series
