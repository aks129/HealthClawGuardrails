# HealthClaw 2.0 demo: shot list and sources

## v2 changes (after independent QA, 2026-09-25)

v1 was kept locally as `healthclaw-2.0-demo-v1.mp4` and is not in the repo.

1. **b9 audio.** 0.5 s of silence was inserted into vo_b9 at 6.18 s, between
   "healthclaw dot I O" and "The AI helps" (the file is `build/vo/vo_b9.wav`).
   Cue 23 now ends at 82.65 s, and cue 24 starts at 83.08 s, after the pause
   (82.6–83.05 s, checked on the final mix). The video is 0.5 s longer.
2. **b6 re-recorded, same code (76fff1d), new local account.** The review now
   opens at "Current medications", so the demographics card never appears on
   screen.
3. **b7 chat.** `fix/careagents-chat-markdown` (d6abad1) appeared at
   09:42. I ran CareAgents from it and recorded a fresh take. Both attempts
   hit Gemini rate limits (HTTP 429). Attempt 1 failed before any answer
   (no frames written). Attempt 2 got through the tools and then showed the
   app's "I'm getting more requests than I can answer" message. Neither shows
   an answer, and I did not keep re-rolling, so v2 keeps take 3 with
   "best of 4 takes" in its frame tag. The failed attempts are in
   `evidence/takes/`.
4. **b5.** The fail-closed box now reads "Same read, separate run · copy of
   the same DB, audit table removed".
5. **b6 tag.** It now includes "form proposed by test script".
6. **Tag sizes.** The kind tags are now 23 px, the recording tags 23 px and
   the footers 21 px. The disclosure list is also saved as `disclosures.md`.
7. **b4.** The A1c row now has a sub-label saying how it was fetched:
   `GET Observation?patient=Patient/demo-patient-rivera`, entry
   `demo-obs-a1c`. That is the request the capture really made (a search,
   not a `GET Observation/demo-obs-a1c`), so the label names it exactly.
8. **b7 footer.** The grade came from the in-process test client (its header
   reads `local(test-client) [tenant=conformance-selftest]`), so the footer
   now says "conformance self-test (GET /r6/fhir/$conformance, in-process
   test client)".
9. **b6 PDF.** The Allergies section is ringed on the page, and an enlarged
   crop of the real PDF next to it reads "No known allergies (patient
   confirmed): Yes".

`healthclaw-2.0-demo.mp4` (v2): 88.2 s, 1920×1080, 30 fps, H.264 + AAC, 13.5 MB,
-16.0 LUFS integrated, -1.5 dBTP. Captions are burned in and also provided as
`captions.vtt`. No music.

Everything was captured on 2026-09-25 from a detached worktree of `origin/main`
at **76fff1d**. The local stack was the engine on :5099 (Flask dev server,
SQLite), CareAgents web on :8600 and `python -m careagents.worker`. All of
them are stopped now. Every record on screen is synthetic.

Only three kinds of visual appear, and each frame carries a tag that says
which kind it is:

- **SLIDE** or **DIAGRAM**: an illustration, drawn in the healthclaw.io palette
  and fonts.
- **CAPTURED**: output from a real run. The values are generated from the
  run's JSON, not typed.
- **RECORDED**: real CareAgents UI, filmed from the running app.

| # | Time (s) | Kind | What it shows | Source |
|---|---|---|---|---|
| b1 | 0.0–6.8 | SLIDE | An illustrated health-record card. The values are blurred, and the Name, Medicines and Test results labels light up as the narration names them. | b1.html. Illustration only. The blurred values are the synthetic demo patient's. |
| b2 | 6.8–16.6 | SLIDE | An AI helper reads the record card. Chips show what it can do, then the tags "Useful" and "Also risky". | b2.html |
| b3 | 16.6–26.8 | DIAGRAM | AI helper → HealthClaw (the guard) → "Your record". Two walled-off "Someone else's" records show tenant isolation. The three promises appear inside the guard. | b3.html. `poster.jpg` is taken from this beat. |
| b4 | 26.8–37.2 | CAPTURED | Stored vs returned for synthetic Patient `demo-patient-rivera`. The A1c result (8.1 %) is kept. The name becomes M. E. R. The phone becomes [Redacted]. The address "123 Clinical Ave, Boston, MA, 02101" becomes MA, and the birth date 1985-03-15 becomes 1985. | capture_b4_b5.py read the stored row straight from `r6_resources` in SQLite, then `GET /r6/fhir/Patient/demo-patient-rivera` (200) from the running engine. Output: `evidence/b4_redaction.json`. Seeded with `flask --app main seed-demo --tenant-id desktop-demo` and `seed-demo-history`. |
| b5 | 37.2–43.9 | CAPTURED | The 3 `audit_events` rows the b4 reads wrote (time, action, resource type, outcome; no detail column). Then the same read with the audit table switched off: HTTP 500, no record returned. | `evidence/b5_audit.json`. The fail-closed case came from a second engine on :5098 run against a *copy* of the DB with `audit_events` renamed (b5_noaudit.md). Its log line was `AuditWriteError: audit write failed`, and the 500 body held no patient data. Also shows `tests/test_audit_failure_posture.py`: 4 passed. |
| b6 | 43.9–55.6 | RECORDED + CAPTURED | CareAgents "Waiting for you" lists a proposed Intake form (ringed). The review page follows, opened at Current medications: "Still taking" on the medication, the "No known allergies (patient confirmed)" box ticked, then **Approve & generate** (ringed) and "Review recorded". Last comes page 1 of the PDF that was really delivered (action status: completed), with the Allergies line enlarged. | record_careagents.mjs b6. The form was proposed with no model, the way `tests/test_beta_acceptance_rows.py` does it (`HealthClawClient.start_form_action`, via propose_form.py). The PDF came from `/api/form/<id>`'s `delivery_link`, and page 1 was rasterized with `pdftoppm`. |
| b7 | 55.6–65.8 | CAPTURED, then RECORDED | Live `$conformance`: Grade A, 7/7 properties, plus `test_guardrail_conformance.py` 37 passed and `test_audit_failure_posture.py` 4 passed. Then a CareAgents chat: "What do my blood test results say?" is typed and sent, and a plain-language answer comes back (A1c flagged high; BP and glucose could not be evaluated; ask your clinician). | `evidence/b7_conformance.txt` and `evidence/b7_pytest.txt`, both from the same checkout. The chat is record_careagents.mjs b7, take 3 of 4. The transcript is `evidence/b7_transcript.txt`. |
| b8 | 65.8–76.3 | SLIDE | Status: made-up records; real records invite-only; no clinician sign-off yet. | b8.html |
| b9 | 76.3–88.2 | SLIDE | HealthClaw 2.0 · free · anyone can read the code · healthclaw.io, then "The AI helps. You stay in charge." | b9.html |

