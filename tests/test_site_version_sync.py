"""Drift guards: healthclaw.io templates must track the released version.

The marketing site (templates/, deployed to healthclaw.io on every push) kept
drifting from reality — a v1.1.0 badge and "23 MCP tools"/"712 tests" survived
five releases. These tests make the sync a CI property instead of a memory:
bumping pyproject or the tool manifest without updating the templates fails
the suite (see RELEASING.md step 2).
"""

import json
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _tool_count():
    manifest = json.loads((ROOT / "adapters" / "tools.manifest.json").read_text())
    assert manifest["tool_count"] == len(manifest["tools"])
    return manifest["tool_count"]


def _version():
    return tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]


def test_landing_stats_tool_count_matches_manifest():
    html = (ROOT / "templates" / "index.html").read_text()
    assert f'data-target="{_tool_count()}"' in html, (
        "index.html stats row tool count != adapters manifest tool_count")


def test_no_stale_tool_counts_in_site_copy():
    count = _tool_count()
    for name in ("index.html", "wiki.html"):
        html = (ROOT / "templates" / name).read_text()
        for claimed in re.findall(r"(\d+) MCP tools", html):
            assert int(claimed) == count, (
                f"{name} claims '{claimed} MCP tools' but the manifest has {count}")


def test_base_nav_badge_matches_released_version():
    html = (ROOT / "templates" / "base.html").read_text()
    assert f"v{_version()}" in html, (
        f"base.html nav badge is not v{_version()} — update it with the release "
        "(RELEASING.md step 2)")


def test_health_context_version_matches_release():
    # index.html's nav badge renders v{{ health_context.version }}, sourced
    # from .health-context.yaml — the value that actually shows on
    # healthclaw.io. It sat at 1.3.0 for three releases before this guard.
    m = re.search(r"^version:\s*([\d.]+)", (ROOT / ".health-context.yaml").read_text(),
                  re.MULTILINE)
    assert m and m.group(1) == _version(), (
        f".health-context.yaml version {m.group(1) if m else '(missing)'} != "
        f"pyproject {_version()} — this drives the live site's nav badge")


def test_readme_release_badge_matches_version():
    readme = (ROOT / "README.md").read_text()
    assert f"release-v{_version()}-" in readme, (
        "README release badge is stale vs pyproject version")
