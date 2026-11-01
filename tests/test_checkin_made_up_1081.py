"""Made-up records are labelled made up on the check-in review and on the
sample clinic's result (#1081).

Whose records answered is the server's word, never the records' own (#1086
security F1: a feed can stamp HTEST on a person's real records). The relay
sets `X-Checkin-Records: made-up` only when it answered from the account's
own made-up test patient; the wallet reads only that, and tells our own
sample clinic through a fixed message on the answer's status rows. Both
pages label the records and drop "Your" from what they call them, on a
return visit too, where no offer screen comes first. Through the real
engine: tests/test_sec_1086_made_up_label_exploits.py. The 375px view:
e2e/tests/careagents-checkin-sample.spec.ts.

Synthetic data only.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_checkin_wallet_test_patient import (  # noqa: F401  (fixture)
    RELAY, tester)

pytestmark = pytest.mark.extension

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "careagents" / "static"
TEMPLATES = ROOT / "careagents" / "templates"
HEADER = "X-Checkin-Records"
TYPES = {"resource_types": ["Condition"]}


# --- the relay says it, from the connection it chose ------------------------

def test_the_test_patient_answer_carries_the_header(tester):  # noqa: F811
    browser, hc, svc, acct = tester
    assert browser.post("/checkin/wallet/test-patient", json={}).status_code == 200
    r = browser.post(RELAY, json=TYPES)
    assert r.status_code == 200
    assert r.headers.get(HEADER) == "made-up"


def test_own_records_never_carry_it(tester):  # noqa: F811
    browser, hc, svc, acct = tester
    svc.add_connection(acct.id, "direct", "ca-own-1086", "Upload")
    r = browser.post(RELAY, json=TYPES)
    assert r.status_code == 200
    assert HEADER not in r.headers


def test_a_real_connection_beside_the_test_patient_wins_without_it(tester):  # noqa: F811
    browser, hc, svc, acct = tester
    assert browser.post("/checkin/wallet/test-patient", json={}).status_code == 200
    svc.add_connection(acct.id, "direct", "ca-own-1086b", "Upload")
    r = browser.post(RELAY, json=TYPES)
    assert r.status_code == 200
    assert hc.exports[-1] == "ca-own-1086b"
    assert HEADER not in r.headers


def test_a_failed_look_after_the_export_omits_the_header(
        tester, monkeypatch, caplog):  # noqa: F811
    # The account read that decides the header fails after a good export:
    # the answer still goes, without the header (it fails to unlabelled),
    # not a 500, and the log says so in a fixed, greppable line.
    browser, hc, svc, acct = tester
    assert browser.post("/checkin/wallet/test-patient", json={}).status_code == 200
    before, real = len(hc.exports), svc.list_home

    def flaky(account_id):
        if len(hc.exports) > before:
            raise RuntimeError("database unavailable")
        return real(account_id)

    monkeypatch.setattr(svc, "list_home", flaky)
    caplog.set_level(logging.WARNING, logger="careagents.checkin_wallet")
    r = browser.post(RELAY, json=TYPES)
    assert len(hc.exports) == before + 1
    assert r.status_code == 200
    assert HEADER not in r.headers
    assert "checkin_made_up_header_omitted (RuntimeError)" in caplog.text
    assert "database unavailable" not in caplog.text


def test_a_refusal_carries_no_header(tester):  # noqa: F811
    browser, *_ = tester
    r = browser.post(RELAY, json=TYPES)
    assert r.status_code == 409
    assert HEADER not in r.headers


# --- the words -------------------------------------------------------------

_RUN = """
const m = await import(process.argv[1]);
const cases = JSON.parse(process.argv[2]);
console.log(JSON.stringify({
  unowned: cases.lines.map((l) => m.unowned(l)),
  said: cases.items.map((i) => m.saidMadeUp(i)),
  message: m.MADE_UP_MESSAGE,
  contentReader: typeof m.madeUp,
}));
"""


def _node(lines=(), items=()):
    node = shutil.which("node")
    if not node:
        # A skip would read as green in CI (#1086 review): there, fail.
        if os.environ.get("CI"):
            pytest.fail("node is required in CI for this test")
        pytest.skip("node is not installed")
    out = subprocess.run(
        [node, "--input-type=module", "-e", _RUN,
         str(STATIC / "checkin_answer.js"),
         json.dumps({"lines": list(lines), "items": list(items)})],
        capture_output=True, text=True, check=True, timeout=30)
    return json.loads(out.stdout)


def test_made_up_records_are_never_called_yours():
    out = _node(lines=["Your medicines", "Your health problems",
                       "Your name, birth date and contact details",
                       "Lisinopril 10 mg", "Yours", ""])
    assert out["unowned"] == ["Medicines", "Health problems",
                              "Name, birth date and contact details",
                              "Lisinopril 10 mg", "Yours", ""]


def test_the_clinic_reads_only_our_wallets_message():
    msg = _node()["message"]
    out = _node(items=[
        [{"id": "a", "status": "fulfilled", "message": msg}],
        [{"id": "a", "status": "fulfilled"}],
        [{"id": "a", "status": "declined", "message": "something else"}],
        [], None, [None, "x"]])
    assert out["said"] == [True, False, False, False, False, False]
    # No reader of record content is left (#1086 security F1).
    assert out["contentReader"] == "undefined"


def test_only_our_own_clinic_is_told():
    js = _code(STATIC / "checkin_wallet.js")
    assert ("if (made && origin === location.origin) {\n"
            "        response.requestStatus.forEach((row) => { row.message = "
            "MADE_UP_MESSAGE; });") in js
    assert "made: relaySaysMadeUp(res.headers) };" in js
    assert "HTEST" not in js
    assert "HTEST" not in _code(STATIC / "checkin_sample.js")


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
    assert js.count("review(request, origin, matches, using, made);") == 2
    assert "let made = records.made === true;" in js
    assert "made = got.records.made === true;" in js
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
    assert "You didn't use the made-up records, so no records were sent." in offer


def test_the_review_heading_claims_nothing_for_made_up_records():
    js = _code(STATIC / "checkin_wallet.js")
    assert ('$("wallet-review-title").textContent = made ? MADE_UP_TITLE '
            ": OWN_TITLE;") in js
    title = re.search(r'const MADE_UP_TITLE = "([^"]*)";', js).group(1)
    assert "made-up patient" in title and "your" not in title.lower()
    # The person's own records keep the page's own heading.
    own = re.search(r'const OWN_TITLE = "([^"]*)";', js).group(1)
    assert own in (TEMPLATES / "checkin_wallet.html").read_text()


def test_the_clinic_never_says_your_records_of_made_up_ones():
    js = _code(STATIC / "checkin_sample.js")
    assert ('const whose = (line) => (made ? line.replace("your records", '
            '"the made-up records") : line);') in js
    # Every "your records" line the result draws goes through it.
    assert 'whose("Not answered from your records.' in js
    assert js.count("whose(said(") == 2
