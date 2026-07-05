"""Self-conformance endpoint — grade the running deployment in one call.

GET /r6/fhir/$conformance runs the guardrail conformance harness against THIS
app in-process (through the full guardrail stack) and returns the scorecard.
Writes land in a dedicated `conformance-selftest` tenant so a caller's data is
never touched. `?format=text` returns the human scorecard; default is JSON.
Returns 200 at Grade A, 503 otherwise (so an uptime check can watch it).
"""

import logging

from flask import Response, current_app, jsonify, request

logger = logging.getLogger(__name__)

_SELFTEST_TENANT = "conformance-selftest"


def register_conformance_routes(blueprint, deps):
    @blueprint.route("/$conformance", methods=["GET"])
    def conformance_selftest():
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
        ctx = ProbeContext(tenant=_SELFTEST_TENANT, step_up_token=token)
        report = run_conformance(client, ctx)
        code = 200 if report.passed else 503

        if request.args.get("format") == "text":
            return Response(report.render(), status=code, mimetype="text/plain")
        return jsonify(report.to_dict()), code
