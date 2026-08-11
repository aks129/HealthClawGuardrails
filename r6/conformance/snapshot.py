"""One live guardrail measurement, for the page that publishes it.

The public dashboard used to state its security posture in hand-written HTML:
nine rows reading "Tenant Isolation — Enforced", "PHI Redaction — All Reads",
"Audit Trail — Immutable", each with a green check beside it. No test could
fail those rows, because they were prose. They are the shape in
docs/defect-catalogue.md §1 — a reassuring word doing a check's job — shipped
on the most-linked page we have.

This module supplies what replaces them: the report the conformance harness
already produces, in which every claim is a named check that a probe either
observed or did not.

Three outcomes, and the third is the one that matters
-----------------------------------------------------
`guardrail_snapshot` returns a measurement, a measurement fetched from the
stateful host, or an explicit failure to measure. It never returns an empty
report. A template handed `{}` renders a page with no failures on it, which
reads exactly like a clean bill of health — "examined nothing" and "found
nothing" have to look different (docs/defect-catalogue.md §0), so the failure
carries a sentence saying what did not happen and the page prints it where the
grade would have gone.
"""

import logging
import re
import time

from deployment import STATEFUL_HOST, is_read_only

logger = logging.getLogger(__name__)

#: Writes made by the harness land here, never in a caller's tenant.
SELFTEST_TENANT = "conformance-selftest"

#: The harness performs real writes, so badge and page traffic must not re-run
#: it on every hit.
CACHE_TTL_SECONDS = 600

#: A page render waits on this. Long enough for a cold Railway dyno to answer,
#: short enough that a dead upstream does not hold the whole page.
REMOTE_TIMEOUT_SECONDS = 5.0

_cache = {"at": 0.0, "report": None}


class HarnessUnavailable(Exception):
    """The harness could not run here.

    `detail` is one sentence, written to be read by a person and to survive
    being pasted into a bug report. It used to be the bare exception class
    name, which told a page visitor nothing and told an API caller only
    slightly more. A refusal states its reason — the same rule the step-up
    gate follows.
    """

    def __init__(self, detail):
        super().__init__(detail)
        self.detail = detail


def local_report(*, fresh=False):
    """Run the harness in this process, or reuse a recent run.

    Returns the report dict with a `cached` flag. Raises HarnessUnavailable
    when the harness cannot run at all, which is a different thing from a
    deployment that runs it and fails.
    """
    now = time.time()
    cached = _cache["report"]
    if not fresh and cached is not None and now - _cache["at"] < CACHE_TTL_SECONDS:
        return {**cached, "cached": True}

    from flask import current_app

    from r6.conformance import FlaskProbeClient, ProbeContext, run_conformance

    try:
        from r6.stepup import generate_step_up_token
        token = generate_step_up_token(SELFTEST_TENANT)
    except Exception as exc:  # noqa: BLE001 — reported, not swallowed
        logger.error("conformance selftest cannot mint token: %s",
                     type(exc).__name__)
        raise HarnessUnavailable(
            "this deployment cannot mint a step-up token, so the harness "
            f"cannot exercise the write guardrails ({type(exc).__name__}); "
            "STEP_UP_SECRET is usually the missing piece") from exc

    client = FlaskProbeClient(current_app.test_client())
    report = run_conformance(
        client, ProbeContext(tenant=SELFTEST_TENANT, step_up_token=token))
    # Stamp the run. A grade with no time on it cannot be told from a grade
    # earned months ago, and this one is served from a cache and republished
    # on a page — both of which put distance between the measurement and the
    # reader. Consumers that predate this key must still render, so everything
    # downstream treats it as optional.
    _cache["report"] = {
        **report.to_dict(),
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
    }
    _cache["at"] = now
    return {**_cache["report"], "cached": False}


def _looks_like_a_report(body):
    return (isinstance(body, dict)
            and isinstance(body.get("properties"), list)
            and isinstance(body.get("score"), dict))


