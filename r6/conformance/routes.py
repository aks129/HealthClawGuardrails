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
import time

from flask import Response, current_app, jsonify, request

logger = logging.getLogger(__name__)

_SELFTEST_TENANT = "conformance-selftest"
_CACHE_TTL_SECONDS = 600  # 10 minutes
_cache = {"at": 0.0, "report": None}


def _shields(report_dict):
    p = report_dict["score"]["passed"]
    t = report_dict["score"]["total"]
    grade = report_dict["grade"]
    return {
        "schemaVersion": 1,
        "label": "guardrail conformance",
        "message": f"{grade} ({p}/{t})",
        "color": "brightgreen" if report_dict["passed"] else
                 ("yellow" if grade in ("B", "C") else "red"),
    }


def register_conformance_routes(blueprint, deps):
    @blueprint.route("/$conformance", methods=["GET"])
    def conformance_selftest():
        fresh = request.args.get("fresh") in ("1", "true", "yes")
        now = time.time()

        cached_report = _cache["report"]
        if (not fresh and cached_report is not None
                and now - _cache["at"] < _CACHE_TTL_SECONDS):
            body = {**cached_report, "cached": True}
        else:
            from r6.conformance import (
                FlaskProbeClient, ProbeContext, run_conformance,
            )
            try:
                from r6.stepup import generate_step_up_token
                token = generate_step_up_token(_SELFTEST_TENANT)
            except Exception as exc:
                logger.error("conformance selftest cannot mint token: %s",
                             type(exc).__name__)
                return jsonify({"error": "conformance harness not configured",
                                "detail": type(exc).__name__}), 503
            client = FlaskProbeClient(current_app.test_client())
            report = run_conformance(
                client, ProbeContext(tenant=_SELFTEST_TENANT, step_up_token=token))
            _cache["report"] = report.to_dict()
            _cache["at"] = now
            body = {**_cache["report"], "cached": False}

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
                    [Check(c["name"], c["passed"], c["detail"]) for c in p["checks"]],
                    p.get("note", ""))
                for p in body["properties"]
            ]
            rep = ConformanceReport(results, base=body.get("target", ""),
                                    tenant=body.get("tenant", ""))
            return Response(rep.render(), status=code, mimetype="text/plain")
        return jsonify(body), code
