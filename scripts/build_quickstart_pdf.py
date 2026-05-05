#!/usr/bin/env python3
"""Build static/healthclaw-quickstart.pdf from this script.

This is the source of truth for the downloadable quickstart guide. Re-run
whenever you want to refresh the PDF:

    uv run python scripts/build_quickstart_pdf.py

The script keeps the PDF under 10 pages, light on prose, heavy on
runnable command blocks. Brand palette mirrors the landing page
(cyan #22d3ee, amber #fbbf24, dark navy #0A0E17).
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    NextPageTemplate,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)


# ── Brand palette ───────────────────────────────────────────────────────────
CYAN = colors.HexColor("#22d3ee")
CYAN_DIM = colors.HexColor("#0e9aaf")
AMBER = colors.HexColor("#fbbf24")
GREEN = colors.HexColor("#34d399")
NAVY = colors.HexColor("#0A0E17")
INK = colors.HexColor("#0f172a")
SLATE = colors.HexColor("#475569")
SLATE_LIGHT = colors.HexColor("#94a3b8")
PAPER = colors.HexColor("#fafafa")
CODE_BG = colors.HexColor("#f1f5f9")
RULE = colors.HexColor("#cbd5e1")


# ── Page templates ──────────────────────────────────────────────────────────
def _draw_cover_bg(canvas, doc):
    """Cover page: full navy bleed with a cyan ribbon."""
    canvas.saveState()
    canvas.setFillColor(NAVY)
    canvas.rect(0, 0, LETTER[0], LETTER[1], fill=1, stroke=0)
    # Cyan top ribbon
    canvas.setFillColor(CYAN)
    canvas.rect(0, LETTER[1] - 0.35 * inch, LETTER[0], 0.35 * inch, fill=1, stroke=0)
    # Faint cyan glow circle (decorative)
    canvas.setFillColor(colors.HexColor("#0e3a44"))
    canvas.circle(LETTER[0] - 1.5 * inch, 1.5 * inch, 1.6 * inch, fill=1, stroke=0)
    canvas.restoreState()


def _draw_body_chrome(canvas, doc):
    """Body pages: subtle top bar, page number bottom-right."""
    canvas.saveState()
    canvas.setFillColor(CYAN)
    canvas.rect(0, LETTER[1] - 0.18 * inch, LETTER[0], 0.18 * inch, fill=1, stroke=0)
    # Page number
    canvas.setFillColor(SLATE_LIGHT)
    canvas.setFont("Helvetica", 8.5)
    canvas.drawRightString(LETTER[0] - 0.6 * inch, 0.45 * inch, f"{doc.page}")
    # Footer brand
    canvas.drawString(0.6 * inch, 0.45 * inch, "HealthClaw Quickstart  ·  healthclaw.io")
    canvas.restoreState()


# ── Styles ──────────────────────────────────────────────────────────────────
def _styles():
    base = getSampleStyleSheet()

    s = {
        "cover_eyebrow": ParagraphStyle(
            "cover_eyebrow", parent=base["Normal"],
            fontName="Helvetica", fontSize=10, leading=14, textColor=CYAN,
            alignment=TA_CENTER, spaceBefore=140,
        ),
        "cover_title": ParagraphStyle(
            "cover_title", parent=base["Title"],
            fontName="Helvetica-Bold", fontSize=44, leading=50,
            textColor=colors.HexColor("#f1f5f9"), alignment=TA_CENTER, spaceAfter=18,
        ),
        "cover_sub": ParagraphStyle(
            "cover_sub", parent=base["Normal"],
            fontName="Helvetica", fontSize=14, leading=20,
            textColor=colors.HexColor("#cbd5e1"), alignment=TA_CENTER, spaceAfter=30,
        ),
        "cover_tagline": ParagraphStyle(
            "cover_tagline", parent=base["Normal"],
            fontName="Helvetica-Oblique", fontSize=11, leading=16,
            textColor=AMBER, alignment=TA_CENTER, spaceBefore=80,
        ),
        "cover_meta": ParagraphStyle(
            "cover_meta", parent=base["Normal"],
            fontName="Courier", fontSize=9, leading=12,
            textColor=colors.HexColor("#94a3b8"), alignment=TA_CENTER,
            spaceBefore=200,
        ),
        "h1": ParagraphStyle(
            "h1", parent=base["Heading1"],
            fontName="Helvetica-Bold", fontSize=22, leading=28,
            textColor=INK, spaceBefore=4, spaceAfter=4,
        ),
        "h1_num": ParagraphStyle(
            "h1_num", parent=base["Heading1"],
            fontName="Helvetica-Bold", fontSize=11, leading=14,
            textColor=CYAN, spaceBefore=2, spaceAfter=0,
        ),
        "h2": ParagraphStyle(
            "h2", parent=base["Heading2"],
            fontName="Helvetica-Bold", fontSize=13, leading=18,
            textColor=INK, spaceBefore=14, spaceAfter=4,
        ),
        "body": ParagraphStyle(
            "body", parent=base["Normal"],
            fontName="Helvetica", fontSize=10.5, leading=15.5,
            textColor=INK, spaceAfter=6, alignment=TA_LEFT,
        ),
        "body_sm": ParagraphStyle(
            "body_sm", parent=base["Normal"],
            fontName="Helvetica", fontSize=9, leading=13,
            textColor=SLATE, spaceAfter=4,
        ),
        "callout": ParagraphStyle(
            "callout", parent=base["Normal"],
            fontName="Helvetica-Oblique", fontSize=10, leading=15,
            textColor=INK, leftIndent=10, rightIndent=10,
            borderColor=AMBER, borderWidth=0, borderPadding=10,
            backColor=colors.HexColor("#fef3c7"),
            spaceBefore=8, spaceAfter=8,
        ),
        "code": ParagraphStyle(
            "code", parent=base["Code"],
            fontName="Courier-Bold", fontSize=9, leading=12.5,
            textColor=INK, backColor=CODE_BG,
            leftIndent=10, rightIndent=10,
            borderPadding=8, borderColor=RULE, borderWidth=0,
            spaceBefore=4, spaceAfter=8,
        ),
        "tip": ParagraphStyle(
            "tip", parent=base["Normal"],
            fontName="Helvetica", fontSize=9.5, leading=13,
            textColor=GREEN, spaceAfter=4, leftIndent=14,
        ),
        "footer_brand": ParagraphStyle(
            "footer_brand", parent=base["Normal"],
            fontName="Helvetica-Bold", fontSize=11, leading=14,
            textColor=CYAN_DIM, alignment=TA_CENTER, spaceBefore=14,
        ),
    }
    return s


# ── Helpers ─────────────────────────────────────────────────────────────────
def code_block(text: str, sty) -> Paragraph:
    """Render a multi-line shell command as a code paragraph (preserve newlines)."""
    html = (
        text.strip()
        .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace("\n", "<br/>")
        .replace("  ", "&nbsp;&nbsp;")
    )
    return Paragraph(html, sty["code"])


def step_header(num: str, title: str, sty) -> list:
    return [
        Paragraph(f"STEP&nbsp;{num}".upper(), sty["h1_num"]),
        Paragraph(title, sty["h1"]),
        Spacer(1, 0.05 * inch),
    ]


def hr(width=7.0):
    """A thin horizontal rule."""
    t = Table([[""]], colWidths=[width * inch], rowHeights=[0.5])
    t.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, -1), 0.5, RULE)]))
    return t


# ── Build ───────────────────────────────────────────────────────────────────
def build(out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    sty = _styles()

    doc = BaseDocTemplate(
        str(out),
        pagesize=LETTER,
        leftMargin=0.7 * inch, rightMargin=0.7 * inch,
        topMargin=0.55 * inch, bottomMargin=0.7 * inch,
        title="HealthClaw Quickstart",
        author="healthclaw.io",
        subject="Get from zero to a private, agent-mediated view of your own health records.",
    )

    cover_frame = Frame(0, 0, LETTER[0], LETTER[1], leftPadding=0,
                        bottomPadding=0, rightPadding=0, topPadding=0,
                        showBoundary=0, id="cover")
    body_frame = Frame(doc.leftMargin, doc.bottomMargin,
                       doc.width, doc.height,
                       leftPadding=0, rightPadding=0,
                       topPadding=0, bottomPadding=0,
                       showBoundary=0, id="body")

    doc.addPageTemplates([
        PageTemplate(id="cover", frames=[cover_frame], onPage=_draw_cover_bg),
        PageTemplate(id="body", frames=[body_frame], onPage=_draw_body_chrome),
    ])

    story: list = []

    # ── COVER ───────────────────────────────────────────────────────────────
    story.append(Paragraph("HEALTHCLAW · OPENCLAW", sty["cover_eyebrow"]))
    story.append(Spacer(1, 12))
    story.append(Paragraph("Your Health Records,<br/>On Your Terms.", sty["cover_title"]))
    story.append(Paragraph(
        "A 30-minute quickstart for the private,<br/>"
        "agent-mediated personal health stack.", sty["cover_sub"]))
    story.append(Paragraph(
        "“ You own your records. The cloud is just a courier. ”",
        sty["cover_tagline"]))
    story.append(Paragraph(
        f"Edition · {date.today().isoformat()}<br/>"
        "github.com/aks129/HealthClawGuardrails",
        sty["cover_meta"]))

    story.append(NextPageTemplate("body"))
    story.append(PageBreak())

    # ── PAGE 2 — What you're building ───────────────────────────────────────
    story += step_header("0", "What you're about to build", sty)
    story.append(Paragraph(
        "By the end of this guide your machine — laptop, Mac mini, "
        "Linux box — will hold a complete, private, agent-mediated view of "
        "your own clinical records. Nothing leaves the box unless you say so.",
        sty["body"]))

    story.append(Spacer(1, 8))
    story.append(Paragraph(
        "<b>The stack, top to bottom:</b>", sty["body"]))

    stack_data = [
        ["You",          "Telegram · Claude Desktop · Claude Code · Web"],
        ["▼", ""],
        ["OpenClaw",     "Personas · Skills · Multi-channel gateway   :4319"],
        ["▼", ""],
        ["HealthClaw",   "PHI redaction · Audit · Step-up auth · MCP   :5000"],
        ["▼", ""],
        ["FHIR Server",  "HAPI or Medplum — your records live here     :8080"],
        ["▼", ""],
        ["Your records", "Never leave this machine."],
    ]
    t = Table(stack_data, colWidths=[1.3 * inch, 5.0 * inch])
    t.setStyle(TableStyle([
        ("FONT", (0, 0), (0, -1), "Helvetica-Bold", 11),
        ("FONT", (1, 0), (1, -1), "Courier", 9),
        ("TEXTCOLOR", (0, 0), (0, -1), CYAN_DIM),
        ("TEXTCOLOR", (1, 0), (1, -1), INK),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        # Color the arrow rows
        ("TEXTCOLOR", (0, 1), (0, 1), CYAN),
        ("TEXTCOLOR", (0, 3), (0, 3), CYAN),
        ("TEXTCOLOR", (0, 5), (0, 5), CYAN),
        ("TEXTCOLOR", (0, 7), (0, 7), CYAN),
    ]))
    story.append(t)

    story.append(Spacer(1, 14))
    story.append(Paragraph("Privacy guarantee", sty["h2"]))
    story.append(Paragraph(
        "Records flow EHR → connector → your machine and stop there. PHI "
        "redaction is applied <b>in-process</b> before any file is written. "
        "The only outbound traffic during data pulls is the OAuth/SMART-on-FHIR "
        "handshake your EHR requires.",
        sty["body"]))

    story.append(Spacer(1, 10))
    story.append(Paragraph("Prerequisites", sty["h2"]))
    prereq = [
        ["macOS / Linux",   "uname -sm"],
        ["Python 3.11+",    "python3 --version"],
        ["Node 22+",        "node --version"],
        ["Docker (optional)", "docker --version"],
        ["git",             "git --version"],
        ["~30 minutes",     "—"],
    ]
    t = Table(prereq, colWidths=[2.4 * inch, 4.0 * inch])
    t.setStyle(TableStyle([
        ("FONT", (0, 0), (0, -1), "Helvetica-Bold", 10),
        ("FONT", (1, 0), (1, -1), "Courier", 9),
        ("TEXTCOLOR", (0, 0), (0, -1), INK),
        ("TEXTCOLOR", (1, 0), (1, -1), SLATE),
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [colors.white, CODE_BG]),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.append(t)

    story.append(PageBreak())

    # ── STEP 1a — OpenClaw ───────────────────────────────────────────────────
    story += step_header("1a", "Install OpenClaw", sty)
    story.append(Paragraph(
        "OpenClaw is your local AI gateway. It runs the agent personas, "
        "exposes them on whichever channels you want — Telegram, WhatsApp, "
        "iMessage, Slack, web — and gives them access to your installed skills.",
        sty["body"]))

    story.append(Paragraph("Already installed?", sty["h2"]))
    story.append(code_block(
        "which openclaw\n"
        "openclaw --version\n"
        "ls ~/.openclaw 2>/dev/null", sty))
    story.append(Paragraph(
        "If those return cleanly, skip ahead.", sty["body_sm"]))

    story.append(Paragraph("Fresh install", sty["h2"]))
    story.append(Paragraph(
        "The canonical install lives at <b>openclaw.ai</b>. Use whatever "
        "the homepage says — install method changes between releases. "
        "Historical commands the repo has used:",
        sty["body"]))
    story.append(code_block(
        "# A — npm (most common)\n"
        "npx -y @openclaw/cli init\n\n"
        "# B — Homebrew tap\n"
        "brew install openclaw/tap/openclaw\n\n"
        "openclaw auth login          # OAuth in browser\n"
        "openclaw gateway start       # http://localhost:4319\n"
        "openclaw status              # confirm running", sty))

    story.append(Paragraph(
        "<b>Tip.</b> For an always-on Mac mini setup with LaunchAgent + caffeinate, "
        "see <font color='#22d3ee'>docs/mac-mini-setup.md</font> in the HealthClaw repo.",
        sty["callout"]))

    story.append(PageBreak())

    # ── STEP 1b — FHIR server ────────────────────────────────────────────────
    story += step_header("1b", "Stand up an open-source FHIR server", sty)
    story.append(Paragraph(
        "Your records need somewhere to live. Two solid choices — pick one.",
        sty["body"]))

    story.append(Paragraph("Option A — HAPI FHIR · recommended", sty["h2"]))
    story.append(Paragraph(
        "Reference Java implementation. Easiest to run, no auth on localhost, "
        "perfect for first-timers.",
        sty["body"]))
    story.append(code_block(
        "# Already up?\n"
        "curl -sf http://localhost:8080/fhir/metadata >/dev/null && echo OK\n\n"
        "# Otherwise — Docker is fastest\n"
        "docker run -d --name hapi-fhir \\\n"
        "  -p 8080:8080 \\\n"
        "  hapiproject/hapi:latest\n\n"
        "# After ~30s\n"
        "curl -s http://localhost:8080/fhir/metadata | head -c 200", sty))

    story.append(Paragraph("Option B — Medplum · more features, OAuth-ready", sty["h2"]))
    story.append(Paragraph(
        "Full Medplum platform: FHIR R4 + auth + dashboard + audit. Heavier "
        "setup but gives you a UI to browse resources.",
        sty["body"]))
    story.append(code_block(
        "git clone https://github.com/medplum/medplum\n"
        "cd medplum\n"
        "docker compose -f compose-dev.yaml up -d\n"
        "open http://localhost:3000   # admin UI\n", sty))

    story.append(Paragraph(
        "<b>Public sandboxes exist</b> (hapi.fhir.org, r4.smarthealthit.org) but "
        "<b>do not put real records there</b> — they're shared, wiped weekly, "
        "and visible to anyone.",
        sty["callout"]))

    story.append(PageBreak())

    # ── STEP 2 — Connect EHR ─────────────────────────────────────────────────
    story += step_header("2", "Connect your real records (privacy-first)", sty)
    story.append(Paragraph(
        "Authenticate with your providers and pull records into the FHIR "
        "server you just stood up. Records flow EHR → connector → your "
        "machine. The cloud is only the OAuth provider.",
        sty["body"]))

    story.append(Paragraph("Pick a connector", sty["h2"]))

    conn = [
        ["Service",           "Best for",                                    "Auth"],
        ["HealthEx",          "US Epic / Cerner / CommonWell users — easiest", "OAuth"],
        ["fhir-skills (Mandel)", "DIY SMART-on-FHIR savvy users",               "SMART"],
        ["Flexpa",            "Apps that want a hosted FHIR endpoint",       "OAuth"],
        ["Direct SMART",      "Power users who want zero middleware",         "OAuth+PKCE"],
        ["TEFCA IAS / Fasten", "Cross-network records via QHIN",              "Stitch"],
    ]
    t = Table(conn, colWidths=[1.7 * inch, 3.6 * inch, 1.1 * inch])
    t.setStyle(TableStyle([
        ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 9.5),
        ("FONT", (0, 1), (-1, -1), "Helvetica", 9),
        ("BACKGROUND", (0, 0), (-1, 0), CYAN),
        ("TEXTCOLOR", (0, 0), (-1, 0), NAVY),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, CODE_BG]),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.append(t)

    story.append(Paragraph("Recommended path — HealthEx", sty["h2"]))
    story.append(Paragraph(
        "Lowest friction, cleanest output, first-class HealthClaw integration "
        "via the <font face='Courier'>/export</font> slash command.",
        sty["body"]))
    story.append(code_block(
        "# 1. Sign up at healthex.io\n"
        "# 2. claude.ai → Settings → Integrations → HealthEx → Connect\n"
        "# 3. Connect your health systems (Epic, Cerner, etc.)\n"
        "# 4. Stash your token in macOS Keychain so agents find it\n"
        "security add-generic-password -s healthex -a me -w '<your-token>'", sty))

    story.append(PageBreak())

    # ── STEP 3 — HealthClaw ──────────────────────────────────────────────────
    story += step_header("3", "Install HealthClaw Guardrails", sty)
    story.append(Paragraph(
        "HealthClaw sits between any agent and your FHIR server, enforcing "
        "PHI redaction, immutable audit trails, step-up auth on writes, and "
        "tenant isolation on every request.",
        sty["body"]))

    story.append(code_block(
        "git clone https://github.com/aks129/HealthClawGuardrails\n"
        "cd HealthClawGuardrails\n"
        "uv sync || pip install -e .\n\n"
        "cp .env.example .env\n"
        "# In .env:\n"
        "#   STEP_UP_SECRET=$(python3 -c 'import secrets; print(secrets.token_hex(32))')\n"
        "#   FHIR_UPSTREAM_URL=http://localhost:8080/fhir   # HAPI from Step 1b\n\n"
        "docker-compose up -d --build       # full stack (Flask + Redis + MCP)\n"
        "curl http://localhost:5000/r6/fhir/health", sty))

    story.append(Paragraph("Wire the OpenClaw personas", sty["h2"]))
    story.append(Paragraph(
        "One script seeds Sally (PCP), Mary (pharmacy), Dom (fitness), and "
        "Kristy (scheduler) — each persona's <font face='Courier'>AGENTS.md</font> "
        "already lists every HealthClaw slash command and tool.",
        sty["body"]))
    story.append(code_block(
        "./scripts/seed_openclaw_workspaces.sh\n"
        "./scripts/update_agent_prompts.sh\n"
        "./scripts/bot_commands_install.sh   # installs ~/.healthclaw/commands.py", sty))

    story.append(Paragraph("Connect Claude Desktop / Code via MCP", sty["h2"]))
    story.append(code_block(
        "{\n"
        '  "mcpServers": {\n'
        '    "healthclaw-local": {\n'
        '      "type": "http",\n'
        '      "url":  "http://localhost:3001/mcp",\n'
        '      "headers": { "X-Tenant-ID": "my-health" }\n'
        "    }\n"
        "  }\n"
        "}", sty))

    story.append(PageBreak())

    # ── STEP 4 — Pull data ───────────────────────────────────────────────────
    story += step_header("4", "Pull data through HealthClaw", sty)
    story.append(Paragraph(
        "Three ways. Pick whichever fits your moment.",
        sty["body"]))

    story.append(Paragraph("A · One slash command from any bot", sty["h2"]))
    story.append(Paragraph(
        "DM Sally / Mary / Dom / Kristy on Telegram:",
        sty["body"]))
    story.append(code_block(
        "/export                                            # HealthEx → redact → file\n"
        "/import ~/.healthclaw/exports/healthex-<date>.json # bundle → FHIR via guardrails", sty))

    story.append(Paragraph("B · Run the scripts directly", sty["h2"]))
    story.append(code_block(
        'HEALTHEX_AUTH_TOKEN="$(security find-generic-password -s healthex -w)" \\\n'
        "  python scripts/export_healthex_mcp.py \\\n"
        "  --tenant-id my-health \\\n"
        "  --output exports/healthex-$(date +%Y-%m-%d).json\n\n"
        "python scripts/import_healthex.py \\\n"
        "  --bundle-file exports/healthex-$(date +%Y-%m-%d).json \\\n"
        "  --tenant-id my-health \\\n"
        "  --step-up-secret \"$STEP_UP_SECRET\"", sty))

    story.append(Paragraph("C · Conversationally via Claude", sty["h2"]))
    story.append(Paragraph(
        "If HealthEx is connected to claude.ai, just ask:",
        sty["body"]))
    story.append(code_block(
        "Pull my complete health history across all categories going back\n"
        "15 years, fully paginated. Build a de-identified FHIR R4 transaction\n"
        "bundle with US Core resources and write it to\n"
        "healthclaw-bundle-<date>.json.", sty))

    story.append(Paragraph(
        "<b>What gets redacted.</b> HumanName → initials, Address → state+country, "
        "Identifier → SHA-256 hash, birthDate → year, telecom → ***, "
        "narrative div → empty. Clinical codes (ICD-10, SNOMED, LOINC, RxNorm, CVX) "
        "are <b>preserved</b> — they're the signal you actually want to analyze.",
        sty["callout"]))

    story.append(PageBreak())

    # ── STEP 5 — Verify ──────────────────────────────────────────────────────
    story += step_header("5", "Verify everything works", sty)
    story.append(Paragraph(
        "A 60-second checklist. Every line should green-light.",
        sty["body"]))

    story.append(Paragraph("Liveness", sty["h2"]))
    story.append(code_block(
        "openclaw status                                       # → running\n"
        "curl -sf http://localhost:8080/fhir/metadata | head -c 80\n"
        "curl -sf http://localhost:5000/r6/fhir/health\n"
        "curl -sf http://localhost:3001/mcp/health || true     # MCP orchestrator", sty))

    story.append(Paragraph("Records present", sty["h2"]))
    story.append(code_block(
        'curl -s "http://localhost:8080/fhir/Patient?_summary=count" \\\n'
        "  | python3 -c \"import sys,json; print('Patients:', json.load(sys.stdin).get('total'))\"", sty))

    story.append(Paragraph("Agent smoke test", sty["h2"]))
    story.append(Paragraph(
        "In Claude with the <font face='Courier'>healthclaw-local</font> MCP server connected:",
        sty["body"]))
    story.append(code_block(
        "Use the healthclaw-local tools. Get a step-up token, search for all\n"
        "Patients in tenant my-health, then read one Condition and one\n"
        "Observation. Confirm responses include _mcp_summary and that PHI\n"
        "looks redacted.", sty))

    story.append(Paragraph("Telegram smoke test", sty["h2"]))
    story.append(Paragraph(
        "DM any persona:", sty["body"]))
    story.append(code_block("/health\n/summary\n/conditions", sty))

    story.append(Paragraph(
        "Each should return a structured response paraphrased by the LLM. "
        "Names appear as initials. Identifiers prefixed with <font face='Courier'>"
        "redacted:sha256:</font>. AuditEvents recorded for every read.",
        sty["body_sm"]))

    story.append(PageBreak())

    # ── PAGE — Where to go next ─────────────────────────────────────────────
    story += step_header("→", "Where to go from here", sty)
    story.append(Paragraph(
        "HealthClaw ships with eight specialised skills that go deeper than this guide. "
        "Loaded into Claude Desktop / Code, they trigger on the matching prompts.",
        sty["body"]))

    skills_data = [
        ["getting-started",          "This guide. End-to-end onboarding."],
        ["fhir-r6-guardrails",       "The 14 MCP tools and their guarantees"],
        ["phi-redaction",            "Exactly what gets redacted and why"],
        ["fhir-upstream-proxy",      "Wire HAPI, Medplum, Epic upstream"],
        ["personal-health-records",  "Conversational HealthEx data pull workflow"],
        ["healthex-export-redacted", "MCP-SDK + in-process PHI redaction (the /export path)"],
        ["healthex-export",          "Tenant-to-tenant copy inside HealthClaw"],
        ["curatr",                   "Find data quality issues; propose patient-approved fixes"],
        ["fasten-connect",           "TEFCA IAS · Stitch widget · QHIN cross-network"],
    ]
    t = Table(skills_data, colWidths=[2.0 * inch, 4.4 * inch])
    t.setStyle(TableStyle([
        ("FONT", (0, 0), (0, -1), "Courier-Bold", 9.5),
        ("FONT", (1, 0), (1, -1), "Helvetica", 9.5),
        ("TEXTCOLOR", (0, 0), (0, -1), CYAN_DIM),
        ("TEXTCOLOR", (1, 0), (1, -1), INK),
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [colors.white, CODE_BG]),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.append(t)

    story.append(Spacer(1, 0.3 * inch))
    story.append(hr())
    story.append(Paragraph(
        "Browse all skills at <b>healthclaw.io/skills</b> · or in the repo "
        "at <font face='Courier'>github.com/aks129/HealthClawGuardrails/tree/main/skills</font>.",
        sty["body_sm"]))

    story.append(Spacer(1, 0.5 * inch))
    story.append(Paragraph("HealthClaw · OpenClaw", sty["footer_brand"]))
    story.append(Paragraph(
        "Open source · MIT License · A healthclaw.io project",
        sty["body_sm"]))

    doc.build(story)


if __name__ == "__main__":
    out = Path(__file__).resolve().parent.parent / "static" / "healthclaw-quickstart.pdf"
    build(out)
    print(f"wrote {out} ({out.stat().st_size:,} bytes)")
