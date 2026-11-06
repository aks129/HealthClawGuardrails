"""The core runs without its optional modules (r6/extensions.py).

Three kinds of evidence, all core-only (none of these is marked
`extension`, so the core-only CI job runs them):

1. Source: no core module imports an optional module. The registry is the
   only way in.
2. Tests: a test module that imports an optional module at the top is marked
   `extension`, so the core-only job deselects it instead of running it
   against a server that does not have the module.
3. Runtime, in a subprocess so the registry is built fresh: with no
   extension loaded, and with the optional modules made unimportable, the
   app starts, the CapabilityStatement serves, and an optional operation
   answers 501 not-supported rather than 404 or 500.
"""

from __future__ import annotations

import ast
import json
import os
import pathlib
import re
import subprocess
import sys

import pytest

from r6 import extensions

ROOT = pathlib.Path(__file__).resolve().parent.parent

#: Python packages the core reaches only through the registry.
OPTIONAL = (
    "careagents",
    "bundled_extensions",
    "r6.labs",
    "r6.caregaps",
    "r6.brief",
    "r6.command_center",
    "r6.quality",
    "r6.smbp",
    "r6.agent_runs",
    "r6.agent_client",
    "r6.context_builder",
    "r6.health_context",
)
_OPTIONAL_RE = re.compile(
    r"^(%s)(\.|$)" % "|".join(re.escape(name) for name in OPTIONAL))

#: Directories and files under r6/ that belong to the optional modules.
_OPTIONAL_PATHS = tuple(
    name.replace(".", "/") for name in OPTIONAL if name.startswith("r6."))


