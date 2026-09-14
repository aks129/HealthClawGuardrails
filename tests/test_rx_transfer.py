"""Prescription transfer request — phase 1 (guardrailed phone call).

A transfer is patient-initiated at the RECEIVING pharmacy: the agent
assembles the transfer package from the record and places ONE human-confirmed
call through the existing action layer. Schedule II is never transferable —
the builder refuses those with an explanation (conservative keyword
deny-list, documented as not authoritative).
"""

import json

from r6.actions.rx_transfer import (SCHEDULE_II_TERMS, UNVERIFIABLE_REASON,
                                    build_transfer_request)


def _refused_as_schedule_ii(res):
    """The refusal class, not a substring: the unverifiable reason also
    mentions the Schedule II rule, so `"Schedule II" in reason` proves
    nothing about which check fired."""
    reasons = [r["reason"] for r in res["refused"]]
    return (res["allowed"] == [] and len(reasons) == 1
            and reasons[0].startswith("Schedule II medications cannot")
            and reasons[0] != UNVERIFIABLE_REASON)


def _med(text, status="active"):
    return {"resourceType": "MedicationRequest", "status": status,
            "intent": "order",
            "medicationCodeableConcept": {"text": text}}


TO_PHARMACY = {"name": "Walgreens Main St", "phone": "+15551230000"}
FROM_PHARMACY = {"name": "CVS Oak Ave", "phone": "+15559870000"}


class TestBuilder:
    def test_builds_call_with_meds_and_pharmacies(self):
        res = build_transfer_request(
            [_med("Atorvastatin 20 mg tablet"), _med("Lisinopril 10 mg")],
            TO_PHARMACY, from_pharmacy=FROM_PHARMACY)
        assert [m["name"] for m in res["allowed"]] == [
            "Atorvastatin 20 mg tablet", "Lisinopril 10 mg"]
        assert res["refused"] == []
        p = res["action_payload"]
        assert p["phone"] == "+15551230000"
        assert p["to"] == "Walgreens Main St"
        body = p["body"]
        assert "Atorvastatin" in body and "Lisinopril" in body
        assert "CVS Oak Ave" in body

    def test_schedule_ii_refused_with_reason(self):
        res = build_transfer_request(
            [_med("Oxycodone 5 mg tablet"), _med("Atorvastatin 20 mg")],
            TO_PHARMACY)
        assert [m["name"] for m in res["allowed"]] == ["Atorvastatin 20 mg"]
        assert len(res["refused"]) == 1
        refusal = res["refused"][0]
        assert refusal["name"] == "Oxycodone 5 mg tablet"
        assert "Schedule II" in refusal["reason"]
        assert "Oxycodone" not in res["action_payload"]["body"]

    def test_all_schedule_ii_yields_no_action(self):
        res = build_transfer_request([_med("Adderall XR 20 mg")], TO_PHARMACY)
        assert res["allowed"] == []
        assert res["action_payload"] is None

    def test_inactive_meds_excluded(self):
        res = build_transfer_request(
            [_med("Atorvastatin 20 mg", status="stopped"),
             _med("Lisinopril 10 mg")], TO_PHARMACY)
        assert [m["name"] for m in res["allowed"]] == ["Lisinopril 10 mg"]

    def test_deny_list_is_lowercase(self):
        assert all(t == t.lower() for t in SCHEDULE_II_TERMS)


