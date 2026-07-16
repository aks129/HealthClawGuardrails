#!/usr/bin/env python3
"""LinkedIn digest — draft a post from recently merged PRs, deliver it for
human approval, and (only on an explicit approve step) publish it.

Design (human-gated by default, fitting a health-data brand):

    draft   gather merged PRs since a date/last-run → write a LinkedIn post →
            email it to you (Resend) AND save it to a pending file. Automatic;
            run it on a schedule (e.g. weekly cron).
    post    publish an approved pending file to LinkedIn. This IS the approval
            action — you run it after reading the draft. Posts via the LinkedIn
            API when LINKEDIN_ACCESS_TOKEN is set; otherwise prints the final
            text for manual paste and exits non-zero so nothing is lost.

Nothing publishes without a human running `post`. No PHI is ever involved —
inputs are public PR titles/numbers only.

Env:
  GITHUB_REPO            owner/name (default: aks129/HealthClawGuardrails)
  ANTHROPIC_API_KEY      optional — LLM drafting; falls back to a clean template
  CARE_MODEL             Anthropic model (default: claude-sonnet-5)
  RESEND_API_KEY         optional — email delivery of the draft
  DIGEST_EMAIL_TO        where to email drafts (default: repo owner via env)
  DIGEST_EMAIL_FROM      verified Resend sender (default: HealthClaw updates addr)
  LINKEDIN_ACCESS_TOKEN  w_member_social OAuth token (required only for `post`)
  LINKEDIN_AUTHOR_URN    e.g. "urn:li:person:XXXX" (required only for `post`)

Usage:
  python scripts/linkedin_digest.py draft [--since 2026-07-01] [--dry-run]
  python scripts/linkedin_digest.py post  [<pending-file>]
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import requests

REPO = os.environ.get("GITHUB_REPO", "aks129/HealthClawGuardrails")
PENDING_DIR = os.environ.get(
    "DIGEST_PENDING_DIR",
    os.path.join(os.path.dirname(__file__), "..", ".digest"))
MAX_PRS = 25
HTTP_TIMEOUT = 20


# --- gather ------------------------------------------------------------------

def merged_prs_since(since_iso: str) -> list[dict]:
    """Merged PRs with mergedAt >= since_iso (UTC date or datetime), via gh."""
    out = subprocess.run(
        ["gh", "pr", "list", "--repo", REPO, "--state", "merged",
         "--limit", "100", "--json", "number,title,mergedAt,author,url"],
        capture_output=True, text=True, check=True).stdout
    since = _parse_iso(since_iso)
    prs = []
    for pr in json.loads(out):
        merged = pr.get("mergedAt")
        if merged and _parse_iso(merged) >= since:
            prs.append(pr)
    prs.sort(key=lambda p: p["mergedAt"])
    return prs[:MAX_PRS]


def _parse_iso(s: str) -> datetime:
    s = s.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        dt = datetime.strptime(s[:10], "%Y-%m-%d")
    return dt.replace(tzinfo=dt.tzinfo or timezone.utc)


# --- draft -------------------------------------------------------------------

SYSTEM = (
    "You write concise, credible LinkedIn posts for HealthClaw Guardrails — an "
    "open-source safety layer between AI agents and FHIR health data. Voice: "
    "engineer-to-engineer, specific, no hype, no emoji spam. 120-200 words. "
    "Lead with the concrete thing that shipped and why it matters for safe "
    "AI + health data. End with a soft CTA to careagents.cloud or the GitHub "
    "repo. Never invent facts beyond the PR titles provided; never mention PHI."
)


def draft_post(prs: list[dict]) -> str:
    lines = "\n".join(f"- #{p['number']} {p['title']}" for p in prs)
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if key:
        try:
            return _draft_llm(key, lines)
        except Exception as exc:  # fall back rather than fail the whole run
            print(f"[warn] LLM draft failed ({type(exc).__name__}); "
                  "using template", file=sys.stderr)
    return _draft_template(prs, lines)


def _draft_llm(key: str, pr_lines: str) -> str:
    model = os.environ.get("CARE_MODEL", "claude-sonnet-5")
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json={"model": model, "max_tokens": 600, "system": SYSTEM,
              "messages": [{"role": "user", "content":
                            "Merged this cycle:\n" + pr_lines +
                            "\n\nWrite the LinkedIn post."}]},
        timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return "".join(b.get("text", "") for b in r.json()["content"]).strip()


def _draft_template(prs: list[dict], pr_lines: str) -> str:
    return (
        f"This cycle on HealthClaw Guardrails — the open-source safety layer "
        f"between AI agents and FHIR health data.\n\n"
        f"Highlights ({len(prs)} PRs merged):\n{pr_lines}\n\n"
        f"Every read is redacted, every write needs step-up plus human "
        f"sign-off, and everything is audited — enforced server-side so a "
        f"client can't bypass it.\n\n"
        f"See it working in CareAgents → https://careagents.cloud · "
        f"Code: https://github.com/{REPO}")


# --- deliver / persist -------------------------------------------------------

def save_pending(text: str, prs: list[dict]) -> str:
    os.makedirs(PENDING_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = os.path.join(PENDING_DIR, f"linkedin-{stamp}.md")
    meta = f"<!-- prs: {','.join(str(p['number']) for p in prs)} -->\n"
    with open(path, "w") as f:
        f.write(meta + text + "\n")
    return path


def email_draft(text: str, prs: list[dict]) -> bool:
    key = os.environ.get("RESEND_API_KEY", "").strip()
    to = os.environ.get("DIGEST_EMAIL_TO", "").strip()
    if not key or not to:
        print("[info] RESEND_API_KEY/DIGEST_EMAIL_TO not set — draft not "
              "emailed (saved to pending file only)", file=sys.stderr)
        return False
    sender = os.environ.get("DIGEST_EMAIL_FROM",
                            "HealthClaw <updates@healthclaw.io>")
    body = ("<p>Draft LinkedIn post from "
            f"{len(prs)} merged PRs. Review, then approve by running "
            "<code>python scripts/linkedin_digest.py post</code>.</p>"
            f"<pre style='white-space:pre-wrap;font-family:inherit'>{text}</pre>")
    r = requests.post("https://api.resend.com/emails",
                      headers={"Authorization": f"Bearer {key}"},
                      json={"from": sender, "to": [to],
                            "subject": "LinkedIn draft — approve to publish",
                            "html": body}, timeout=HTTP_TIMEOUT)
    ok = r.status_code in (200, 201)
    if not ok:
        print(f"[warn] Resend returned {r.status_code}", file=sys.stderr)
    return ok


# --- publish -----------------------------------------------------------------

def post_to_linkedin(text: str) -> bool:
    token = os.environ.get("LINKEDIN_ACCESS_TOKEN", "").strip()
    author = os.environ.get("LINKEDIN_AUTHOR_URN", "").strip()
    if not token or not author:
        print("\n--- LinkedIn not configured — copy/paste this post ---\n",
              file=sys.stderr)
        print(text)
        print("\n[blocked] Set LINKEDIN_ACCESS_TOKEN + LINKEDIN_AUTHOR_URN to "
              "auto-publish.", file=sys.stderr)
        return False
    r = requests.post(
        "https://api.linkedin.com/v2/ugcPosts",
        headers={"Authorization": f"Bearer {token}",
                 "X-Restli-Protocol-Version": "2.0.0",
                 "Content-Type": "application/json"},
        json={
            "author": author,
            "lifecycleState": "PUBLISHED",
            "specificContent": {"com.linkedin.ugc.ShareContent": {
                "shareCommentary": {"text": text},
                "shareMediaCategory": "NONE"}},
            "visibility": {
                "com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"}},
        timeout=HTTP_TIMEOUT)
    if r.status_code not in (200, 201):
        print(f"[error] LinkedIn API {r.status_code}: {r.text[:200]}",
              file=sys.stderr)
        return False
    print("[ok] Published to LinkedIn.")
    return True


# --- commands ----------------------------------------------------------------

def cmd_draft(args) -> int:
    since = args.since or (
        datetime.now(timezone.utc) - timedelta(days=args.days)
    ).strftime("%Y-%m-%d")
    prs = merged_prs_since(since)
    if not prs:
        print(f"No PRs merged since {since}. Nothing to draft.")
        return 0
    text = draft_post(prs)
    print(f"\n=== DRAFT ({len(prs)} PRs since {since}) ===\n{text}\n")
    if args.dry_run:
        return 0
    path = save_pending(text, prs)
    print(f"[saved] {path}")
    email_draft(text, prs)
    print("Approve by running: python scripts/linkedin_digest.py post")
    return 0


def cmd_post(args) -> int:
    path = args.file
    if not path:
        pend = sorted(glob.glob(os.path.join(PENDING_DIR, "linkedin-*.md")))
        if not pend:
            print("No pending draft. Run `draft` first.", file=sys.stderr)
            return 1
        path = pend[-1]
    with open(path) as f:
        text = "".join(ln for ln in f if not ln.startswith("<!--")).strip()
    ok = post_to_linkedin(text)
    if ok:
        done = path.replace(".md", ".posted.md")
        os.rename(path, done)
        print(f"[archived] {done}")
    return 0 if ok else 2


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("draft", help="draft a post from merged PRs")
    d.add_argument("--since", help="ISO date; default: --days ago")
    d.add_argument("--days", type=int, default=7)
    d.add_argument("--dry-run", action="store_true",
                   help="print the draft; don't save/email")
    d.set_defaults(func=cmd_draft)
    po = sub.add_parser("post", help="publish an approved draft to LinkedIn")
    po.add_argument("file", nargs="?", help="pending file; default: newest")
    po.set_defaults(func=cmd_post)
    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
