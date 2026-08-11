"""Self-conformance endpoint — grade the running deployment in one call.

GET /r6/fhir/$conformance runs the guardrail conformance harness against THIS
app in-process (through the full guardrail stack) and returns the scorecard.
Writes land in a dedicated `conformance-selftest` tenant so a caller's data is
never touched. Returns 200 at Grade A, 503 otherwise (so an uptime check can
watch it).

Formats: default JSON · `?format=text` human scorecard · `?format=shields`
a shields.io endpoint-badge object (for a live "guardrail conformance: A" badge).
Results are cached in-process (TTL) so badge/monitor traffic doesn't re-run the
harness — and its synthetic writes — on every hit; `?fresh=1` forces a new run.
"""

import logging

from flask import Response, jsonify, request

from r6.conformance.snapshot import HarnessUnavailable, local_report

logger = logging.getLogger(__name__)


def _shields(report_dict):
    p = report_dict["score"]["passed"]
    t = report_dict["score"]["total"]
    grade = report_dict["grade"]
    error_fidelity = next(
        (p for p in report_dict["properties"]
         if p["key"] == "error_fidelity"),
        None,
    )
    fidelity_suffix = ""
    if error_fidelity is not None:
        fidelity_suffix = (
            f"; error fidelity {error_fidelity['grade']}, "
            f"{error_fidelity['coverage']}"
        )
    return {
        "schemaVersion": 1,
        "label": "guardrail conformance",
        "message": f"{grade} ({p}/{t}{fidelity_suffix})",
        "color": "brightgreen" if report_dict["passed"] else
                 ("yellow" if grade in ("B", "C") else "red"),
    }


def register_conformance_routes(blueprint, deps):
    @blueprint.route("/$conformance", methods=["GET"])
    def conformance_selftest():
        fresh = request.args.get("fresh") in ("1", "true", "yes")

        # The run and its cache moved to r6/conformance/snapshot.py so the
        # dashboard renders the same measurement this endpoint serves. Two
        # caches would mean two harness runs, and the harness writes — the
        # page and the badge would disagree about a deployment that changed
        # between them.
        try:
            body = local_report(fresh=fresh)
        except HarnessUnavailable as exc:
            return jsonify({"error": "conformance harness not configured",
                            "detail": exc.detail}), 503

        code = 200 if body["passed"] else 503
        fmt = request.args.get("format")
        if fmt == "shields":
            return jsonify(_shields(body)), 200  # badge always renders (200)
        if fmt == "text":
            from r6.conformance.probes import ConformanceReport, ProbeResult, Check
            # Re-render from the cached dict without re-running probes.
            results = [
                ProbeResult(
                    p["key"], p["property"],
                    # Rehydrate the two halves, not the derived `detail`.
                    # Feeding `detail` back in as the measurement would put a
                    # failure sentence into `observed`, where it prints on a
                    # pass — the exact bug, reintroduced by the cache path.
                    [Check(c["name"], c["passed"], c.get("observed", ""),
                           on_failure=c.get("on_failure", ""))
                     for c in p["checks"]],
                    note=p.get("note", ""),
                    grade=p.get("grade"),
                    coverage=p.get("coverage", "full"),
                    profiles=p.get("profiles", {}),
                )
                for p in body["properties"]
            ]
            rep = ConformanceReport(results, base=body.get("target", ""),
                                    tenant=body.get("tenant", ""))
            return Response(rep.render(), status=code, mimetype="text/plain")
        return jsonify(body), code
