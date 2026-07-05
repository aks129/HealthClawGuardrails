"""Agent Skills Discovery (RFC 8615 well-known URI) endpoint.

Serves /.well-known/agent-skills/index.json (+ v0.1 alias /.well-known/skills/)
per the Cloudflare Agent Skills Discovery spec v0.2.0, so Hermes, Claude Code,
Cursor, and any spec client can discover and install our skills straight from
healthclaw.io.
"""

import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAME_RE = re.compile(r"^(?!-)(?!.*--)[a-z0-9-]{1,64}(?<!-)$")


def test_index_serves_all_repo_skills(client):
    r = client.get("/.well-known/agent-skills/index.json")
    assert r.status_code == 200
    body = r.get_json()
    assert body["$schema"].startswith("https://schemas.agentskills.io/discovery/")
    names = {s["name"] for s in body["skills"]}
    on_disk = {p.parent.name for p in ROOT.glob("skills/*/SKILL.md")}
    assert names == on_disk


def test_index_entries_conform_to_spec(client):
    body = client.get("/.well-known/agent-skills/index.json").get_json()
    for s in body["skills"]:
        assert NAME_RE.match(s["name"]), s["name"]
        assert s["type"] == "skill-md"          # our skills are SKILL.md-only
        assert s["description"]
        assert s["url"].endswith(f"/{s['name']}/SKILL.md")
        assert s["digest"].startswith("sha256:")


def test_skill_md_served_and_digest_matches(client):
    body = client.get("/.well-known/agent-skills/index.json").get_json()
    s = body["skills"][0]
    r = client.get(f"/.well-known/agent-skills/{s['name']}/SKILL.md")
    assert r.status_code == 200
    assert "text/markdown" in r.content_type
    digest = hashlib.sha256(r.get_data()).hexdigest()
    assert s["digest"] == f"sha256:{digest}"


def test_unknown_skill_404s_and_no_traversal(client):
    assert client.get("/.well-known/agent-skills/nope/SKILL.md").status_code == 404
    r = client.get("/.well-known/agent-skills/..%2F..%2Fpyproject.toml/SKILL.md")
    assert r.status_code in (400, 404)


def test_v01_alias_path_works(client):
    # Hermes and v0.1 clients use /.well-known/skills/
    r = client.get("/.well-known/skills/index.json")
    assert r.status_code == 200
    assert r.get_json()["skills"]
