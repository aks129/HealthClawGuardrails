"""The Schedule II check on medication orders, and the builder that is gone.

The module used to build a prescription-transfer call script; that builder
and its route are retired (tests/actions/test_free_text_calls_cannot_dial.py
holds the 410). What stays is the refusal: Schedule II is never
transferable, and the check reads every name the order carries, so the
feed's word for the drug cannot hide it (conservative keyword deny-list,
documented as not authoritative).
"""

import ast
from pathlib import Path

from r6.actions import rx_transfer
from r6.actions.rx_transfer import (SCHEDULE_II_RXCUI, SCHEDULE_II_TERMS,
                                    _is_schedule_ii, medication_names)

RXNORM = "http://www.nlm.nih.gov/research/umls/rxnorm"


def _med(text):
    return {"resourceType": "MedicationRequest", "status": "active",
            "intent": "order",
            "medicationCodeableConcept": {"text": text}}


def _coded(code, text=None, display=None, system=RXNORM):
    coding = {"system": system, "code": code}
    if display:
        coding["display"] = display
    concept = {"coding": [coding]}
    if text:
        concept["text"] = text
    return {"resourceType": "MedicationRequest", "status": "active",
            "intent": "order", "medicationCodeableConcept": concept}


def _refused(med):
    return _is_schedule_ii(med, medication_names(med))


def _name_uses(tree):
    """Every place a module's source mentions a name: loads, attribute
    reads and `from x import name`."""
    uses = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            uses.append(node.id)
        elif isinstance(node, ast.Attribute):
            uses.append(node.attr)
        elif isinstance(node, ast.ImportFrom):
            uses.extend(alias.name for alias in node.names)
    return uses


def test_the_feeds_names_reach_nothing_but_the_schedule_ii_check():
    """The script builder said a medicine as "<label> (recorded as: <feed
    text>)", and as the feed text alone when the code had no label. Feed
    text is where real feeds put a patient's name.

    What this proves: the one function that reads an order's `text` and
    `display` (`_feed_names`) is called only by `medication_names`, and
    nothing in r6/ uses `medication_names`, so the names it collects cannot
    reach an action payload, a script or a page by this module. It also
    pins that the three deleted builder functions stay deleted.

    What it does not prove: that nobody writes a second reader of `text`
    or `display` under another name somewhere else. No test here can; that
    is the redaction rule's job (labels come from r6/terminology.py, after
    apply_redaction) and a reviewer's.

    MUTATION: add a function to r6/actions/rx_transfer.py that returns
    `_feed_names(med)[0]`, or import `medication_names` into
    r6/actions/routes.py -> red.
    """
    for gone in ("display_name", "build_transfer_request", "_call_script"):
        assert not hasattr(rx_transfer, gone), gone

    module = ast.parse(Path(rx_transfer.__file__).read_text())
    callers = [fn.name for fn in module.body
               if isinstance(fn, ast.FunctionDef) and fn.name != "_feed_names"
               and "_feed_names" in _name_uses(fn)]
    assert callers == ["medication_names"], callers
    readers = [fn.name for fn in module.body
               if isinstance(fn, ast.FunctionDef)
               and "medication_names" in _name_uses(fn)]
    assert readers == [], readers

    root = Path(rx_transfer.__file__).resolve().parents[1]
    scanned, outside = 0, []
    for path in sorted(root.rglob("*.py")):
        if path == Path(rx_transfer.__file__).resolve():
            continue
        scanned += 1
        uses = _name_uses(ast.parse(path.read_text()))
        if {"medication_names", "_feed_names"} & set(uses):
            outside.append(str(path.relative_to(root)))
    assert scanned > 50, "the scan walked %d modules; it is broken" % scanned
    assert outside == [], outside


class TestScheduleIIByName:
    def test_a_named_schedule_ii_order_is_refused(self):
        assert _refused(_med("Oxycodone 5 mg tablet"))
        assert _refused(_med("Adderall XR 20 mg"))

    def test_an_ordinary_order_is_not(self):
        assert not _refused(_med("Atorvastatin 20 mg tablet"))

    def test_deny_list_is_lowercase(self):
        assert all(t == t.lower() for t in SCHEDULE_II_TERMS)


class TestScheduleIIIsNotKeyedOnFeedText:
    """The refusal used to read `text` first and stop there (#727).

    RxNorm 7804 is oxycodone. A feed that says "pain reliever" in `text`
    passed; so did a code-only order, which is what a redacted read yields
    for a code the label table does not know.
    """

    def test_the_feeds_word_for_the_drug_does_not_override_its_display(self):
        assert _refused(_coded("7804", text="pain reliever",
                               display="Oxycodone"))

    def test_a_coded_order_with_no_name_is_caught_by_its_code(self):
        assert _refused(_coded("7804"))

    def test_the_code_check_honours_the_oid_form_of_the_system(self):
        assert _refused(_coded("4337",
                               system="urn:oid:2.16.840.1.113883.6.88"))

    def test_an_order_nothing_names_carries_no_name_to_check(self):
        med = _coded("999999")
        assert medication_names(med) == []
        assert not _refused(med)

    def test_our_own_label_names_a_recognised_code_first(self):
        # RxNorm 6809 is Metformin in r6/terminology.py's static table.
        names = medication_names(
            _coded("6809", text="Metformin 500 mg tablet twice daily"))
        assert names == ["Metformin", "Metformin 500 mg tablet twice daily"]

    def test_a_label_that_matches_the_feed_is_not_repeated(self):
        assert medication_names(_coded("6809", text="metformin")) == [
            "Metformin"]

    def test_every_display_is_checked_not_only_the_first(self):
        med = _coded("999999", display="Something else")
        med["medicationCodeableConcept"]["coding"].append(
            {"system": "http://example.org/local", "code": "x",
             "display": "Hydromorphone 2 mg"})
        assert _refused(med)


def test_every_schedule_ii_term_with_an_ingredient_has_a_code():
    """The code set and the term list describe the same substances. A term
    with no ingredient code is a gap a coded, unnamed order walks through;
    brand and product terms (Percocet, Adderall) are name-only by design."""
    ingredients = {
        "oxycodone": "7804", "hydrocodone": "5489", "fentanyl": "4337",
        "morphine": "7052", "hydromorphone": "3423", "oxymorphone": "7814",
        "methadone": "6813", "meperidine": "6754", "codeine": "2670",
        "amphetamine": "725", "dextroamphetamine": "3288",
        "methylphenidate": "6901", "dexmethylphenidate": "352372",
        "lisdexamfetamine": "700810", "tapentadol": "787390",
        "pentobarbital": "8004", "secobarbital": "9624", "cocaine": "2653",
    }
    # Codeine is code-only on purpose: the single-entity ingredient is
    # Schedule II, but the word appears on Schedule III-V combination
    # products (acetaminophen/codeine) that are transferable, so a keyword
    # would refuse the common case. The ingredient RxCUI never appears on a
    # combination product's coding, so the code catches only the II case.
    code_only = {"codeine"}
    for term, code in ingredients.items():
        assert term in SCHEDULE_II_TERMS or term in code_only, term
        assert code in SCHEDULE_II_RXCUI, (term, code)
    assert "700449" not in SCHEDULE_II_RXCUI, "the code that resolved to nothing"
