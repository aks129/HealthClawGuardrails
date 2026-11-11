"""The run queue and the conversation tables are registered by the core.

r6/agent_runs/ and r6/conversations.py are core paths, so the core registers
their tables (r6/database_migrations.py) and their blueprint (main.py) itself,
not through an extension. Each probe runs in a fresh interpreter, because the
extension registry is built once per process.

The core-only tests carry no `extension` mark: the core-only CI job runs them.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

import pytest

from r6 import extensions

ROOT = pathlib.Path(__file__).resolve().parent.parent

RUN_QUEUE_TABLES = {
    "agent_runs", "agent_tool_calls", "agent_run_events",
    "agent_worker_presence", "cc_conversations", "cc_conversation_messages",
}

# What migrations/env.py does: the model registration alone, no app. Booting
# the app would import the tables through the blueprint and hide a missing
# registration.
_METADATA_PROBE = r"""
import json, sys
from models import db
from r6.database_migrations import register_model_metadata
from r6.extensions import get_registry

register_model_metadata()
print(json.dumps({
    "loaded": get_registry().loaded,
    "metadata": sorted(db.metadata.tables),
    "command_center": sorted(m for m in sys.modules
                             if m.startswith("r6.command_center")),
}))
"""

_APP_PROBE = r"""
import json, os
from sqlalchemy import inspect
from main import create_app
from models import db
from r6.extensions import get_registry

app = create_app({"TESTING": True, "LEGACY_BOOT_ON_CREATE": False,
                  "SQLALCHEMY_DATABASE_URI": os.environ["PROBE_DB"]})
init_db = app.test_cli_runner().invoke(args=["init-db"])
with app.app_context():
    created = sorted(inspect(db.engine).get_table_names())
client = app.test_client()
health = "/command-center/api/runs/workers/health"
answered = client.get(health, headers={"X-Internal-Secret": "probe-secret"})
print(json.dumps({
    "loaded": get_registry().loaded,
    "init_db": [init_db.exit_code, init_db.output[-500:]],
    "rules": [[r.rule, r.endpoint] for r in app.url_map.iter_rules()],
    "metadata": sorted(db.metadata.tables),
    "created": created,
    "health_refused": client.get(health).status_code,
    "health": [answered.status_code, answered.get_json()],
}))
"""


def _run(code, extensions_env, tmp_path, db_name="probe.db"):
    env = os.environ.copy()
    env.pop(extensions.ENV_VAR, None)
    if extensions_env is not None:
        env[extensions.ENV_VAR] = extensions_env
    env["PROBE_DB"] = f"sqlite:///{tmp_path / db_name}"
    env["SQLALCHEMY_DATABASE_URI"] = env["PROBE_DB"]
    env["INTERNAL_TOKEN_MINT_SECRET"] = "probe-secret"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, env=env, text=True,
        capture_output=True, timeout=180, check=False)
    assert result.returncode == 0, result.stderr[-3000:]
    return json.loads(result.stdout.strip().splitlines()[-1])


def _run_queue_rules(out):
    return [rule for rule in out["rules"] if rule[1].startswith("agent_runs.")]


def test_the_core_registers_the_run_queue_tables_without_an_extension(
        tmp_path):
    out = _run(_METADATA_PROBE, "", tmp_path)
    assert out["loaded"] == []
    assert RUN_QUEUE_TABLES <= set(out["metadata"])
    # The conversation tables come from r6/conversations.py, not through the
    # command center's re-export.
    assert out["command_center"] == []


def test_the_core_serves_the_run_queue_without_an_extension(tmp_path):
    out = _run(_APP_PROBE, "", tmp_path)
    assert out["loaded"] == []
    assert out["init_db"][0] == 0, out["init_db"][1]
    assert RUN_QUEUE_TABLES <= set(out["created"])
    assert RUN_QUEUE_TABLES <= set(out["metadata"])
    assert len(_run_queue_rules(out)) == 14
    # The worker-health route alone (tests/test_run_queue_core_only_gates.py
    # covers the gates on all 14): refused without the internal secret. With
    # it, the queue query ran against the tables init-db created ("ok"; a
    # missing table answers "error"), and no worker is present.
    assert out["health_refused"] == 403
    status, body = out["health"]
    assert status == 503
    assert body["queue"] == "ok"
    assert body["status"] == "unavailable"


@pytest.mark.extension
def test_the_bundled_modules_add_to_the_core_and_move_nothing(tmp_path):
    pytest.importorskip("bundled_extensions")
    core = _run(_APP_PROBE, "", tmp_path, "core.db")
    full = _run(_APP_PROBE, None, tmp_path)
    assert full["loaded"] == list(extensions.BUNDLED)
    assert set(core["metadata"]) < set(full["metadata"])
    assert set(core["created"]) == set(full["created"])
    # Registered once, by the core, in the same place either way: directly
    # before the first blueprint an extension adds. The bundled extension
    # registers smbp's first, so that name marks where the extensions' rules
    # start; if it reorders its blueprints, update the name here. The order
    # is pinned because the rules stood there before the move, and a later
    # position would let an extension's rule shadow a run-queue path.
    assert _run_queue_rules(full) == _run_queue_rules(core)
    endpoints = [endpoint for _, endpoint in full["rules"]]
    last_run_rule = max(i for i, e in enumerate(endpoints)
                        if e.startswith("agent_runs."))
    assert endpoints[last_run_rule + 1].startswith("smbp.")
