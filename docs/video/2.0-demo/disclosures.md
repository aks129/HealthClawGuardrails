# HealthClaw 2.0 demo video: how it was made

You can repeat this list word for word on the site or in the README.

- **Every record is made up.** The patient in the video is a synthetic demo
  record. No real person's data appears.
- **Three kinds of picture, each labelled on screen.** SLIDE and DIAGRAM mean
  an illustration. CAPTURED means output copied from a real run. RECORDED
  means the real CareAgents app, filmed while it ran on a local machine from
  the code on `main` (commit 76fff1d).
- **The redaction and audit screens are real output.** The stored record was
  read straight from the database and set next to what the API returned for
  the same record. The audit rows are the ones those reads wrote. The
  "audit log switched off" result came from a separate run against a copy of
  the same database with the audit table removed. That read failed with
  HTTP 500 and returned no record.
- **The form was proposed by a test script, not by the AI.** The script calls
  the same function the AI's tool uses. Everything after that is the real app:
  the list of waiting requests, the review page, Approve, and the PDF that was
  delivered. Pauses were trimmed, and the review page is shown from
  "Current medications" down.
- **"No known allergies" was ticked on camera by the person's scripted
  clicks.** The page never ticks this box for you, and approval needs either a
  confirmed allergy or this box. The sample record has no allergy on file.
- **The chat used Google's Gemini 3.5 Flash model.** It was reached through
  CareAgents' standard OpenAI-compatible setting, because the OpenAI key we
  had was out of credit. The wait for the reply was cut. The answer shown is the best of four real takes:
  three of the four showed markdown symbols (`**`) as plain text, and we
  used the one that did not. All four transcripts are published with the
  video.
- **The test results are real runs.** The Grade A score is the built-in
  conformance self-test, run against the local engine in-process. The test
  counts are from pytest on the same commit.
- **Two captions differ from the spoken words.** "two point oh" is written as
  "2.0", and "healthclaw dot I O" as "healthclaw.io".
- **What this is not.** Real records are invite-only for now, and no clinician
  has signed off on the medical parts.
