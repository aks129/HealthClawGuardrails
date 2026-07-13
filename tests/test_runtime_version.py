"""Runtime surfaces must advertise the release version from one source."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_runtime_version_matches_project_metadata():
    from r6.version import __version__

    assert __version__ == "1.8.0"


def test_runtime_surfaces_do_not_embed_stale_release_versions():
    routes = (ROOT / "r6" / "routes.py").read_text(encoding="utf-8")
    proxy = (ROOT / "r6" / "fhir_proxy.py").read_text(encoding="utf-8")

    assert "'version': '1.0.0'" not in routes
    assert "HealthClaw-Guardrails/1.0.0" not in proxy
    assert "from r6.version import __version__" in routes
    assert "from r6.version import __version__" in proxy
