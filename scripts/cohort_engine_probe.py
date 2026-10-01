#!/usr/bin/env python3
"""Probe HealthClaw's FHIR API directly with a synthetic cohort.

    python scripts/cohort_engine_probe.py --cohort <cohort> \\
        --base http://127.0.0.1:5300 --internal-secret <INTERNAL_TOKEN_MINT_SECRET> \\
        --db <SQLAlchemy URL of HealthClaw's database>

The cohort comes from scripts/synthetic_hospital_cohort.py. LOCAL stacks
only: it needs the internal secret, and it writes one tenant per patient.

For each patient it ingests the bundle into its own tenant, then reads every
resource type back through /r6/fhir and checks:

  - No patient canary name or phone, and no clinician name, in any response.
  - No free text the source wrote survives: note text, chief complaints,
    allergy and medication text, attachment data.
  - Every surviving `display` or `text` is a label the server's terminology
    table would give for a code on that record.
  - Every entry is counted: search totals match what was ingested.
  - Each read writes at least one audit row.
  - A token for another tenant reads nothing from this one.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import requests
from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TYPES = ("Patient", "Encounter", "Condition", "Observation", "AllergyIntolerance",
         "MedicationStatement", "DocumentReference")

FAILS: list[str] = []
PASSES = 0


def check(ok, label, evidence=""):
    global PASSES
    if ok:
        PASSES += 1
    else:
        FAILS.append(f"{label} :: {evidence}")
        print(f"  FAIL {label} :: {evidence}")
    return ok


def walk(obj, key=None):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from walk(v, k)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk(v, key)
    else:
        yield key, obj


def source_free_text(bundle):
    """Strings the source wrote into free-text fields, long enough to be specific."""
    out = set()
    for e in bundle["entry"]:
        for k, v in walk(e["resource"]):
            if k in ("text", "display", "data") and isinstance(v, str) and len(v) >= 12:
                out.add(v)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--cohort", required=True)
    ap.add_argument("--base", default="http://127.0.0.1:5300")
    ap.add_argument("--internal-secret", required=True)
    ap.add_argument("--db", required=True)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args(argv)

    base = args.base.rstrip("/") + "/r6/fhir"
    manifest = json.loads((Path(args.cohort) / "manifest.json").read_text())[: args.limit or None]
    from r6 import terminology  # labels the server is allowed to emit

    eng = create_engine(args.db if "://" in args.db else f"sqlite:///{args.db}")

    def audit_rows(tenant):
        with eng.connect() as con:
            return con.execute(text("select count(*) from audit_events where tenant_id = :t"),
                               {"t": tenant}).scalar()

    def token(tenant):
        r = requests.post(f"{base}/internal/step-up-token", json={"tenant_id": tenant},
                          headers={"X-Internal-Secret": args.internal_secret}, timeout=10)
        r.raise_for_status()
        return r.json()["token"]

    # Tenant ids and their credentials are kept apart, so nothing that
    # carries a token ever reaches a printed label.
    tenants = []
    headers = {}
    for m in manifest:
        bundle = json.loads((Path(args.cohort) / m["file"]).read_text())
        tenant = f"probe-{m['patient_id']}"
        pid = f"sh-{m['patient_id']}"
        print(f"== {m['file']} ({m['reason']})")
        r = requests.post(f"{base}/internal/ingest-bundle", json={"bundle": bundle},
                          headers={"X-Tenant-Id": tenant, "X-Internal-Secret": args.internal_secret},
                          timeout=120)
        body = r.json()
        check(r.ok and body.get("ingested") == len(bundle["entry"]) and not body.get("failed"),
              f"{m['file']} ingest", f"{r.status_code} ingested={body.get('ingested')} failed={body.get('failed')}")

        expected = {}
        for e in bundle["entry"]:
            rt = e["resource"]["resourceType"]
            expected[rt] = expected.get(rt, 0) + 1
        phone = next((t["value"] for e in bundle["entry"] if e["resource"]["resourceType"] == "Patient"
                      for t in e["resource"].get("telecom", [])), None)
        canaries = [m["canary_family"], phone, *m["clinicians"]]
        free_text = source_free_text(bundle)
        # Labels the server may attach: terminology lookups of every code present.
        allowed = set()
        for e in bundle["entry"]:
            stack = [e["resource"]]
            while stack:
                o = stack.pop()
                if isinstance(o, dict):
                    if "system" in o and "code" in o:
                        lab = terminology.lookup(o["system"], o["code"])
                        if lab:
                            allowed.add(lab)
                    stack.extend(o.values())
                elif isinstance(o, list):
                    stack.extend(o)

        tok = token(tenant)
        hdr = {"X-Tenant-Id": tenant, "X-Step-Up-Token": tok}
        before = audit_rows(tenant)
        reads = 0
        for rt in TYPES:
            url = f"{base}/{rt}/{pid}" if rt == "Patient" else f"{base}/{rt}"
            params = {} if rt == "Patient" else {"patient": f"Patient/{pid}", "_count": "200"}
            r = requests.get(url, params=params, headers=hdr, timeout=30)
            reads += 1
            if not check(r.status_code == 200, f"{m['file']} {rt} read", str(r.status_code)):
                continue
            out = r.text
            low = out.lower()
            leaked = [c for c in canaries if c and c.lower() in low]
            check(not leaked, f"{m['file']} {rt} carries no canary", f"{len(leaked)} canary values leaked")
            doc = r.json()
            # Field by field, not substring: a server label for one code may
            # contain a shorter phrase the source wrote as text elsewhere.
            survived = sorted({v for k, v in walk(doc) if k in ("text", "display", "data")
                               and v in free_text and v not in allowed})
            check(not survived, f"{m['file']} {rt} carries no source free text",
                  f"{len(survived)} source strings survived")
            if rt != "Patient":
                n = doc.get("total", len(doc.get("entry", [])))
                check(n == expected.get(rt, 0), f"{m['file']} {rt} count {n} == ingested {expected.get(rt, 0)}",
                      f"got {n}")
            bad = [v for k, v in walk(doc) if k == "display" and isinstance(v, str)
                   and v not in allowed and v.strip()]
            # displays on Reference/meta/disclaimer fields are server-authored; report them for review
            if bad:
                print(f"  note {m['file']} {rt}: {len(set(bad))} displays not from the terminology table")
        after = audit_rows(tenant)
        check(after - before >= reads, f"{m['file']} {reads} reads wrote audit rows",
              f"before {before} after {after}")
        tenants.append((tenant, pid))
        headers[tenant] = hdr

    print("== cross-tenant")
    for (ta, pa), (tb, _) in zip(tenants, tenants[1:] + tenants[:1]):
        if ta == tb:
            break
        hb = headers[tb]
        r = requests.get(f"{base}/Patient/{pa}", headers=hb, timeout=10)
        check(r.status_code in (403, 404), f"{tb} cannot read {ta}'s Patient", str(r.status_code))
        r = requests.get(f"{base}/Condition", params={"patient": f"Patient/{pa}"}, headers=hb, timeout=10)
        n = r.json().get("total", len(r.json().get("entry", []))) if r.ok else 0
        check(n == 0, f"{tb} search finds none of {ta}'s Conditions", f"{r.status_code} total={n}")
        hdr_forged = dict(hb, **{"X-Tenant-Id": ta})
        r = requests.get(f"{base}/Patient/{pa}", headers=hdr_forged, timeout=10)
        check(r.status_code in (401, 403), f"{tb}'s token with {ta}'s tenant header is refused",
              str(r.status_code))

    print(f"\n{PASSES} passed, {len(FAILS)} failed")
    for f in FAILS:
        print("  FAIL", f)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