class TestProposeRoute:
    def _propose(self, client, tenant_headers, body):
        return client.post("/r6/actions/rx-transfer/propose",
                           headers={**tenant_headers,
                                    "Content-Type": "application/json"},
                           data=json.dumps(body))

    def _seed_med(self, client, auth_headers, tenant_headers, text):
        med = {**_med(text), "subject": {"reference": "Patient/rx-test-pt"}}
        return client.post(
            "/r6/fhir/MedicationRequest",
            headers={**auth_headers, "X-Human-Confirmed": "true",
                     "Content-Type": "application/fhir+json"},
            data=json.dumps(med))

    def test_propose_creates_pending_action(self, client, auth_headers,
                                            tenant_headers):
        r = self._seed_med(client, auth_headers, tenant_headers,
                           "Metformin 500 mg tablet")
        assert r.status_code == 201, r.get_data(as_text=True)
        resp = self._propose(client, auth_headers, {
            "to_pharmacy": TO_PHARMACY, "from_pharmacy": FROM_PHARMACY})
        assert resp.status_code == 201, resp.get_data(as_text=True)
        body = resp.get_json()
        assert body["action"]["kind"] == "phone-call"
        assert body["action"]["status"] == "proposed"
        assert any("Metformin" in m["name"] for m in body["allowed"])
        # commit (submit-for-confirmation) still requires a step-up token
        commit = client.post(f"/r6/actions/{body['action']['id']}/commit",
                             headers=tenant_headers)
        assert commit.status_code == 401

    def test_missing_pharmacy_400(self, client, tenant_headers):
        assert self._propose(client, tenant_headers, {}).status_code == 400

    def test_no_transferable_meds_422(self, client, auth_headers,
                                      tenant_headers):
        self._seed_med(client, auth_headers, tenant_headers,
                       "Fentanyl patch 25 mcg")
        resp = self._propose(client, tenant_headers,
                             {"to_pharmacy": TO_PHARMACY,
                              "medication_names": ["Fentanyl patch 25 mcg"]})
        assert resp.status_code == 422
        assert resp.get_json()["refused"]


class TestProposeStepUpGate:
    """The step-up gate on rx-transfer propose (kernel slice 5).

    Every assertion here failed to exist before the migration. Mutation
    testing the slice found three gates whose behaviour nothing measured:
    the Bearer fallback, and the write-scope requirement on both propose and
    review. Each mutation left the whole suite green.

    A gate nothing measures is a gate that can be removed by accident, which
    is the defect class this refactor exists to end — so the pins land with
    the migration rather than after it.
    """

    def _seed_med(self, client, auth_headers):
        med = {**_med("Metformin 500 mg tablet"),
               "subject": {"reference": "Patient/rx-test-pt"}}
        return client.post(
            "/r6/fhir/MedicationRequest",
            headers={**auth_headers, "X-Human-Confirmed": "true",
                     "Content-Type": "application/fhir+json"},
            data=json.dumps(med))

    def _body(self):
        return json.dumps({"to_pharmacy": TO_PHARMACY,
                           "from_pharmacy": FROM_PHARMACY})

    def test_a_bearer_token_is_accepted_in_place_of_the_header(
            self, client, auth_headers, tenant_id, step_up_token):
        """The Authorization fallback this endpoint has always had.

        MUTATION: drop also_bearer=True from require_grant -> red.
        """
        assert self._seed_med(client, auth_headers).status_code == 201
        resp = client.post(
            "/r6/actions/rx-transfer/propose",
            headers={"X-Tenant-Id": tenant_id,
                     "Authorization": f"Bearer {step_up_token}",
                     "Content-Type": "application/json"},
            data=self._body())
        assert resp.status_code == 201, resp.get_data(as_text=True)

    def test_a_read_scoped_token_cannot_propose(self, client, auth_headers,
                                                tenant_id):
        """Persisting a ProposedAction is a write.

        The endpoint's own comment says read-scoped credentials may preview
        the refusal response but must not persist. `require_scope` defaulted
        to 'write', so that held by accident; Scope.WRITE now says it.

        MUTATION: scope=Scope.TENANT_BOUND in rx_transfer_propose -> red.
        """
        from r6.stepup import generate_step_up_token

        assert self._seed_med(client, auth_headers).status_code == 201
        resp = client.post(
            "/r6/actions/rx-transfer/propose",
            headers={"X-Tenant-Id": tenant_id,
                     "X-Step-Up-Token": generate_step_up_token(
                         tenant_id, scope="read"),
                     "Content-Type": "application/json"},
            data=self._body())
        assert resp.status_code == 401, resp.get_data(as_text=True)


