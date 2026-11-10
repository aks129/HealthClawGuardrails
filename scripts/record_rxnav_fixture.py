#!/usr/bin/env python3
"""Record what RxNav says about every RxNorm code in the static label table.

Writes tests/fixtures/rxnav_concepts.json, which
tests/test_terminology_rxnorm_drift.py checks the table against. The test
never uses the network; this script is how the fixture is refreshed after a
row is added.

    uv run python scripts/record_rxnav_fixture.py

Only the code is sent (rxnav.nlm.nih.gov, one request per code). This product
uses publicly available data from the U.S. National Library of Medicine
(NLM), National Institutes of Health, Department of Health and Human
Services; NLM is not responsible for the product and does not endorse or
recommend this or any other product.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from r6.terminology import RXNORM, _LABELS  # noqa: E402

OUT = ROOT / "tests" / "fixtures" / "rxnav_concepts.json"
URL = "https://rxnav.nlm.nih.gov/REST/rxcui/%s/historystatus.json"


def fetch(code: str) -> dict:
    if not code.isdigit() or len(code) > 8:
        raise SystemExit("not an RxCUI: %r" % code)
    with urllib.request.urlopen(URL % code, timeout=30) as resp:
        body = json.load(resp)
    history = body.get("rxcuiStatusHistory") or {}
    meta = history.get("metaData") or {}
    attrs = history.get("attributes") or {}
    features = history.get("definitionalFeatures") or {}
    parts = [
        {
            "ingredient": (p.get("baseName") or "").lower(),
            "numerator": p.get("numeratorValue") or "",
            "unit": p.get("numeratorUnit") or "",
            "denominator": p.get("denominatorValue") or "",
            "denominator_unit": p.get("denominatorUnit") or "",
            "active_ingredient": (p.get("activeIngredientName") or "").lower(),
        }
        for p in features.get("ingredientAndStrength") or []
    ]
    forms = [f.get("doseFormName") or ""
             for f in features.get("doseFormConcept") or []]
    pack = [
        {"rxcui": m.get("packRxcui") or "", "name": m.get("packName") or "",
         "count": m.get("packNumber") or ""}
        for m in (history.get("pack") or {}).get("packConcept") or []
    ]
    return {
        "name": attrs.get("name") or "",
        "tty": attrs.get("tty") or "",
        "status": meta.get("status") or "",
        "ingredients": parts,
        "dose_forms": forms,
        "pack": pack,
    }


def main() -> None:
    codes = sorted((code for (system, code) in _LABELS if system == RXNORM),
                   key=int)
    recorded = {}
    for code in codes:
        recorded[code] = fetch(code)
        time.sleep(0.25)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    # No timestamp in the file: a re-record that changes nothing should
    # produce no diff. The recording date is the commit's.
    OUT.write_text(json.dumps(recorded, indent=2, sort_keys=True) + "\n")
    print("recorded %d concepts to %s" % (len(recorded), OUT.relative_to(ROOT)))


if __name__ == "__main__":
    main()
