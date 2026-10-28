"""
The optional modules bundled with this repository, as one extension.

The core reaches these modules only through the extension registry
(r6/extensions.py), which loads this module by default when
HEALTHCLAW_EXTENSIONS is unset. Set HEALTHCLAW_EXTENSIONS to the empty
string to run the core without them.

Everything here is registered in the order the core used to wire it, so a
deployment with every module present behaves as it did before the registry.
"""

from __future__ import annotations

import click


def register(registry) -> None:
    """Declare the bundled modules' routes, tables, purge rows and hooks."""
    registry.add_r6_routes(_quality_routes)
    registry.add_r6_routes(_labs_routes,
                           operations=("Observation/$interpret",))
    registry.add_r6_routes(_caregaps_routes,
                           operations=("Patient/$care-gaps",))
    registry.add_r6_routes(_brief_routes)

    # The durable run queue is keyed to the conversation tables (its foreign
    # keys name them), so the two register together.
    registry.add_model_module("r6.agent_runs.models")
    registry.add_model_module("r6.command_center.models")
    registry.add_model_module("r6.smbp.models")

    # Child rows first: an agent run references its conversation message.
    for import_path, attr, label in (
            ("r6.agent_runs.models", "AgentRunEvent", "agent_run_events"),
            ("r6.agent_runs.models", "AgentToolCall", "agent_tool_calls"),
            ("r6.agent_runs.models", "AgentRun", "agent_runs"),
            ("r6.command_center.models", "ConversationMessage", "messages"),
            ("r6.command_center.models", "Conversation", "conversations"),
            ("r6.command_center.models", "AgentTask", "agent_tasks"),
            ("r6.smbp.models", "SMBPSession", "smbp_sessions"),
    ):
        registry.add_purge_model(import_path, attr, label)

    registry.add_app_hook(_register_app)


# --- FHIR blueprint routes -------------------------------------------------

def _quality_routes(blueprint, helpers):
    # $evaluate-measure — NQF 0018
    from r6.quality.routes import register_quality_routes

    register_quality_routes(blueprint, {
        "operation_outcome": helpers["operation_outcome"],
        "authenticate_tenant_read": helpers["authenticate_tenant_read"],
    })


def _labs_routes(blueprint, helpers):
    # Lab reference-range interpreter ($interpret)
    from r6.labs.routes import register_labs_routes

    register_labs_routes(blueprint, {
        "authenticate_tenant_read": helpers["authenticate_tenant_read"],
    })
    _lab_trends_app(blueprint, helpers)


def _caregaps_routes(blueprint, helpers):
    # Preventive-care gaps ($care-gaps)
    from r6.caregaps.routes import register_caregaps_routes

    register_caregaps_routes(blueprint, {
        "authenticate_tenant_read": helpers["authenticate_tenant_read"],
    })
    _care_gaps_app(blueprint, helpers)


def _brief_routes(blueprint, helpers):
    # Appointment Brief ($appointment-brief)
    from r6.brief.routes import register_brief_routes

    register_brief_routes(blueprint, {
        "authenticate_tenant_read": helpers["authenticate_tenant_read"],
    })


def _care_gaps_app(blueprint, helpers):
    from flask import Response, render_template

    from r6.caregaps.report import caller_reasons

    mcp_app_tenant = helpers["mcp_app_tenant"]

    @blueprint.route('/mcp-apps/care-gaps/', methods=['GET'])
    @blueprint.route('/mcp-apps/care-gaps', methods=['GET'])
    def mcp_app_care_gaps():
        """
        MCP App: Preventive Care Gaps.

        Renders the Patient/$care-gaps Parameters response (summary buckets,
        per-rule cards, consumer lines, disclaimer). Linked from the
        `care_gaps` MCP tool via `_meta.ui.resourceUri`. Layout ported from
        SmartHealthConnect's care-gaps view (archived); data path rebuilt on
        the engine's own operation so redaction + audit apply by construction.
        """
        tenant_id = mcp_app_tenant()
        html = render_template(
            'mcp_apps/care_gaps.html',
            tenant_id=tenant_id,
            # The engine's own "nothing was read" list; the page must not copy it (#538).
            not_evaluated_reasons=caller_reasons(),
        )
        resp = Response(html, mimetype='text/html')
        resp.headers['Content-Type'] = 'text/html; profile=mcp-app'
        resp.headers['X-MCP-App'] = 'care-gaps'
        return resp


def _lab_trends_app(blueprint, helpers):
    from flask import Response, render_template

    mcp_app_tenant = helpers["mcp_app_tenant"]

    @blueprint.route('/mcp-apps/lab-trends/', methods=['GET'])
    @blueprint.route('/mcp-apps/lab-trends', methods=['GET'])
    def mcp_app_lab_trends():
        """
        MCP App: Lab Trends.

        A timeline of one analyte over time — the shape "give me a timeline of my
        cholesterol results" actually asks for, and which prose answers badly.
        Linked from the `fhir_interpret_labs` MCP tool via `_meta.ui.resourceUri`.

        Data path is the engine's own Observation/$interpret with an empty body,
        so redaction, audit and tenant scoping apply by construction AND the
        normal/high flags drawn here are the engine's verdict rather than a
        threshold re-implemented in a browser. Reference ranges live in exactly
        one place (r6/labs/interpret.py) and this view is not a second one.
        """
        tenant_id = mcp_app_tenant()
        html = render_template(
            'mcp_apps/lab_trends.html',
            tenant_id=tenant_id,
        )
        resp = Response(html, mimetype='text/html')
        resp.headers['Content-Type'] = 'text/html; profile=mcp-app'
        resp.headers['X-MCP-App'] = 'lab-trends'
        return resp


# --- App-level registration ------------------------------------------------

def _register_app(flask_app) -> None:
    import logging

    from main import _is_true

    logger = logging.getLogger("main")

    from r6.agent_runs.routes import agent_runs_blueprint

    flask_app.register_blueprint(agent_runs_blueprint)

    from r6.smbp.routes import smbp_blueprint

    flask_app.register_blueprint(smbp_blueprint)

    if _is_true(flask_app.config.get("DISABLE_COMMAND_CENTER")):
        logger.info("Command Center disabled via DISABLE_COMMAND_CENTER")
    else:
        from r6.command_center.routes import command_center_blueprint

        flask_app.register_blueprint(command_center_blueprint)

    @flask_app.cli.command("seed-demo-history")
    @click.option("--tenant-id", default="desktop-demo",
                  help="Tenant to load the history into.")
    def seed_demo_history_command(tenant_id: str) -> None:
        """Load the multi-year synthetic BP history into a demo tenant.

        Separate from `seed-demo`, which loads the small built-in set that
        runs before every deploy. This one is bulkier and is loaded on
        demand: it exists so a demo, a screenshot or an acceptance test has
        data with a shape worth looking at.

        Idempotent — every resource carries a fixed id, so a second run
        reports 0.
        """
        from r6.seed import seed_demo_data
        from r6.smbp.demo_history import smbp_history_resources
        with flask_app.app_context():
            count = seed_demo_data(tenant_id,
                                   resources=smbp_history_resources(),
                                   source='sample')
        click.echo(f"Seeded {count} resource(s) into {tenant_id}.")