RXNORM = "http://www.nlm.nih.gov/research/umls/rxnorm"


def _coded(code, text=None, display=None, system=RXNORM):
    coding = {"system": system, "code": code}
    if display:
        coding["display"] = display
    concept = {"coding": [coding]}
    if text:
        concept["text"] = text
    return {"resourceType": "MedicationRequest", "status": "active",
            "intent": "order", "medicationCodeableConcept": concept}


class TestScheduleIIIsNotKeyedOnFeedText:
    """The refusal used to read `text` first and stop there (#727).

    RxNorm 7804 is oxycodone. A feed that says "pain reliever" in `text`
    was proposed for transfer; so was a code-only order, which is what a
    redacted read yields for a code the label table does not know.
    """

    def test_the_feeds_word_for_the_drug_does_not_override_its_display(self):
        res = build_transfer_request(
            [_coded("7804", text="pain reliever", display="Oxycodone")],
            TO_PHARMACY)
        assert _refused_as_schedule_ii(res)

    def test_a_coded_order_with_no_name_is_caught_by_its_code(self):
        res = build_transfer_request([_coded("7804")], TO_PHARMACY)
        assert _refused_as_schedule_ii(res)

    def test_the_code_check_honours_the_oid_form_of_the_system(self):
        res = build_transfer_request(
            [_coded("4337", system="urn:oid:2.16.840.1.113883.6.88")],
            TO_PHARMACY)
        assert _refused_as_schedule_ii(res)

    def test_an_order_nothing_names_is_refused_as_unverifiable(self):
        res = build_transfer_request([_coded("999999")], TO_PHARMACY)
        assert res["allowed"] == []
        assert res["action_payload"] is None
        assert res["refused"][0]["name"] == "unnamed medication"
        assert res["refused"][0]["reason"] == UNVERIFIABLE_REASON

    def test_our_own_label_names_a_recognised_code_first(self):
        # RxNorm 6809 is Metformin in r6/terminology.py's static table; the
        # feed's text is what the pharmacy would have been read otherwise.
        res = build_transfer_request(
            [_coded("6809", text="the little white ones")], TO_PHARMACY)
        assert [m["name"] for m in res["allowed"]] == ["Metformin"]
        assert "Metformin" in res["action_payload"]["body"]

    def test_every_display_is_checked_not_only_the_first(self):
        med = _coded("999999", display="Something else")
        med["medicationCodeableConcept"]["coding"].append(
            {"system": "http://example.org/local", "code": "x",
             "display": "Hydromorphone 2 mg"})
        res = build_transfer_request([med], TO_PHARMACY)
        assert _refused_as_schedule_ii(res)


class TestNameFilterReadsEveryName:
    def _seed(self, client, auth_headers, med):
        med = {**med, "subject": {"reference": "Patient/rx-test-pt"}}
        return client.post(
            "/r6/fhir/MedicationRequest",
            headers={**auth_headers, "X-Human-Confirmed": "true",
                     "Content-Type": "application/fhir+json"},
            data=json.dumps(med))

    def test_medication_names_matches_our_label_for_a_coded_order(
            self, client, auth_headers):
        assert self._seed(client, auth_headers, _coded("6809")).status_code == 201
        resp = client.post("/r6/actions/rx-transfer/propose",
                           headers={**auth_headers,
                                    "Content-Type": "application/json"},
                           data=json.dumps({"to_pharmacy": TO_PHARMACY,
                                            "medication_names": ["metformin"]}))
        assert resp.status_code == 201, resp.get_data(as_text=True)
        assert [m["name"] for m in resp.get_json()["allowed"]] == ["Metformin"]


def test_every_schedule_ii_term_with_an_ingredient_has_a_code():
    """The code set and the term list describe the same substances. A term
    with no ingredient code is a gap a coded, unnamed order walks through;
    brand and product terms (Percocet, Adderall) are name-only by design."""
    from r6.actions.rx_transfer import SCHEDULE_II_RXCUI
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
