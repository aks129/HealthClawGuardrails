# HealthClaw 2.0 demo video: the recipe

This folder holds what it takes to rebuild the 88-second demo on
healthclaw.io. The published files live in `static/videos/`:

- `healthclaw-2.0-demo.mp4` (1920×1080, H.264 + AAC, captions burned in)
- `healthclaw-2.0-demo.vtt` (the same captions as a text track)
- `healthclaw-2.0-demo-poster.jpg` (a frame from the diagram beat)

Read `disclosures.md` before you reuse the video anywhere. It lists every
edit and every place where the video is not a plain recording.

## The three-visual rule

Every frame is one of three kinds, and a tag on screen says which:

- **SLIDE** or **DIAGRAM**: an illustration. It shows no real output.
- **CAPTURED**: output copied from a real run. The values come from the run's
  JSON at render time, so nobody types them.
- **RECORDED**: the real CareAgents app, filmed while it ran.

A new shot must fit one of these kinds and carry its tag. Do not mix an
illustration into a frame tagged CAPTURED or RECORDED.

## Regenerating it

`shots.md` is the shot list, with the source of each beat. The scripts
assume they sit in a scratch directory of their own. Copy `scripts/` there
and run everything from that copy, never from inside the repo.

1. Check out the commit you want to film. Start the engine on :5099, then
   CareAgents web on :8600 and `python -m careagents.worker`.
2. Copy `scripts/capture/env.example.sh` to `env.sh` in the scratch copy.
   Fill in local values, then source it. Never commit `env.sh`.
3. Seed synthetic data only: `flask --app main seed-demo --tenant-id
   desktop-demo`, then `seed-demo-history`.
4. Capture: `capture/capture_b4_b5.py` for redaction and audit, then
   `capture/propose_form.py` and `capture/record_careagents.mjs b6` and `b7`.
5. Build: `build/plan.py`, then `render_slides.mjs`, `render_frames.mjs` and
   `render_captions.mjs`, then `build/assemble.py`.

The Node scripts load Playwright from the repo's `e2e/` install, so set
`HC_REPO_ROOT` to your checkout before you run them. The build needs
`ffmpeg` and `pdftoppm`.

## What is not here

The narration audio, the raw frames and most of the run output stayed local.
Only the chat transcripts are kept, in `evidence/`, because `disclosures.md`
promises them. The other `evidence/` files that `shots.md` names were not
committed. The delivered PDF is one of them, since it prints the synthetic
patient's full contact details.