## Disclosures: read these before publishing

1. **The b7 chat ran on Gemini 3.5 Flash, not OpenAI or Anthropic.** The repo
   `.env` has no Anthropic key. Its `OPENAI_API_KEY` returned `insufficient_quota`
   (out of credit, so not a rate limit). The chat was recorded through the
   product's own OpenAI-compatible provider setting (`OPENAI_BASE_URL` set to
   Google's endpoint, `CARE_OPENAI_MODEL=gemini-3.5-flash`), using
   `GOOGLE_GEMINI_API_KEY` from the same `.env`. The frame tag says so.
2. **The model's wait was cut.** About 19 s passed between Send and the reply.
   The edit keeps the question being typed and sent, then cuts to the reply
   while it is still typing out. The tag says "reply wait cut".
3. **I picked the best of 4 real takes.** Take 1 used markdown `**` that the UI
   shows as literal asterisks. Takes 2 and 4 did the same. Take 3 did not, so
   it is the one used. All 4 were real runs, and their transcripts are in
   `evidence/takes/`.
4. **Pauses were trimmed and one frame is held in b6.** Idle time on the
   approvals page and the review page was cut. The frame just before the
   Approve click is held for 0.6 s while the ring is drawn on the button. The
   tag says "pauses trimmed".
5. **The "No known allergies" box was ticked by the script, acting as the
   person.** The sample record has no allergy on file. The page never
   pre-checks the box, and approval requires either a confirmed allergy or
   that box, so the scripted person ticks it explicitly. That is the
   attestation path, not an inference.
6. **The PDF still shows the synthetic patient's full name, phone and address,
   at thumbnail size.** It is the person's own form. In v2 the review page is
   shown from Current medications down, so its demographics card is not on
   screen.
7. **Captions spell two phrases the way they are read.** "two point oh" is
   captioned as "2.0", and "healthclaw dot I O" as "healthclaw.io". No other
   narration word changed.
8. **The sign-in page is not shown.** The brief's stack notes asked for the
   sign-in page followed by a cut to the signed-in app. b6's shot list and its
   ~10 s beat had no room for it, so it was dropped. A real screenshot of it
   exists locally (`capture/signin.png`) and is not in the cut.
9. **b6 and b7 use different sign-ins.** The b6 recording and chat take 3 each
   used a fresh local account (both `@example.test`, codes from the
   dev mail-stub log), so neither conversation carried history.

## How it was built

- The slides are HTML in : `card.css` restyled onto
  `static/css/healthclaw.css` tokens, with Archivo and Fragment Mono copied
  from `static/fonts`. They are rendered one frame at a time
  (render_slides.mjs calls `render(t)` and then takes a screenshot),
  so the slow push and the reveals are frame-exact. The b4/b5/b7 values are
  read from the capture JSON at render time.
- The recordings drive Playwright (record_careagents.mjs) against the
  live stack. Viewport 1280×720, deviceScaleFactor 2, slowMo 120.
- The ffmpeg here has no drawtext or libass, so captions are transparent PNG
  overlays (render_captions.mjs). Each cue is one line, which is under
  the two-line limit.
- assemble.py handles timing (plan.py), the zoom and click
  rings on the recordings, 0.35 s crossfades, the VO on an absolute timeline,
  two-pass loudnorm and the x264 `+faststart` encode.

### One deviation from the brief

The brief asked for Playwright `recordVideo`. It records **CSS pixels**: a
1280×720 viewport records at 1280×720 whatever deviceScaleFactor is set, and
a larger `size` only pads the frame (I tested this). So the recordings use the
same Chromium screencast that Playwright uses internally, asked for **device
pixels**: 2560×1440 JPEG frames, each with its own timestamp, turned into
30 fps video by `assemble.py`. Nothing is drawn onto the frames except the
click rings, which are added in the edit. `recordVideo` at 1920×1080 was also
tried first, and it looked soft at the zoom this cut uses.
