"""Public surfaces must not overclaim a legal de-identification standard."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PUBLIC_SURFACES = (
    "README.md",
    "templates/faq.html",
    "templates/r6_dashboard.html",
    "templates/index.html",
    "static/js/r6-dashboard.js",
    "skills/phi-redaction/SKILL.md",
    "skills/healthex-export/SKILL.md",
    "r6/routes.py",
)


def test_public_surfaces_call_output_a_preview_not_safe_harbor():
    forbidden = (
        "HIPAA Safe Harbor de-identification",
        "Safe Harbor De-identified",
        "De-identify Patient (Safe Harbor)",
        "hipaa-safe-harbor",
    )

    for relative_path in PUBLIC_SURFACES:
        text = (ROOT / relative_path).read_text()
        for phrase in forbidden:
            assert phrase not in text, f"{relative_path} still contains {phrase!r}"
