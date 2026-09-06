"""A skill never tells a reader to configure a host that hosts nothing (#626).

`healthclaw.up.railway.app` answered 404 'Application not found' from the
platform on 2026-09-04 and again on 2026-09-06, while the personal-health-
records skill and the generated quickstart told a reader to configure its
`/mcp` and to seed against its `/r6/fhir`. Measured on 2026-09-06 before the
replacement: the demo MCP remote in server.json answers 200 on /health, and
app.healthclaw.io answers 200 on /r6/fhir/health. The skill now names those.

The retired host stays only in the evidence inventory that recorded it and in
the inventory script's dead list. MUTATION: put the retired host back into
the skill -> red naming the file.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RETIRED = "healthclaw.up.railway.app"
ALLOWED = {"docs/evidence", "scripts/surface_inventory.py"}


def test_no_skill_or_script_names_the_retired_host():
    offenders = []
    for folder in ("skills", "scripts", "templates", "docs", "README.md"):
        base = ROOT / folder
        paths = [base] if base.is_file() else sorted(base.rglob("*"))
        for path in paths:
            if not path.is_file() or path.suffix not in (".md", ".py", ".json", ".html", ".txt"):
                continue
            rel = path.relative_to(ROOT).as_posix()
            if any(rel.startswith(a) for a in ALLOWED):
                continue
            if RETIRED in path.read_text(encoding="utf-8", errors="ignore"):
                offenders.append(rel)
    assert not offenders, offenders


def test_the_skill_names_the_demo_remote_from_server_json():
    import json
    remotes = [r["url"] for r in json.loads((ROOT / "server.json").read_text())["remotes"]]
    skill = (ROOT / "skills/personal-health-records/SKILL.md").read_text()
    demo = [u for u in remotes if "demo" in u]
    assert demo and demo[0] in skill
