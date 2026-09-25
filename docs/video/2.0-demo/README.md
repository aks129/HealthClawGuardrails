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

`shots.md` is the shot list, with the source of each beat. The capture and
build scripts are one-off production tooling, so they are kept with the
source files outside the repo, not here. The steps are:

1. Check out the commit you want to film and seed synthetic data only.
2. Capture the real output and record the real app, one beat at a time.
3. Render the slides, time each beat to its voiceover, and assemble.
4. Run an independent QA pass before publishing.

## What is not here

The scripts, the narration audio, the raw frames and most of the run output stayed local.
Only the chat transcripts are kept, in `evidence/`, because `disclosures.md`
promises them. The other `evidence/` files that `shots.md` names were not
committed. The delivered PDF is one of them, since it prints the synthetic
patient's full contact details.
