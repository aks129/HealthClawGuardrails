#!/usr/bin/env python3
"""Forms-rail demo smoke test — verify the Aug-18 demo runs without hiccups.

Drives the exact demo path against a live (or local) deployment and asserts
every beat, including the load-bearing safety gate:

    propose form-fill -> commit -> open review -> [allergy attestation gate
    rejects a bare submit] -> honest review -> out-of-band confirm -> execute
    -> download the signed, provenance-stamped PDF (no auth headers).

Exits non-zero on the first failed gate, so it doubles as a pre-webinar check
and a CI/monitor probe.

    python scripts/demo_smoke.py                     # prod, desktop-demo
    python scripts/demo_smoke.py --base http://127.0.0.1:5000 --tenant desktop-demo
"""
from __future__ import annotations

import argparse
import sys

import requests

_GREEN, _RED, _DIM, _RESET = "\033[92m", "\033[91m", "\033[2m", "\033[0m"


def _fail(msg: str) -> None:
    print(f"{_RED}FAIL{_RESET} {msg}")
    sys.exit(1)


def _ok(msg: str) -> None:
    print(f"{_GREEN} OK {_RESET} {msg}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default="https://app.healthclaw.io",
                    help="deployment base URL")
    ap.add_argument("--tenant", default="desktop-demo",
                    help="public demo tenant")
    ap.add_argument("--timeout", type=float, default=20.0)
    args = ap.parse_args()
    base, tenant = args.base.rstrip("/"), args.tenant
    s = requests.Session()
    fhir = f"{base}/r6/fhir"
    actions = f"{base}/r6/actions"
    print(f"{_DIM}Forms-rail demo smoke test → {base} (tenant={tenant}){_RESET}\n")

    # 0. Deployment is up and grading its own guardrails.
    r = s.get(f"{fhir}/health", timeout=args.timeout)
    if r.status_code != 200:
        _fail(f"health {r.status_code}")
    version = r.json().get("version")
    _ok(f"health: {r.json().get('status')} (version {version})")

    # 1. Mint a tenant-bound step-up credential (the human's approval key).
    r = s.post(f"{fhir}/internal/step-up-token", json={"tenant_id": tenant},
               headers={"X-Tenant-Id": tenant}, timeout=args.timeout)
    token = (r.json() or {}).get("token") if r.ok else None
    if not token:
        _fail(f"step-up token mint {r.status_code}: {r.text[:120]}")
    h = {"X-Tenant-Id": tenant, "X-Step-Up-Token": token}
    _ok("minted step-up token")

    # 2. The agent PROPOSES filling the intake — it does not submit it.
    body = {"kind": "form-fill", "payload": {
        "to": "Intake portal", "questionnaire": "healthclaw-intake",
        "body": "new patient intake"}}
    r = s.post(f"{actions}/propose", json=body, headers=h, timeout=args.timeout)
    aid = (r.json() or {}).get("id") if r.ok else None
    if not aid:
        _fail(f"propose {r.status_code}: {r.text[:120]}")
    _ok(f"proposed form-fill action {aid}")

    # 3. Commit only SUBMITS it for out-of-band approval (HTTP 202).
    r = s.post(f"{actions}/{aid}/commit", headers=h, timeout=args.timeout)
    if r.status_code != 202:
        _fail(f"commit expected 202, got {r.status_code}: {r.text[:120]}")
    _ok("committed → awaiting_confirmation (202)")

    # 4. The per-item review page renders.
    r = s.get(f"{actions}/{aid}/review", headers=h, timeout=args.timeout)
    if r.status_code != 200:
        _fail(f"review page {r.status_code}")
    _ok("review page rendered")

    # 5. THE SAFETY BEAT: a submit that attests nothing about allergies (no
    #    confirmed allergy, no explicit "no known allergies") must be rejected
    #    server-side. Silence is never consent; NKA is never inferred.
    over = {f"med-{i}": "yes" for i in range(10)}
    over.update({f"allergy-{i}": "remove" for i in range(10)})  # no nka key
    r = s.post(f"{actions}/{aid}/review", json=over, headers=h,
               timeout=args.timeout)
    if r.status_code != 422:
        _fail(f"allergy-attestation gate should reject with 422, got "
              f"{r.status_code} — the demo's core safety beat is broken")
    _ok("allergy-attestation gate rejects a bare submit (422)")

    # 6. Honest submit: every med decided + explicit "no known allergies".
    honest = {"nka": "true"}
    honest.update({f"med-{i}": "yes" for i in range(10)})
    honest.update({f"allergy-{i}": "confirm" for i in range(10)})
    honest.update({f"condition-{i}": "confirm" for i in range(10)})
    r = s.post(f"{actions}/{aid}/review", json=honest, headers=h,
               timeout=args.timeout)
    if r.status_code != 200:
        _fail(f"honest review submit {r.status_code}: {r.text[:160]}")
    _ok("honest per-item review accepted")

    # 7. Out-of-band confirm EXECUTES: reviewed answers → PDF →
    #    DocumentReference → signed link.
    r = s.post(f"{actions}/{aid}/confirm", headers=h, timeout=args.timeout)
    data = r.json() if r.ok else {}
    import json as _json
    outcome = _json.loads(data.get("outcome_summary") or "{}")
    link = outcome.get("delivery_link")
    if data.get("status") != "completed" or not link:
        _fail(f"confirm/execute status={data.get('status')} link={link} "
              f"({r.status_code}: {r.text[:160]})")
    _ok("confirm → completed; delivery link issued")

    # 8. The shareable artifact downloads with NO auth headers — the signed
    #    URL is the credential — and is a real PDF.
    r = requests.get(link, timeout=args.timeout)
    ctype = r.headers.get("Content-Type", "")
    if r.status_code != 200 or "application/pdf" not in ctype \
            or not r.content.startswith(b"%PDF"):
        _fail(f"PDF download {r.status_code} type={ctype} "
              f"bytes={len(r.content)} — link did not serve a PDF")
    _ok(f"downloaded provenance-stamped PDF ({len(r.content)} bytes)")

    # 9. Tampered signature is refused (the link can't be forged).
    r = requests.get(link[:-4] + "dead", timeout=args.timeout)
    if r.status_code != 403:
        _fail(f"tampered link should be 403, got {r.status_code}")
    _ok("tampered delivery link refused (403)")

    print(f"\n{_GREEN}Forms-rail demo path is healthy — ready to present.{_RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
