"""Security re-review of #913 at 3938f4e: every write route, one sweep.

Enumerates every POST/PUT/PATCH/DELETE rule in the engine's URL map and
fires each, with every credential a caller could hold for the tenant (the
internal secret, a write step-up token, X-Human-Confirmed), at a CLOSED
tenant with several generic bodies. Afterwards the tenant's stored records
must be byte-for-byte what they were. Path parameters are filled with a
kept record's own type and id, so update-shaped routes reach a real row.

Breadth, not depth: a route whose handler needs a specific body shape to
reach its write is covered by the targeted probes in
tests/test_sec913_closed_tenant_write_paths.py. Synthetic ids only.
"""


from models import db
from r6.fasten.models import TenantClosure
from r6.models import R6Resource

SECRET = "sec913-sweep-secret"
CLOSED = "sec913-sweep-closed"

_OBS = {"resourceType": "Observation", "status": "final",
        "subject": {"reference": "Patient/p1"},
        "code": {"coding": [{"system": "http://loinc.org",
                             "code": "8867-4"}]},
        "valueQuantity": {"value": 72, "unit": "/min"}}
_BUNDLE = {"resourceType": "Bundle", "type": "collection",
           "entry": [{"resource": {**_OBS, "id": "sweep-obs"}}]}
_BODIES = (
    _OBS,
    _BUNDLE,
    {"bundle": _BUNDLE},
    {"bundle": _BUNDLE, "tenant_id": CLOSED},
    {"tenant_id": CLOSED, "patient_ref": "Patient/p1", "systolic": 120,
     "diastolic": 80, "effective": "2026-10-08T09:00:00Z"},
    {"fixes": [{"field_path": "Observation.status", "new_value": "amended"}],
     "patient_intent": "sweep"},
)


def _snapshot():
    db.session.expire_all()
    return sorted((r.resource_type, r.id, r.resource_json, r.is_deleted)
                  for r in R6Resource.query.filter_by(tenant_id=CLOSED))


#: Deletion is not an arrival: purge empties a closed tenant by design. The
#: revoke route is what closes a tenant; in the control run it would close
#: the open one mid-sweep.
_SKIP = {"/r6/fhir/internal/purge-tenant", "/r6/fhir/internal/fasten-revoke"}


def _write_rules(app):
    for rule in app.url_map.iter_rules():
        if rule.rule in _SKIP:
            continue
        for method in sorted(rule.methods & {"POST", "PUT", "PATCH",
                                             "DELETE"}):
            yield method, rule


def _fill(rule, rid):
    path = rule.rule
    for arg in rule.arguments:
        value = {"resource_type": "Observation",
                 "resource_id": rid}.get(arg, "x")
        path = path.replace(f"<{arg}>", value)
        for conv in ("string", "int", "path"):
            path = path.replace(f"<{conv}:{arg}>", value)
    return path


def _sweep(app, client, monkeypatch, close):
    monkeypatch.setenv("INTERNAL_TOKEN_MINT_SECRET", SECRET)
    from r6.fasten import routes as fasten_routes
    from r6.shc import routes as shc_routes
    from r6.stepup import generate_step_up_token
    # No daemon threads or outbound calls from the sweep.
    monkeypatch.setattr(fasten_routes, "_launch_ingest", lambda *a, **k: None)
    monkeypatch.setattr(shc_routes.threading, "Thread",
                        lambda *a, **k: type("T", (), {"start": lambda s: None,
                                                       "daemon": True})())
    token = generate_step_up_token(CLOSED)
    headers = {"X-Tenant-Id": CLOSED, "X-Step-Up-Token": token,
               "X-Internal-Secret": SECRET, "X-Human-Confirmed": "true",
               "X-Agent-Id": "sweep"}
    kept = client.post("/r6/fhir/Observation", json={**_OBS, "id": "kept-1"},
                       headers=headers)
    assert kept.status_code == 201
    if close:
        db.session.add(TenantClosure(tenant_id=CLOSED))
        db.session.commit()
    before = _snapshot()

    from r6.rate_limit import _rate_limits
    changed = []
    for method, rule in _write_rules(app):
        path = _fill(rule, "kept-1")
        for i, body in enumerate(_BODIES):
            # A sweep is hundreds of requests: without this, 429s would
            # stand in for refusals and the closed run would prove nothing.
            _rate_limits.clear()
            # A fresh entry id per request, so a bundle route that stores it
            # changes the snapshot even after an earlier route stored one.
            fresh = {"resourceType": "Bundle", "type": "collection",
                     "entry": [{"resource": {
                         **_OBS, "id": f"sweep-{i}-{len(changed)}-"
                                       f"{abs(hash(method + rule.rule)) % 10**8}"}}]}
            if body is _BUNDLE:
                body = fresh
            elif "bundle" in body:
                body = {**body, "bundle": fresh}
            r = client.open(path, method=method, json=body, headers=headers)
            assert r.status_code != 429, f"{method} {rule.rule} rate-limited"
            if _snapshot() != before:
                changed.append(f"{method} {rule.rule}")
                before = _snapshot()
                break
    return changed


def test_no_write_route_changes_a_closed_tenants_records(
        app, client, monkeypatch):
    changed = _sweep(app, client, monkeypatch, close=True)
    assert changed == [], changed


def test_control_the_same_sweep_does_write_into_an_open_tenant(
        app, client, monkeypatch):
    """Proves the sweep reaches real writes, so the closed run's empty list
    means refused, not unreached."""
    changed = _sweep(app, client, monkeypatch, close=False)
    for expected in ("POST /r6/fhir/<resource_type>",
                     "POST /r6/fhir/Bundle/$ingest-context",
                     "POST /r6/fhir/internal/ingest-bundle",
                     "POST /r6/smbp/reading"):
        assert expected in changed, changed
