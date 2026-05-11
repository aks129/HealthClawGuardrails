#!/usr/bin/env python3
"""Build docs/healthclaw-devpost.pdf — submission artifact for the
PromptOpinion "Agents Assemble Challenge" on Devpost.

    uv run python scripts/build_devpost_pdf.py

Style mirrors scripts/build_quickstart_pdf.py — same palette, same
typography, same paper size — so a judge sees a consistent brand
across both PDFs.
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
    Preformatted,
    Spacer,
    Table,
    TableStyle,
)


# ── Brand palette (matches build_quickstart_pdf.py) ────────────────────────
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


def _draw_cover_bg(canvas, doc):
    canvas.saveState()
    canvas.setFillColor(NAVY)
    canvas.rect(0, 0, LETTER[0], LETTER[1], fill=1, stroke=0)
    # Cyan accent band
    canvas.setFillColor(CYAN)
    canvas.rect(0, LETTER[1] - 0.6 * inch, LETTER[0], 0.08 * inch, fill=1, stroke=0)
    # Amber accent band
    canvas.setFillColor(AMBER)
    canvas.rect(0, 0.5 * inch, LETTER[0], 0.04 * inch, fill=1, stroke=0)
    canvas.restoreState()


def _draw_body_bg(canvas, doc):
    canvas.saveState()
    canvas.setFillColor(PAPER)
    canvas.rect(0, 0, LETTER[0], LETTER[1], fill=1, stroke=0)
    # Header rule
    canvas.setStrokeColor(RULE)
    canvas.setLineWidth(0.5)
    canvas.line(0.75 * inch, LETTER[1] - 0.55 * inch,
                LETTER[0] - 0.75 * inch, LETTER[1] - 0.55 * inch)
    canvas.setFillColor(SLATE)
    canvas.setFont("Helvetica", 8)
    canvas.drawString(0.75 * inch, LETTER[1] - 0.42 * inch,
                      "HealthClaw Guardrails · Agents Assemble Challenge")
    canvas.drawRightString(LETTER[0] - 0.75 * inch, LETTER[1] - 0.42 * inch,
                           "healthclaw.io")
    canvas.setFillColor(SLATE_LIGHT)
    canvas.drawCentredString(LETTER[0] / 2, 0.4 * inch,
                             f"Page {doc.page}")
    canvas.restoreState()


def build():
    out_dir = Path(__file__).resolve().parent.parent / "docs"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "healthclaw-devpost.pdf"

    doc = BaseDocTemplate(
        str(out_path),
        pagesize=LETTER,
        leftMargin=0.75 * inch,
        rightMargin=0.75 * inch,
        topMargin=0.75 * inch,
        bottomMargin=0.75 * inch,
        title="HealthClaw Guardrails — Devpost Submission",
        author="fhiriq",
        subject="PromptOpinion Agents Assemble Challenge",
    )

    cover_frame = Frame(0, 0, LETTER[0], LETTER[1],
                        leftPadding=0.75 * inch, rightPadding=0.75 * inch,
                        topPadding=0.75 * inch, bottomPadding=0.75 * inch,
                        showBoundary=0)
    body_frame = Frame(0.75 * inch, 0.75 * inch,
                       LETTER[0] - 1.5 * inch, LETTER[1] - 1.6 * inch,
                       showBoundary=0)
    doc.addPageTemplates([
        PageTemplate(id="cover", frames=[cover_frame], onPage=_draw_cover_bg),
        PageTemplate(id="body", frames=[body_frame], onPage=_draw_body_bg),
    ])

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "title", parent=styles["Title"], fontName="Helvetica-Bold",
        fontSize=32, leading=38, textColor=colors.white, alignment=TA_LEFT,
        spaceAfter=8,
    )
    subtitle_style = ParagraphStyle(
        "subtitle", parent=styles["Normal"], fontName="Helvetica",
        fontSize=14, leading=20, textColor=CYAN, alignment=TA_LEFT,
        spaceAfter=24,
    )
    tagline_style = ParagraphStyle(
        "tagline", parent=styles["Normal"], fontName="Helvetica",
        fontSize=11, leading=17, textColor=colors.HexColor("#cbd5e1"),
        alignment=TA_LEFT, spaceAfter=8,
    )
    cover_footer = ParagraphStyle(
        "cover_footer", parent=styles["Normal"], fontName="Helvetica",
        fontSize=10, leading=14, textColor=colors.HexColor("#94a3b8"),
        alignment=TA_LEFT,
    )
    h1 = ParagraphStyle(
        "h1", parent=styles["Heading1"], fontName="Helvetica-Bold",
        fontSize=20, leading=24, textColor=NAVY, spaceBefore=4, spaceAfter=10,
    )
    h2 = ParagraphStyle(
        "h2", parent=styles["Heading2"], fontName="Helvetica-Bold",
        fontSize=13, leading=18, textColor=CYAN_DIM, spaceBefore=16, spaceAfter=6,
    )
    body = ParagraphStyle(
        "body", parent=styles["Normal"], fontName="Helvetica",
        fontSize=10, leading=14, textColor=INK, spaceAfter=8,
    )
    bullet = ParagraphStyle(
        "bullet", parent=body, leftIndent=14, bulletIndent=2,
        spaceAfter=3,
    )
    code = ParagraphStyle(
        "code", parent=styles["Code"], fontName="Courier",
        fontSize=8, leading=10, textColor=INK, backColor=CODE_BG,
        leftIndent=8, rightIndent=8, spaceBefore=4, spaceAfter=10,
    )

    story = []

    # ── Cover ──────────────────────────────────────────────────────────────
    story.append(Spacer(1, 2.6 * inch))
    story.append(Paragraph("HealthClaw<br/>Guardrails", title_style))
    story.append(Paragraph(
        "SHARP-on-MCP compliance layer for AI agents accessing FHIR data",
        subtitle_style,
    ))
    story.append(Spacer(1, 0.25 * inch))
    story.append(Paragraph(
        "Submitted to the <b>PromptOpinion Agents Assemble Challenge</b>",
        tagline_style,
    ))
    story.append(Paragraph(
        "Build Interoperable Healthcare Agents at the Intersection of MCP, A2A &amp; FHIR",
        tagline_style,
    ))
    story.append(Spacer(1, 1.8 * inch))
    story.append(Paragraph(
        f"A project of <b>fhiriq</b><br/>"
        f"healthclaw.io · github.com/aks129/HealthClawGuardrails<br/>"
        f"{date.today().strftime('%B %Y')}",
        cover_footer,
    ))

    story.append(NextPageTemplate("body"))
    story.append(PageBreak())

    # ── Page 2: Links + problem ────────────────────────────────────────────
    story.append(Paragraph("Links", h1))
    link_data = [
        ["Marketplace — Agent",
         "app.promptopinion.ai/marketplace/agent/019e183d…"],
        ["Marketplace — MCP Superpower",
         "app.promptopinion.ai/marketplace/mcp/019e1831…"],
        ["Demo video (under 3 min)", "youtu.be/2fVL28CW9p8"],
        ["Source code", "github.com/aks129/HealthClawGuardrails"],
        ["Live MCP endpoint", "mcp-server-production-5112.up.railway.app/mcp"],
        ["Marketing + skills site", "healthclaw.io"],
    ]
    t = Table(link_data, colWidths=[2.4 * inch, 4.5 * inch])
    t.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("TEXTCOLOR", (0, 0), (0, -1), SLATE),
        ("TEXTCOLOR", (1, 0), (1, -1), INK),
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, -2), 0.25, RULE),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(t)

    story.append(Paragraph("Problem", h1))
    story.append(Paragraph(
        "Every health system in the country is running AI experiments. "
        "Almost none have agents touching production charts. The blocker "
        "isn't capability — it's compliance. The moment a model sees a "
        "name, an MRN, or a date of birth, that conversation is governed "
        "by HIPAA, every state's analog, and the organization's BAA stack. "
        "Projects stall at <i>we can't let the agent touch real data</i>.",
        body,
    ))
    story.append(Paragraph(
        "The status quo gives three bad options:",
        body,
    ))
    for line in [
        "<b>Build per-EHR.</b> Re-implement guardrails in every Epic, Cerner, "
        "MEDITECH, athenahealth, eClinicalWorks deployment.",
        "<b>Anonymize upstream.</b> Ship a one-way de-identification pipeline "
        "that destroys the round-trip — fine for analytics, broken for agentic "
        "workflows that need to read <i>and</i> write back.",
        "<b>Trust the model.</b> Include \"do not output PHI\" in the system "
        "prompt and hope for the best. This is the current default. It is "
        "not auditable.",
    ]:
        story.append(Paragraph("• " + line, bullet))

    story.append(Paragraph(
        "HealthClaw makes the right thing the default.",
        body,
    ))

    # ── Page 3: What it is ─────────────────────────────────────────────────
    story.append(PageBreak())
    story.append(Paragraph("What it is", h1))
    story.append(Paragraph(
        "HealthClaw is an MCP server (Superpower) plus an A2A agent that "
        "sits between any AI agent and any SMART-on-FHIR endpoint. The agent "
        "host obtains a SMART access token; HealthClaw forwards it on every "
        "call via <font face='Courier'>X-FHIR-Server-URL</font>, "
        "<font face='Courier'>X-FHIR-Access-Token</font>, "
        "<font face='Courier'>X-Patient-ID</font> headers, routes the request "
        "to the correct upstream EHR, applies the full guardrail stack on the "
        "response, and only then returns data to the model.",
        body,
    ))
    story.append(Paragraph(
        "The same deployment works against Epic, Cerner, MEDITECH, athenahealth, "
        "eClinicalWorks, HAPI, SMART Health IT — no per-EHR code, no per-customer "
        "rebuild. That portability comes from compliance with two open specs:",
        body,
    ))
    story.append(Paragraph(
        "• <b>SHARP-on-MCP</b> (sharponmcp.com) — vendor-neutral header-forwarding "
        "contract advertised under <font face='Courier'>capabilities.experimental"
        "</font>",
        bullet,
    ))
    story.append(Paragraph(
        "• <b>PromptOpinion FHIR Extension</b> — advertised under <font face='Courier'>"
        "capabilities.extensions[\"ai.promptopinion/fhir-context\"]</font> with a "
        "SMART-on-FHIR scope manifest "
        "(<font face='Courier'>patient/*.read</font> required, "
        "<font face='Courier'>patient/*.write</font> and "
        "<font face='Courier'>offline_access</font> optional)",
        bullet,
    ))
    story.append(Paragraph(
        "Both specs declare the same headers and the same scope model, so a "
        "single MCP server satisfies both ecosystems.",
        body,
    ))

    story.append(Paragraph("What every response gets", h1))
    story.append(Paragraph("<b>On every read</b>", h2))
    for line in [
        "PHI redaction — names → initials, identifiers masked, addresses "
        "stripped, birth dates truncated to year, photos removed",
        "Immutable <font face='Courier'>AuditEvent</font> appended to a "
        "tenant-scoped, append-only trail",
        "Medical disclaimer injected on clinical resources",
        "Upstream URLs rewritten so the source EHR never leaks into the response",
    ]:
        story.append(Paragraph("• " + line, bullet))

    story.append(Paragraph("<b>On every write</b>", h2))
    for line in [
        "HMAC-SHA256 step-up tokens with 128-bit nonce and 5-minute TTL",
        "Human-in-the-loop gate on clinical resources "
        "(HTTP 428 until <font face='Courier'>X-Human-Confirmed</font>)",
        "Local <font face='Courier'>$validate</font> runs before commit",
        "<font face='Courier'>ETag</font> / <font face='Courier'>If-Match</font> "
        "concurrency control",
    ]:
        story.append(Paragraph("• " + line, bullet))

    story.append(Paragraph("<b>Always</b>", h2))
    for line in [
        "Tenant isolation enforced at the database layer (local mode) or "
        "propagated as a guardrail header (proxy mode)",
        "OAuth 2.1 + PKCE (S256), dynamic client registration, token revocation",
    ]:
        story.append(Paragraph("• " + line, bullet))

    # ── Page 4: AI factor / impact / feasibility ───────────────────────────
    story.append(PageBreak())
    story.append(Paragraph("Why this needs generative AI", h1))
    story.append(Paragraph(
        "Three places where generative AI does what conditional logic can't:",
        body,
    ))
    story.append(Paragraph(
        "<b>1. Tool selection under uncertainty.</b> A clinical question "
        "doesn't map cleanly to one endpoint. <i>What's this patient's recent "
        "diabetes control look like?</i> turns into "
        "<font face='Courier'>fhir_search</font>(Condition, code=diabetes) → "
        "<font face='Courier'>fhir_lastn</font>(Observation, code=HbA1c) → "
        "<font face='Courier'>fhir_stats</font>(Observation, code=glucose) → "
        "narrative synthesis. The agent does the planning; HealthClaw does "
        "the policy.",
        body,
    ))
    story.append(Paragraph(
        "<b>2. Curatr semantic quality checks.</b> A smoking-status field "
        "set to <i>current smoker</i> combined with a clinical note saying "
        "<i>patient denies tobacco use</i> is a contradiction no validator "
        "catches with conformance rules. The agent reads both, flags the "
        "inconsistency, and (with step-up + HITL) proposes a Provenance-linked "
        "fix.",
        body,
    ))
    story.append(Paragraph(
        "<b>3. Guardrail narration.</b> The demo agent doesn't just retrieve "
        "data; it points out <i>what HealthClaw did</i> to each response — "
        "<i>\"the patient's name has been truncated to initials per HealthClaw's "
        "HIPAA Safe Harbor de-identification\"</i> — making the compliance layer "
        "visible to clinical reviewers in a way pure log lines can't.",
        body,
    ))

    story.append(Paragraph("Potential impact", h1))
    story.append(Paragraph(
        "PHI exposure is THE blocker for clinical AI deployment in 2026. "
        "Every CIO survey says it; every pilot that doesn't ship cites it. "
        "HealthClaw is a deployable architectural pattern that converts the "
        "blocker into a configuration choice. Once a health system trusts the "
        "redaction + audit + HITL guarantees, the conversation moves from "
        "<i>can we let the agent see this?</i> to <i>which tools should we "
        "enable?</i>",
        body,
    ))
    story.append(Paragraph(
        "Because the server is SHARP-on-MCP + PromptOpinion compliant rather "
        "than EHR-specific, a single HealthClaw deployment can sit in front of "
        "an entire health system's SMART-launched agent ecosystem — a 50× "
        "reduction in per-vendor compliance work versus the per-EHR alternative.",
        body,
    ))
    story.append(Paragraph(
        "The pattern is also reusable beyond healthcare. Any regulated domain "
        "with similar boundary requirements — GLBA financial records, FERPA "
        "education records, government CUI — fits the same shape: agent host "
        "forwards an access token, server applies policy on the response, "
        "audit trail emitted.",
        body,
    ))

    # ── Page 5: Feasibility ────────────────────────────────────────────────
    story.append(PageBreak())
    story.append(Paragraph("Feasibility", h1))
    story.append(Paragraph(
        "This is not a slide-deck submission. The full stack is deployed today:",
        body,
    ))
    feas_data = [
        ["Flask app", "Railway · app.healthclaw.io · FHIR REST facade, OAuth 2.1, audit, redaction, Curatr"],
        ["MCP server", "Railway · mcp-server-production-5112.up.railway.app · Streamable HTTP + SSE + JSON-RPC bridge"],
        ["OpenClaw stack", "Telegram personas (Sally-PCP, Mary-pharmacy, Dom-fitness, Kristy-scheduler)"],
        ["Marketing site", "Vercel · healthclaw.io · skills catalogue + quickstart PDF"],
    ]
    t = Table(feas_data, colWidths=[1.5 * inch, 5.4 * inch])
    t.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("TEXTCOLOR", (0, 0), (0, -1), CYAN_DIM),
        ("TEXTCOLOR", (1, 0), (1, -1), INK),
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BACKGROUND", (0, 0), (-1, -1), colors.white),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("LINEBELOW", (0, 0), (-1, -2), 0.25, RULE),
    ]))
    story.append(t)
    story.append(Spacer(1, 0.15 * inch))

    story.append(Paragraph("<b>Test coverage</b>: 516 Python tests + 49 Node tests, all passing. "
                           "TypeScript strict-mode <font face='Courier'>tsc --noEmit</font> "
                           "clean. End-to-end gate script "
                           "(<font face='Courier'>scripts/demo_e2e.sh</font>) covers 10 "
                           "compliance gates: liveness → seed → read-with-redaction → "
                           "audit trail → cross-tenant isolation → Curatr evaluate → "
                           "human-in-the-loop.", body))

    story.append(Paragraph("<b>Compliance posture</b>:", body))
    for line in [
        "HIPAA Safe Harbor de-identification on by default; patient-controlled mode preserves selected fields",
        "SOC2-aligned audit trail with database-level immutability",
        "HITRUST-aligned tenant isolation",
        "<font face='Courier'>.claude/compliance/{hipaa,soc2,hitrust}.md</font> "
        "gate checklists committed to the repo",
    ]:
        story.append(Paragraph("• " + line, bullet))

    story.append(Paragraph(
        "<b>Demo data is synthetic.</b> The <font face='Courier'>desktop-demo"
        "</font> tenant is seeded with a Grover Keeling sample record on first "
        "boot. No real PHI was used in any test, screenshot, or video.",
        body,
    ))

    story.append(Paragraph("Architecture", h1))
    arch = (
        "Agent Host  (PromptOpinion, SMART launcher, ...)\n"
        "  obtains SMART-on-FHIR access token\n"
        "         |\n"
        "         |  X-FHIR-Server-URL\n"
        "         |  X-FHIR-Access-Token\n"
        "         |  X-Patient-ID\n"
        "         v\n"
        "MCP Server  (Node.js + TypeScript)\n"
        "  /mcp       Streamable HTTP   /sse  SSE   /mcp/rpc  JSON-RPC\n"
        "  Advertises SHARP + PromptOpinion FHIR extension\n"
        "         |   headers forwarded\n"
        "         v\n"
        "Flask Guardrail Layer  (Python)\n"
        "  SHARP per-request proxy   PHI redaction   AuditEvent\n"
        "  Step-up verification      HITL gate       Tenant isolation\n"
        "  URL rewriting             Curatr engine\n"
        "         |   per-request upstream\n"
        "         v\n"
        "Upstream FHIR Server\n"
        "  Epic | Cerner | MEDITECH | athenahealth | eClinicalWorks\n"
        "  HAPI | SMART Health IT | ..."
    )
    story.append(Preformatted(arch, code))

    # ── Page 6: Tools + tech stack + what's next ───────────────────────────
    story.append(PageBreak())
    story.append(Paragraph("Tool catalog", h1))
    tools = [
        ["Read tier",
         "context_get, fhir_read, fhir_search, fhir_validate, fhir_stats, "
         "fhir_lastn, fhir_permission_evaluate, fhir_subscription_topics, "
         "curatr_evaluate"],
        ["Write tier (step-up)",
         "fhir_propose_write, fhir_commit_write, curatr_apply_fix"],
        ["Utility",
         "fhir_get_token, fhir_seed"],
    ]
    t = Table(tools, colWidths=[1.5 * inch, 5.4 * inch])
    t.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("TEXTCOLOR", (0, 0), (0, -1), CYAN_DIM),
        ("TEXTCOLOR", (1, 0), (1, -1), INK),
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("LINEBELOW", (0, 0), (-1, -2), 0.25, RULE),
    ]))
    story.append(t)
    story.append(Spacer(1, 0.1 * inch))
    story.append(Paragraph(
        "Coverage spans FHIR R4 US Core v9 stable resources "
        "(AllergyIntolerance, Immunization, MedicationRequest, Procedure, "
        "DiagnosticReport, Coverage, ServiceRequest, Goal, CarePlan, Patient, "
        "Encounter, Observation, Condition, ...) and FHIR R6 ballot3 "
        "experimental resources (Permission, SubscriptionTopic, DeviceAlert, "
        "NutritionIntake).",
        body,
    ))

    story.append(Paragraph("Tech stack", h1))
    stack = [
        ["Backend",
         "Python 3.11+ (Flask, SQLAlchemy, httpx, gunicorn), "
         "Node.js 20 + TypeScript (MCP SDK, Express)"],
        ["Specs",
         "MCP 2024-11-05, SHARP-on-MCP 1.0, PromptOpinion FHIR Context ext, "
         "SMART-on-FHIR, FHIR R4 US Core v9, FHIR R6 ballot3, OAuth 2.1 + PKCE"],
        ["Infrastructure",
         "Railway (Flask + MCP + Postgres + Redis), Vercel (marketing), GitHub Actions"],
        ["Storage",
         "SQLite default · PostgreSQL on Railway · Redis (rate-limit, sessions, token cache)"],
        ["Testing",
         "pytest (516), Jest (49), Playwright (browser e2e), demo_e2e.sh (10 gates)"],
    ]
    t = Table(stack, colWidths=[1.2 * inch, 5.7 * inch])
    t.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("TEXTCOLOR", (0, 0), (0, -1), SLATE),
        ("TEXTCOLOR", (1, 0), (1, -1), INK),
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("LINEBELOW", (0, 0), (-1, -2), 0.25, RULE),
    ]))
    story.append(t)

    story.append(Paragraph("What's next", h1))
    for line in [
        "Strict StructureDefinition + terminology binding validation (currently structural only)",
        "SubscriptionTopic notification dispatch (currently storage + discovery)",
        "Cryptographic human-in-the-loop confirmation (currently header-based)",
        "Cross-version translation for R5/R6 upstreams (currently pass-through)",
        "Provider Directory de-duplication on the upstream proxy path",
    ]:
        story.append(Paragraph("• " + line, bullet))

    story.append(Spacer(1, 0.3 * inch))
    story.append(Paragraph(
        "Open source. github.com/aks129/HealthClawGuardrails · healthclaw.io",
        ParagraphStyle("repo", parent=body, alignment=TA_CENTER,
                       textColor=CYAN_DIM, fontName="Helvetica-Bold"),
    ))
    story.append(Paragraph(
        "A project of fhiriq.",
        ParagraphStyle("fhiriq", parent=body, alignment=TA_CENTER,
                       textColor=SLATE),
    ))

    doc.build(story)
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    build()
