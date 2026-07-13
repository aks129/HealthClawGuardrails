"""Prescription transfer request — phase 1 (guardrailed phone call).

A transfer is patient-initiated at the RECEIVING pharmacy: the agent
assembles the transfer package from the record and places ONE human-confirmed
call through the existing action layer. Schedule II is never transferable —
the builder refuses those with an explanation (conservative keyword
deny-list, documented as not authoritative).
"""

import json

from r6.actions.rx_transfer import build_transfer_request, SCHEDULE_II_TERMS


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
