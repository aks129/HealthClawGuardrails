"""QA #1030: each bound on a website's inline form, on its own.

Mutation-checking readForm (careagents/static/checkin_answer.js) showed
five of its bounds could be removed with every existing test still green,
because the existing "too large" and "bad options" cases trip some other
bound first. The form is website-supplied content put on our page, so each
bound gets a case that only it refuses:

- the serialised size cap, with every shown text inside its own cap;
- `accept` must include application/fhir+json;
- at least two answer options per question;
- the vendored library's own unsupported list wins over readForm;
- the per-text length cap (500 characters), at the boundary.

Runs the shipped module under node, as tests/test_checkin_wallet_phq2.py
does. Synthetic data only.
"""
from __future__ import annotations

import copy
import json
import shutil
import subprocess

import pytest

from tests.test_checkin_wallet_phq2 import STATIC, _phq2_only, _q

_RUN = """
const m = await import(process.argv[1]);
const request = JSON.parse(process.argv[2]);
const unsupportedItems = JSON.parse(process.argv[3]);
const matches = m.matchItems(request, unsupportedItems, m.prepareRecords({ entry: [] }));
console.log(JSON.stringify(matches.map((x) => ({ form: !!x.form, unsupported: x.unsupported }))));
"""


def _read(request, unsupported_items=()):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    out = subprocess.run(
        [node, "--input-type=module", "-e", _RUN,
         str(STATIC / "checkin_answer.js"), json.dumps(request),
         json.dumps(list(unsupported_items))],
        capture_output=True, text=True, check=True, timeout=30)
    return json.loads(out.stdout)


ANSWERED = [{"form": True, "unsupported": False}]
REFUSED = [{"form": False, "unsupported": True}]


def test_the_published_form_is_answered():
    """The baseline every case below changes one thing from."""
    assert _read(_phq2_only()) == ANSWERED


def test_a_form_over_the_size_cap_is_refused_on_size_alone():
    req = _phq2_only()
    # Every shown text stays short; only a field nobody shows is large.
    _q(req)["description"] = "x" * 40_000
    assert _read(req) == REFUSED


def test_a_form_the_website_will_not_accept_as_fhir_json_is_refused():
    req = _phq2_only()
    req["items"][0]["accept"] = ["application/smart-health-card"]
    assert _read(req) == REFUSED


def test_a_question_with_one_option_is_refused():
    req = _phq2_only()
    question = next(i for i in _q(req)["item"] if i["type"] == "choice")
    question["answerOption"] = question["answerOption"][:1]
    assert _read(req) == REFUSED


def test_the_librarys_own_unsupported_list_wins():
    req = _phq2_only()
    assert _read(req, [{"id": "phq2"}]) == REFUSED


@pytest.mark.parametrize("length,want", [(500, ANSWERED), (501, REFUSED)])
def test_a_question_text_over_500_characters_is_refused(length, want):
    req = copy.deepcopy(_phq2_only())
    question = next(i for i in _q(req)["item"] if i["type"] == "choice")
    question["text"] = "q" * length
    assert _read(req) == want
