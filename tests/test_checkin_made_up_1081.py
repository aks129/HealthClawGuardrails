"""Made-up records are labelled made up on the check-in review and on the
sample clinic's result (#1081).

The made-up patient's records each carry HL7's HTEST "test data" marker,
which the engine's check-in export keeps (r6/access.py _test_markers).
careagents/static/checkin_answer.js madeUp() reads it, under node here;
both pages label the records and drop "Your" from what they call them,
on a return visit too, where no offer screen comes first. The 375px view:
e2e/tests/careagents-checkin-sample.spec.ts.

Synthetic data only.
"""

from __future__ import annotations

import copy
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.extension

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "careagents" / "static"
TEMPLATES = ROOT / "careagents" / "templates"
EXPORT = json.loads((ROOT / "e2e" / "fixtures" /
                     "checkin-export-test-patient.json").read_text())
HTEST = {"system": "http://terminology.hl7.org/CodeSystem/v3-ActReason",
         "code": "HTEST"}

_RUN = """
const m = await import(process.argv[1]);
const cases = JSON.parse(process.argv[2]);
console.log(JSON.stringify({
  madeUp: cases.entries.map((e) => m.madeUp(e)),
  unowned: cases.lines.map((l) => m.unowned(l)),
  prepared: m.madeUp(m.prepareRecords(cases.bundle).entries),
}));
"""


def _node(entries, lines=(), bundle=None):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    out = subprocess.run(
        [node, "--input-type=module", "-e", _RUN,
         str(STATIC / "checkin_answer.js"),
         json.dumps({"entries": entries, "lines": list(lines),
                     "bundle": bundle or EXPORT})],
        capture_output=True, text=True, check=True, timeout=30)
    return json.loads(out.stdout)


def _entries(meta_list):
    return [{"fullUrl": f"urn:x:{i}",
             "resource": {"resourceType": "Condition", "id": str(i),
                          **({"meta": m} if m is not None else {})}}
            for i, m in enumerate(meta_list)]


def test_the_made_up_patients_export_reads_as_made_up():
    out = _node([EXPORT["entry"]])
    assert out["madeUp"] == [True]
    # Through the wallet's own preparation as well.
    assert out["prepared"] is True


def test_real_or_mixed_records_never_read_as_made_up():
    mixed = copy.deepcopy(EXPORT["entry"])
    del mixed[0]["resource"]["meta"]
    out = _node([
        mixed,
        [],                                            # nothing at all
        _entries([None, None]),                        # no marker
        _entries([{"tag": [{**HTEST, "system": "http://example.org"}]}]),
        _entries([{"tag": [{**HTEST, "code": "htest"}]}]),
        _entries([{"tag": {"not": "a list"}}]),
        [None, "x"],
    ])
    assert out["madeUp"] == [False] * 7


def test_the_marker_counts_in_either_slot():
    out = _node([_entries([{"security": [HTEST]}, {"tag": [HTEST]}])])
    assert out["madeUp"] == [True]


def test_made_up_records_are_never_called_yours():
    out = _node([], lines=["Your medicines", "Your health problems",
                           "Your name, birth date and contact details",
                           "Lisinopril 10 mg", "Yours", ""])
    assert out["unowned"] == ["Medicines", "Health problems",
                              "Name, birth date and contact details",
                              "Lisinopril 10 mg", "Yours", ""]


# --- the pages ------------------------------------------------------------

def _code(path):
    """Source without comments."""
    src = path.read_text()
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", src)


def test_the_review_is_labelled_whichever_way_the_records_came():
    js = _code(STATIC / "checkin_wallet.js")
    # The first review (a return visit opens it straight away) and a
    # review after a pick both pass the label's answer.
    assert js.count("review(request, origin, matches, using, madeUp(") == 2
    assert '$("wallet-made-up").hidden = !made;' in js
    assert "made ? unowned(itemHeading(m)) : itemHeading(m)" in js
    assert "made ? recordNames(m).map(unowned) : recordNames(m)" in js
    page = (TEMPLATES / "checkin_wallet.html").read_text()
    label = page[page.index('id="wallet-made-up"'):]
    assert label.index("Made-up") < label.index("</p>")
    assert "Test version." not in re.sub(r"{#.*?#}", "", page, flags=re.S)


def test_the_clinic_result_is_labelled_and_a_decline_says_nothing_came():
    js = _code(STATIC / "checkin_sample.js")
    assert '$("sample-made-up").hidden = !made;' in js
    assert "said no to every item" not in js
    assert "No records or answers came back, so the form is still blank." in js
    page = (TEMPLATES / "checkin_sample.html").read_text()
    assert 'id="sample-made-up" hidden><b>Made-up patient.</b>' in page


def test_declining_the_made_up_records_says_they_were_not_used():
    js = _code(STATIC / "checkin_wallet.js")
    offer = js[js.index("await offerTestPatient("):]
    offer = offer[:offer.index("declineAll(request)")]
    assert "You didn't use the made-up records, so nothing was sent." in offer
