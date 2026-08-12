"""GET /r6/smbp/trend — the BP trend chart for one patient.

Read-only, tenant-scoped, audited like every other read. The chart itself is
built in r6/smbp/trend.py; this is the route that selects the Observations
and hands them over.

Registered onto smbp_blueprint from r6/smbp/routes.py so main.py picks it up,
matching the scheduler-routes pattern — and deliberately NOT added to
r6/routes.py, which is the module the architecture ratchet exists to shrink.
"""

from flask import Response, jsonify, request

from r6.access import TenantRejected, TenantSource, tenant_from_request
from r6.audit import record_audit_event
from r6.models import R6Resource
from r6.smbp.trend import render_svg, summarize

_BP_PANEL = "85354-9"

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Blood pressure over time — {label}</title>
<style>
  :root {{
    --paper:#FAF9F5; --ink:#14140F; --dim:#6B6B60; --rule:#DAD8CE;
    --sys:#A82318; --dia:#1B2FBF; --office:#7E5000; --goal:#1B6B45;
  }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--paper); color:var(--ink);
         font:16px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
  main {{ max-width:1040px; margin:0 auto; padding:32px 24px 56px; }}
  h1 {{ font-size:26px; margin:0 0 4px; letter-spacing:-.01em; }}
  .sub {{ color:var(--dim); margin:0 0 24px; font-size:15px; }}
  .card {{ background:#fff; border:1px solid var(--rule); border-radius:2px;
           padding:20px 20px 8px; overflow-x:auto; }}
  .bp-chart {{ width:100%; height:auto; min-width:720px; display:block; }}
  .goal {{ fill:rgba(27,107,69,.08); }}
  .goal-line {{ stroke:var(--goal); stroke-width:1; stroke-dasharray:4 3; }}
  .grid {{ stroke:var(--rule); stroke-width:1; }}
  .axis {{ fill:var(--dim); font-size:11px;
           font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }}
  .line {{ fill:none; stroke-width:1.5; opacity:.55; }}
  .line.sys {{ stroke:var(--sys); }}
  .line.dia {{ stroke:var(--dia); }}
  .pt.home {{ fill:var(--sys); }}
  .pt.office {{ fill:none; stroke:var(--office); stroke-width:1.8; }}
  .empty {{ fill:var(--dim); font-size:14px; }}
  .key {{ display:flex; gap:22px; flex-wrap:wrap; margin:16px 0 0;
          padding:0; list-style:none; color:var(--dim); font-size:14px; }}
  .key b {{ color:var(--ink); font-weight:600; }}
  .counts {{ margin-top:20px; font-size:15px; color:var(--dim); }}
  .counts b {{ color:var(--ink); }}
</style></head>
<body><main>
<h1>Blood pressure over time</h1>
<p class="sub">{label} · {first} to {last}</p>
<div class="card">{svg}
<ul class="key">
  <li><b>Filled dot</b> home reading, measured by the patient</li>
  <li><b>Hollow square</b> clinic reading, taken at a visit</li>
  <li><b>Shaded band</b> home goal, at or below 130/80</li>
</ul>
</div>
<p class="counts"><b>{total}</b> readings · <b>{office}</b> clinic ·
<b>{home}</b> home</p>
</main></body></html>
"""


def register_trend_routes(blueprint, deps):
    operation_outcome = deps["operation_outcome"]
    authenticate_tenant_read = deps["authenticate_tenant_read"]

    @blueprint.route("/trend", methods=["GET"])
    def bp_trend():
        # HEADER then QUERY, through the access kernel. This is a PAGE: a
        # browser opening it cannot set X-Tenant-Id, so a header-only read
        # makes the chart unreachable from the one place it is meant to be
        # looked at. The MCP App pages resolved the same problem the same
        # way (kernel slice 11a); the rest of this blueprint is header-only
        # because the rest of it is an API.
        try:
            tenant = tenant_from_request(
                sources=(TenantSource.HEADER, TenantSource.QUERY))
        except TenantRejected as exc:
            reason = ("X-Tenant-Id header or ?tenant_id= is required"
                      if exc.reason == TenantRejected.ABSENT
                      else "tenant id must match [a-zA-Z0-9_-]{1,64}")
            code = ("security" if exc.reason == TenantRejected.ABSENT
                    else "invalid")
            return jsonify(operation_outcome("error", code, reason)), 400
        tenant_id = tenant.id
        auth_err = authenticate_tenant_read(tenant_id)
        if auth_err is not None:
            return auth_err[0], auth_err[1]

        subject = (request.args.get("subject") or "").strip()
        if not subject:
            return jsonify(operation_outcome(
                "error", "invalid",
                "subject is required, e.g. ?subject=Patient/demo-marisol")), 400
        if not subject.startswith("Patient/"):
            subject = f"Patient/{subject}"

        rows = R6Resource.query.filter_by(resource_type="Observation",
                                          tenant_id=tenant_id,
                                          is_deleted=False).all()
        observations = []
        for row in rows:
            obs = row.to_fhir_json()
            if obs.get("subject", {}).get("reference") != subject:
                continue
            coding = (obs.get("code", {}).get("coding") or [{}])[0]
            if coding.get("code") != _BP_PANEL:
                continue
            observations.append(obs)

        stats = summarize(observations)
        # record_audit_event, NOT the kernel audit(). Two guards said so:
        # audit() flushes without committing, so a read path has to commit
        # its own row — and a GET that commits is what
        # test_no_new_get_route_mutates_the_store exists to stop. Slice 12
        # migrates read audits wholesale; until then this matches every
        # other read route in the blueprint.
        record_audit_event(
            "read", "Observation", subject.split("/")[-1],
            agent_id=request.headers.get("X-Agent-Id"),
            tenant_id=tenant_id,
            detail="bp trend readings=%d" % stats["total"])

        html = _PAGE.format(
            label=subject.split("/")[-1],
            svg=render_svg(observations),
            first=stats["first"] or "—",
            last=stats["last"] or "—",
            total=stats["total"], office=stats["office"], home=stats["home"],
        )
        return Response(html, mimetype="text/html")