def remote_report(host, *, timeout=REMOTE_TIMEOUT_SECONDS):
    """Fetch the measurement from the host that can perform it.

    A non-200 status is NOT treated as a failure to measure. The endpoint
    answers 503 precisely when the deployment is below Grade A, so reading the
    status instead of the body would hide the failing grades this page exists
    to show, and would show them as an outage rather than as a result.
    """
    import requests

    url = f"{host}/r6/fhir/$conformance"
    try:
        resp = requests.get(url, timeout=timeout,
                            headers={"Accept": "application/json"})
    except Exception as exc:  # noqa: BLE001 — network, DNS, TLS, timeout
        raise HarnessUnavailable(
            f"could not reach {host} ({type(exc).__name__})") from exc

    try:
        body = resp.json()
    except ValueError as exc:
        raise HarnessUnavailable(
            f"{host} answered {resp.status_code} with a non-JSON body") from exc

    if not _looks_like_a_report(body):
        raise HarnessUnavailable(
            f"{host} answered {resp.status_code} without a conformance report")
    return body


def guardrail_snapshot(config=None, *, fresh=False):
    """The measurement for this deployment, with its provenance.

    Returns a dict the template renders directly:

        measured   bool     — False means say so, loudly
        report     dict|None
        origin     str      — 'this deployment' or the host it came from
        remote     bool
        error      str      — why there is no measurement, when there is none
    """
    read_only = is_read_only(config)
    try:
        if read_only:
            return {"measured": True, "report": remote_report(STATEFUL_HOST),
                    "origin": STATEFUL_HOST, "remote": True, "error": ""}
        return {"measured": True, "report": local_report(fresh=fresh),
                "origin": "this deployment", "remote": False, "error": ""}
    except HarnessUnavailable as exc:
        logger.warning("guardrail snapshot unavailable: %s", exc.detail)
        return {"measured": False, "report": None,
                "origin": STATEFUL_HOST if read_only else "this deployment",
                "remote": read_only, "error": exc.detail}


#: A note holding exactly `ResourceType/id` is provenance — which synthetic
#: resource the probe worked on — not a caveat about coverage.
_REFERENCE = re.compile(r"^[A-Z][A-Za-z]+/[A-Za-z0-9._-]{1,64}$")


def note_kind(note):
    """Classify a property note: 'reference', 'caveat', or ''.

    The harness overloads one field for two unrelated things. phi_redaction
    reports `Patient/d4318e10-…` (the record it probed); human_in_the_loop
    reports "the confirmation header is supplied by the probe: this grades the
    gate, not the human attestation behind it (#214)" (a real limit on what
    the grade means). Rendering both under one heading put a UUID under "Limit
    recorded by the harness" and would have listed it among the scope
    limitations, which is noise where the page can least afford it.
    """
    text = (note or "").strip()
    if not text:
        return ""
    return "reference" if _REFERENCE.match(text) else "caveat"


def summarize(report):
    """The page's view model, derived once here rather than in Jinja.

    `checks_total` counts every named check across every property, which is
    the honest denominator: the score is 7/7 properties, but those seven rest
    on thirty-odd individual observations and the page says so.
    """
    properties = report.get("properties") or []
    checks = [c for p in properties for c in (p.get("checks") or [])]
    rows = [_row(p) for p in properties]
    return {
        "properties_passed": (report.get("score") or {}).get("passed", 0),
        "properties_total": (report.get("score") or {}).get("total", 0),
        "checks_total": len(checks),
        "checks_failed": sum(1 for c in checks if not c.get("passed")),
        "rows": rows,
        "partial_coverage": [r for r in rows if r["partial"]],
        "caveats": [r for r in rows if r["caveat"]],
    }


def _row(prop):
    checks = prop.get("checks") or []
    kind = note_kind(prop.get("note"))
    coverage = prop.get("coverage") or "full"
    return {
        "key": prop.get("key", ""),
        "name": prop.get("property", ""),
        "grade": prop.get("grade"),
        "coverage": coverage,
        "partial": coverage != "full",
        "passed": _passed(prop),
        "checks": checks,
        "checks_failed": sum(1 for c in checks if not c.get("passed")),
        "caveat": prop.get("note", "").strip() if kind == "caveat" else "",
        "reference": prop.get("note", "").strip() if kind == "reference" else "",
    }


def _passed(prop):
    """A property passes when the report says so AND every check agrees.

    The report carries its own per-property `passed`, so this could read that
    field alone. It deliberately does not. The two are computed at different
    times by different code, and if they ever disagree the honest rendering is
    the pessimistic one: a summary flag that outvotes a red probe is how a
    green page outlives a real failure. Taking the AND means a disagreement
    shows up as a failure on the page instead of being averaged away.
    """
    return (bool(prop.get("passed"))
            and all(c.get("passed") for c in (prop.get("checks") or [])))
