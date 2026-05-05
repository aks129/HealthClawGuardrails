#!/usr/bin/env python3
"""Build static/healthclaw-quickstart.pdf from this script.

This is the source of truth for the downloadable quickstart guide. Re-run
whenever you want to refresh the PDF:

    uv run python scripts/build_quickstart_pdf.py

The PDF has two parallel sections so it works for any reader:

  Path A — "No terminal needed"  (~3 pages, claude.ai + HealthEx,
            paste-ready prompts, no install of anything)
  Path B — "Self-host the stack" (~6 pages, the technical track —
            OpenClaw, FHIR server, HealthClaw, agent personas)

A "Pick your path" page after the cover lets the reader jump straight
to whichever fits.

Brand palette mirrors the landing page: cyan #22d3ee, amber #fbbf24,
dark navy #0A0E17.
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


def _path_banner(label: str, title: str, accent, sty) -> list:
    """A coloured ribbon that opens a Path A / Path B section."""
    pal = Paragraph(
        f'<font face="Helvetica-Bold" size="9" color="#0A0E17">'
        f'{label}</font> &nbsp;&nbsp; '
        f'<font face="Helvetica-Bold" size="14" color="#0A0E17">{title}</font>',
        ParagraphStyle("path_banner_inner", fontName="Helvetica",
                       fontSize=10, leading=18, alignment=TA_LEFT))
    t = Table([[pal]], colWidths=[7.0 * inch])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), accent),
        ("LEFTPADDING", (0, 0), (-1, -1), 14),
        ("RIGHTPADDING", (0, 0), (-1, -1), 14),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    return [t, Spacer(1, 14)]


def _prompt_box(text: str, sty) -> Table:
    """Render a paste-ready prompt as a tinted card with a small label."""
    label = Paragraph(
        '<font face="Helvetica-Bold" size="8" color="#0e9aaf">PROMPT — paste into Claude</font>',
        sty["body_sm"])
    body = Paragraph(text, ParagraphStyle(
        "prompt_body", parent=sty["body"],
        fontName="Helvetica", fontSize=10.5, leading=15.5,
        textColor=INK, leftIndent=0, rightIndent=0, spaceAfter=0))
    t = Table([[label], [body]], colWidths=[6.4 * inch])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#ecfeff")),
        ("LINEABOVE", (0, 0), (-1, 0), 2, CYAN),
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
        ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (0, 0), 8),
        ("BOTTOMPADDING", (0, 0), (0, 0), 2),
        ("TOPPADDING", (0, 1), (0, 1), 0),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 12),
    ]))
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
        "Two ways in:<br/>"
        "a 3-minute chat-only path, or a 30-minute fully-private self-host.",
        sty["cover_sub"]))
    story.append(Paragraph(
        "“ You own your records. The cloud is just a courier. ”",
        sty["cover_tagline"]))
    story.append(Paragraph(
        f"Edition · {date.today().isoformat()}<br/>"
        "github.com/aks129/HealthClawGuardrails",
        sty["cover_meta"]))

    story.append(NextPageTemplate("body"))
    story.append(PageBreak())

    # ── PAGE 2 — Pick your path ─────────────────────────────────────────────
    story += step_header("→", "Pick your path", sty)
    story.append(Paragraph(
        "There are two ways to get your records into a conversation with "
        "Claude. Both keep your data under your control. Pick whichever "
        "matches the time you have and how comfortable you are with a "
        "terminal.",
        sty["body"]))

    paths = [
        ["",                 "PATH A · No terminal",                       "PATH B · Self-host"],
        ["Time",             "≈ 3 minutes",                                "≈ 30 minutes"],
        ["What you do",      "Click around in claude.ai. That's it.",      "Run a few commands on your laptop"],
        ["Tools you need",   "A claude.ai account. A health-system login.", "claude.ai + Docker + Python"],
        ["Where your\nrecords live", "In your active Claude conversation. Closed when you close the chat.", "On your machine. Never leave."],
        ["What you can do",  "Ask anything: care gaps, lab trends, prep for a visit",  "Same — plus Telegram bots, audit trail, full guardrails"],
        ["Best for",         "“I want to talk to Claude about my records.”", "“I want my own private store with agents.”"],
        ["Where it lives",   "Pages 3 – 5",                                 "Pages 6 – 11"],
    ]
    t = Table(paths, colWidths=[1.4 * inch, 2.5 * inch, 2.5 * inch])
    t.setStyle(TableStyle([
        ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 10),
        ("FONT", (0, 1), (0, -1), "Helvetica-Bold", 9.5),
        ("FONT", (1, 0), (-1, -1), "Helvetica", 9.5),
        ("BACKGROUND", (1, 0), (1, 0), CYAN),
        ("BACKGROUND", (2, 0), (2, 0), AMBER),
        ("TEXTCOLOR", (1, 0), (-1, 0), NAVY),
        ("TEXTCOLOR", (0, 0), (0, 0), colors.white),
        ("BACKGROUND", (0, 0), (0, 0), SLATE),
        ("TEXTCOLOR", (0, 1), (0, -1), INK),
        ("TEXTCOLOR", (1, 1), (-1, -1), INK),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, CODE_BG]),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ("LINEBELOW", (0, 0), (-1, 0), 0.6, NAVY),
    ]))
    story.append(t)

    story.append(Spacer(1, 12))
    story.append(Paragraph(
        "<b>Not sure?</b> Start with Path A. It takes 3 minutes and you'll "
        "know within 10 if it's enough for what you want. Path B is always "
        "there if you want to go deeper later — and many people end up "
        "running both.",
        sty["callout"]))

    story.append(Spacer(1, 8))
    story.append(Paragraph("Privacy guarantee (both paths)", sty["h2"]))
    story.append(Paragraph(
        "Records flow EHR → connector → wherever you chose to put them. "
        "The cloud only ever appears as the OAuth provider that proves "
        "you're you. PHI redaction is built in.",
        sty["body"]))

    story.append(PageBreak())

    # ════════════════════════════════════════════════════════════════════════
    # PATH A — NO TERMINAL NEEDED
    # ════════════════════════════════════════════════════════════════════════
    story += _path_banner("PATH A", "No terminal needed", CYAN, sty)

    # ── PATH A · STEP 1 — Connect HealthEx ───────────────────────────────────
    story += step_header("A · 1", "Connect HealthEx (3 minutes)", sty)
    story.append(Paragraph(
        "HealthEx is a free service that connects to the US health-data "
        "exchange networks (Carequality, CommonWell, eHealth Exchange) and "
        "pulls records from any participating EHR — Epic, Cerner, MEDITECH, "
        "athenahealth, AllScripts, and most major US health systems.",
        sty["body"]))

    walk_steps = [
        ("1.", "Go to <b>healthex.io</b> and create a free account."),
        ("2.", "In HealthEx, connect each health system you've used. Each is "
               "a browser SMART-on-FHIR login — same as logging into MyChart."),
        ("3.", "Open <b>claude.ai</b> → <b>Settings</b> → <b>Integrations</b> "
               "→ find <b>HealthEx</b> → click <b>Connect</b>."),
        ("4.", "Authorize once more in the popup. The HealthEx tools are now "
               "live in every Claude conversation you start."),
    ]
    walk_data = [[Paragraph(num, sty["h2"]), Paragraph(text, sty["body"])]
                 for num, text in walk_steps]
    t = Table(walk_data, colWidths=[0.4 * inch, 6.0 * inch])
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(t)

    story.append(Spacer(1, 8))
    story.append(Paragraph("Confirm it worked", sty["h2"]))
    story.append(Paragraph(
        "In a fresh Claude conversation, paste:",
        sty["body"]))
    story.append(_prompt_box("Check when my health records were last updated.", sty))
    story.append(Paragraph(
        "Claude calls the <font face='Courier'>update_and_check_recent_records</font> "
        "tool and reports your sync status. If you see a friendly summary, "
        "you're done with setup — no terminal, no install, no Docker. "
        "Skip to <b>Step A·2</b> for prompts to try next.",
        sty["body"]))

    story.append(Paragraph(
        "<b>Privacy in Path A.</b> HealthEx reads your records read-only — it "
        "<i>cannot write</i> to your EHR. Records arrive in the active Claude "
        "conversation and are gone when you close it. Anthropic doesn't keep "
        "them. Nothing is uploaded anywhere else.",
        sty["callout"]))

    story.append(PageBreak())

    # ── PATH A · STEP 2 — Starter prompts ───────────────────────────────────
    story += step_header("A · 2", "Prompts you can paste right now", sty)
    story.append(Paragraph(
        "Each block below is a complete prompt — copy it into Claude as-is. "
        "Edit anything in [brackets] for your own situation.",
        sty["body"]))

    story.append(Paragraph("Get the lay of the land", sty["h2"]))
    story.append(_prompt_box(
        "Get my health summary. Show me active conditions, current "
        "medications, recent labs, immunizations, and any allergies — "
        "all on one page.", sty))
    story.append(_prompt_box(
        "Build a chronological timeline of my medical conditions from "
        "first documented to present. For each active condition, note "
        "how long I've had it and whether there's documented treatment.",
        sty))

    story.append(Paragraph("Trends over time", sty["h2"]))
    story.append(_prompt_box(
        "Pull my lab results for the last 5 years. Identify any values "
        "that have been trending in a concerning direction, even if "
        "still within the normal range. Flag anything that's been "
        "consistently at the edge of the reference range.", sty))
    story.append(_prompt_box(
        "Compare my most recent labs to my results from 2 years ago. "
        "What has improved? What has gotten worse?", sty))

    story.append(Paragraph("Preventive care & gaps", sty["h2"]))
    story.append(_prompt_box(
        "Based on my age, gender, conditions, and immunization history, "
        "identify any preventive care I may be overdue for. Reference "
        "USPSTF guidelines for screening recommendations.", sty))

    story.append(Paragraph("Pre-appointment prep", sty["h2"]))
    story.append(_prompt_box(
        "I have an appointment with a [cardiologist] on [date] for "
        "[reason]. Pull my relevant history and prepare a 1-page summary "
        "I can bring. Include relevant conditions, current medications, "
        "recent labs, and 3 questions I should ask based on gaps in my "
        "record.", sty))

    story.append(Paragraph("Medication review", sty["h2"]))
    story.append(_prompt_box(
        "Review my medication history for the last 5 years. Identify any "
        "meds that were started then stopped (and why if documented), "
        "any dosage changes over time, and any gaps in chronic "
        "medication coverage.", sty))

    story.append(PageBreak())

    # ── PATH A · STEP 3 — Try the public demo ───────────────────────────────
    story += step_header("A · 3", "Want to see the guardrails? Try the public demo", sty)
    story.append(Paragraph(
        "If you're curious what the HealthClaw guardrails look like in action "
        "— PHI redaction, data-quality flags, audit trails — there's a public "
        "demo you can poke at without installing anything.",
        sty["body"]))

    story.append(Paragraph("In your browser (zero install)", sty["h2"]))
    story.append(Paragraph(
        "Open <b>healthclaw.io/r6-dashboard</b>. You'll see a live, interactive "
        "dashboard pre-seeded with a sample patient (Maria Rivera) whose record "
        "has intentional data-quality issues. Try the Curatr panel — it'll flag "
        "an ICD-9 code that should be ICD-10, and propose the exact fix.",
        sty["body"]))

    story.append(Paragraph("In Claude Desktop (one config-file edit)", sty["h2"]))
    story.append(Paragraph(
        "If you want Claude to talk to the demo over MCP — same way it would "
        "talk to your own self-hosted instance — paste this into your Claude "
        "Desktop config:",
        sty["body"]))
    story.append(code_block(
        "{\n"
        '  "mcpServers": {\n'
        '    "healthclaw-demo": {\n'
        '      "type": "streamable-http",\n'
        '      "url":  "https://healthclaw.up.railway.app/mcp",\n'
        '      "headers": { "X-Tenant-ID": "desktop-demo" }\n'
        "    }\n"
        "  }\n"
        "}", sty))
    story.append(Paragraph(
        "Restart Claude Desktop. Then ask:",
        sty["body"]))
    story.append(_prompt_box(
        "Use the healthclaw-demo tools. Get a step-up token, then search "
        "for all Patients in the demo store. Read the Condition for the "
        "patient, then run curatr_evaluate on it — it has an ICD-9 code "
        "that should be flagged.", sty))

    story.append(Paragraph("That's the whole non-technical path", sty["h2"]))
    story.append(Paragraph(
        "Records connected. Prompts ready. Demo to play with. Zero terminal "
        "commands. If that's enough for what you need — you're done. If you "
        "want a fully-private store on your own machine plus Telegram bots "
        "and an audit trail you control, the rest of this guide is Path B.",
        sty["body"]))

    story.append(Paragraph(
        "<b>Hint:</b> Many people start in Path A, get comfortable, and "
        "graduate to Path B for the records they really care about. "
        "There's no wrong order.",
        sty["callout"]))

    story.append(PageBreak())

    # ════════════════════════════════════════════════════════════════════════
    # PATH B — SELF-HOST THE STACK
    # ════════════════════════════════════════════════════════════════════════
    story += _path_banner("PATH B", "Self-host the full stack", AMBER, sty)

    story += step_header("B · 0", "What you're about to build", sty)
    story.append(Paragraph(
        "By the end of Path B your machine — laptop, Mac mini, Linux box — "
        "will hold a complete, private, agent-mediated view of your records. "
        "Nothing leaves the box unless you say so.",
        sty["body"]))

    story.append(Spacer(1, 8))
    story.append(Paragraph("<b>The stack, top to bottom:</b>", sty["body"]))

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
        ("TEXTCOLOR", (0, 1), (0, 1), CYAN),
        ("TEXTCOLOR", (0, 3), (0, 3), CYAN),
        ("TEXTCOLOR", (0, 5), (0, 5), CYAN),
        ("TEXTCOLOR", (0, 7), (0, 7), CYAN),
    ]))
    story.append(t)

    story.append(Spacer(1, 12))
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

    # ── STEP B · 1a — OpenClaw ───────────────────────────────────────────────
    story += step_header("B · 1a", "Install OpenClaw", sty)
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
    story += step_header("B · 1b", "Stand up an open-source FHIR server", sty)
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
    story += step_header("B · 2", "Connect your real records (privacy-first)", sty)
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
    story += step_header("B · 3", "Install HealthClaw Guardrails", sty)
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
    story += step_header("B · 4", "Pull data through HealthClaw", sty)
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
    story += step_header("B · 5", "Verify everything works", sty)
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