def _imported_names(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.lineno, node.module
            for alias in node.names:
                yield node.lineno, f"{node.module}.{alias.name}"


def _core_sources():
    for path in sorted((ROOT / "r6").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if any(rel == p + ".py" or rel.startswith(p + "/")
               for p in _OPTIONAL_PATHS):
            continue
        yield rel, path
    for rel in ("app.py", "main.py"):
        yield rel, ROOT / rel
    for path in sorted((ROOT / "api").glob("*.py")):
        yield path.relative_to(ROOT).as_posix(), path


def test_no_core_module_imports_an_optional_module():
    offenders = [
        f"{rel}:{line} imports {name}"
        for rel, path in _core_sources()
        for line, name in _imported_names(ast.parse(path.read_text()))
        if _OPTIONAL_RE.match(name)
    ]
    assert not offenders, (
        "The core reaches optional modules only through r6/extensions.py:\n"
        + "\n".join(offenders))


def _module_marked(tree):
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", None) == "pytestmark" for t in node.targets):
            return "pytest.mark.extension" in ast.unparse(node.value)
    return False


#: Being changed by work in flight; mark it, and drop it from the core-only
#: job's --ignore in .github/workflows/ci.yml, once that work lands.
_MARK_PENDING = {"tests/test_careagents_calm_hub_view.py"}


def test_a_test_that_imports_an_optional_module_is_marked():
    unmarked = []
    for path in sorted((ROOT / "tests").rglob("test_*.py")):
        if path.relative_to(ROOT).as_posix() in _MARK_PENDING:
            continue
        tree = ast.parse(path.read_text())
        top = [name for node in tree.body
               if isinstance(node, (ast.Import, ast.ImportFrom))
               for _, name in _imported_names(node)
               if _OPTIONAL_RE.match(name)]
        if top and not _module_marked(tree):
            unmarked.append(f"{path.relative_to(ROOT)} imports {top[0]}")
    assert not unmarked, (
        "Mark these `pytestmark = pytest.mark.extension` so the core-only "
        "job deselects them:\n" + "\n".join(unmarked))


# --- discovery ---------------------------------------------------------------

def test_an_empty_setting_loads_no_extension(monkeypatch):
    monkeypatch.setenv(extensions.ENV_VAR, "")
    assert extensions._discover() == []


def test_unset_loads_the_bundled_modules(monkeypatch):
    pytest.importorskip("bundled_extensions")
    monkeypatch.delenv(extensions.ENV_VAR, raising=False)
    names = [name for name, _ in extensions._discover()]
    assert names[-len(extensions.BUNDLED):] == list(extensions.BUNDLED)


def test_a_named_extension_that_cannot_import_fails_loudly(monkeypatch):
    monkeypatch.setenv(extensions.ENV_VAR, "no_such_extension_module")
    with pytest.raises(ModuleNotFoundError):
        extensions._discover()


def test_a_registry_claims_only_what_its_extensions_declare():
    registry = extensions.ExtensionRegistry()
    registry.add_r6_routes(lambda bp, helpers: None,
                           operations=("Observation/$interpret",))
    assert registry.provides("Observation/$interpret")
    assert not registry.provides("Patient/$care-gaps")


def test_without_providers_the_core_reads_an_empty_context():
    registry = extensions.ExtensionRegistry()
    assert registry.health_context() == {}
    assert registry.bundle_ingest is None
    registry.add_health_context(lambda: {"audit_agent_default": "x"})
    registry.add_bundle_ingest(lambda bundle, tenant_id=None: {})
    assert registry.health_context() == {"audit_agent_default": "x"}
    assert registry.bundle_ingest is not None


def test_the_careagents_back_link_reads_app_config(app, monkeypatch):
    from app import _careagents_home

    monkeypatch.delenv("CAREAGENTS_CONSENT_URL", raising=False)

    with app.test_request_context("/"):
        app.config["CAREAGENTS_CONSENT_URL"] = "https://hub.example/authorize"
        assert _careagents_home() == "https://hub.example/home"
        app.config["CAREAGENTS_CONSENT_URL"] = ""
        assert _careagents_home() == "https://careagents.cloud/home"


def test_the_back_link_and_the_consent_handoff_read_the_same_url(
        app, monkeypatch):
    # Config and environment disagree: both readers must pick the same one.
    from urllib.parse import urlsplit

    from app import _careagents_home
    from r6.oauth import consent_url

    monkeypatch.setenv("CAREAGENTS_CONSENT_URL", "https://env.example/authorize")
    with app.test_request_context("/"):
        app.config["CAREAGENTS_CONSENT_URL"] = "https://hub.example/authorize"
        origin = urlsplit(consent_url())
        assert _careagents_home() == f"{origin.scheme}://{origin.netloc}/home"
        assert _careagents_home() == "https://hub.example/home"


def test_a_health_context_provider_is_what_the_core_reads(
        app, client, monkeypatch):
    # Values unlike the core's fallbacks ('healthclaw-guardrails' and the
    # release version), so the assertions cannot pass on the fallbacks.
    from models import db
    from r6.audit import add_audit_event
    from r6.models import AuditEventRecord

    monkeypatch.setattr(
        extensions.get_registry(), "health_context_provider",
        lambda: {"audit_agent_default": "provider-agent",
                 "version": "9.9.9-provider"})
    add_audit_event("read", "Patient", "provider-probe",
                    tenant_id="test-tenant", detail="probe")
    db.session.commit()
    row = AuditEventRecord.query.filter_by(resource_id="provider-probe").one()
    assert row.agent_id == "provider-agent"
    meta = client.get("/r6/fhir/metadata",
                      headers={"X-Tenant-Id": "test-tenant"}).get_json()
    assert meta["software"]["version"] == "9.9.9-provider"


def test_the_oauth_consent_url_reads_app_config_first(app, monkeypatch):
    from r6.oauth import consent_url

    monkeypatch.setenv("CAREAGENTS_CONSENT_URL", "https://env.example/authorize")
    with app.app_context():
        app.config["CAREAGENTS_CONSENT_URL"] = "https://hub.example/authorize"
        assert consent_url() == "https://hub.example/authorize"
        app.config["CAREAGENTS_CONSENT_URL"] = ""
        assert consent_url() == "https://env.example/authorize"


# --- runtime, in a fresh interpreter -------------------------------------------

_PROBE = r"""
import importlib.abc, json, os, sys

blocked = tuple(json.loads(os.environ["PROBE_BLOCK"]))

class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if any(name == b or name.startswith(b + ".") for b in blocked):
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)
        return None

sys.meta_path.insert(0, Block())

from main import create_app
from models import db

app = create_app({"TESTING": True,
                  "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
                  "LEGACY_BOOT_ON_CREATE": False})
with app.app_context():
    db.create_all()
client = app.test_client()
h = {"X-Tenant-Id": "test-tenant"}
interpret = client.post("/r6/fhir/Observation/$interpret", json={}, headers=h)
gaps_get = client.get("/r6/fhir/Patient/$care-gaps", headers=h)
gaps_post = client.post("/r6/fhir/Patient/$care-gaps", json={}, headers=h)
meta = client.get("/r6/fhir/metadata", headers=h)
ingest = client.post("/r6/fhir/Bundle/$ingest-context",
                     json={"resourceType": "Bundle", "type": "collection"},
                     headers=h)
home = client.get("/")
from r6.audit import add_audit_event
from r6.models import AuditEventRecord
with app.app_context():
    add_audit_event("read", "Patient", "probe-1", tenant_id="test-tenant",
                    detail="probe")
    db.session.commit()
    audit_agent = AuditEventRecord.query.filter_by(
        resource_id="probe-1").one().agent_id
from r6.extensions import get_registry
print(json.dumps({
    "loaded": get_registry().loaded,
    "interpret": [interpret.status_code, interpret.get_json()],
    "gaps_get": [gaps_get.status_code, gaps_get.get_json()],
    "gaps_post": gaps_post.status_code,
    "metadata": [meta.status_code, meta.get_data(as_text=True)],
    "ingest": [ingest.status_code, ingest.get_json()],
    "home": home.status_code,
    "audit_agent": audit_agent,
    "command_center": client.get("/command-center/api/overview").status_code,
    "optional_loaded": sorted(m for m in sys.modules
                              if any(m == b or m.startswith(b + ".")
                                     for b in blocked)),
}))
"""


def _probe(env_extra, block=()):
    env = os.environ.copy()
    env.pop(extensions.ENV_VAR, None)
    env.update(env_extra)
    env["PROBE_BLOCK"] = json.dumps(list(block))
    env["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    result = subprocess.run(
        [sys.executable, "-c", _PROBE], cwd=ROOT, env=env, text=True,
        capture_output=True, timeout=120, check=False)
    assert result.returncode == 0, result.stderr[-3000:]
    return json.loads(result.stdout.strip().splitlines()[-1])


def _assert_core_only(out):
    assert out["loaded"] == []
    status, body = out["interpret"]
    assert status == 501
    assert body["resourceType"] == "OperationOutcome"
    assert body["issue"][0]["code"] == "not-supported"
    assert out["gaps_get"][0] == 501
    assert out["gaps_get"][1]["issue"][0]["code"] == "not-supported"
    assert out["gaps_post"] == 501
    status, text = out["metadata"]
    assert status == 200
    assert "$interpret" not in text and "care-gaps" not in text
    assert "ingest-context" not in text
    # The release version, not a stale literal, with no context provider.
    from r6.version import __version__
    assert json.loads(text)["software"]["version"] == __version__
    status, body = out["ingest"]
    assert status == 501
    assert body["issue"][0]["code"] == "not-supported"
    assert out["home"] == 200
    # Audit is still written, under the core's own agent name.
    assert out["audit_agent"] == "healthclaw-guardrails"
    assert out["command_center"] == 404


def test_with_no_extension_the_optional_operations_answer_not_supported():
    _assert_core_only(_probe({extensions.ENV_VAR: ""}))


def test_the_core_starts_when_the_optional_modules_cannot_be_imported():
    # Nothing set: the bundled default is tried, found missing, and skipped.
    out = _probe({}, block=OPTIONAL)
    _assert_core_only(out)
    assert out["optional_loaded"] == []
