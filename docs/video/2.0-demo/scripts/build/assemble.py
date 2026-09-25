"""Assemble the HealthClaw 2.0 demo from the real captures and the slides.

  python3 build/assemble.py [--skip-slides]

1. plan.py timing (build/timeline.json) -> slide renders (render_slides.mjs)
2. b6/b7: the device-pixel screencasts (capture/b6_frames, b7_frames) cut,
   gently zoomed and set into a paper frame; click highlights are drawn from
   the recorder's own click marks (Playwright renders no cursor)
3. beats joined with 0.35 s crossfades; VO placed on the absolute timeline
4. captions overlaid as PNGs; two-pass loudnorm to -16 LUFS; x264 + faststart
Every intermediate lands in build/clips/.
"""
import json
import subprocess
import sys
from pathlib import Path

V = Path(__file__).resolve().parent.parent
B = V / "build"
CAP = V / "capture"
CL = B / "clips"
OUT = V / "out"
FPS = 30
SLOT = (192, 64, 1536, 864)           # where a recording sits in its frame

def sh(*a):
    return subprocess.run(a, check=True)


def ff(*a):
    return sh("ffmpeg", "-v", "error", "-y", *a)


def slides(tl):
    jobs = [("b1.html", "b1", None), ("b2.html", "b2", None), ("b3.html", "b3", None),
            ("b4.html", "b4", None), ("b5.html", "b5", None), ("b8.html", "b8", None),
            ("b9.html", "b9", None),
            ("b7a.html", "b7", 5.30), ("b6pdf.html", "b6", 1.85)]
    for card, bid, dur in jobs:
        out = CL / f"slide_{card.replace('.html', '')}.mp4"
        args = ["node", str(B / "render_slides.mjs"), card, bid, str(out)]
        if dur:
            args.append(str(dur))
        elif bid == tl["beats"][-1]["id"]:
            args.append(str(tl["beats"][-1]["dur"]))      # last beat: no pad
        sh(*args)


def cfr(name):
    """The screencast's ffconcat list -> 30 fps 2560x1440 source video."""
    out = CL / f"{name}_src.mp4"
    if not out.exists():
        ff("-f", "concat", "-safe", "0", "-i", str(CAP / f"{name}_frames" / "list.ffconcat"),
           "-vf", "fps=30,scale=2560:1440:flags=lanczos,format=yuv420p",
           "-fps_mode", "cfr", "-r", "30",
           "-c:v", "libx264", "-crf", "10", "-preset", "medium", str(out))
    return out


def marks(name):
    d = json.loads((CAP / f"{name}_marks.json").read_text())
    return d["first_frame_offset"], {m["name"]: m for m in d["marks"]}


def segment(src, v0, v1, hold, zoom, focus, boxes, out):
    """One cut of a recording: source seconds [v0, v1), held on its last frame
    for `hold` s, a push from zoom[0] to zoom[1] about `focus` (source px),
    and highlight rings: boxes = [(t0, t1, css_box)] in segment seconds."""
    n = v1 - v0 + hold
    z0, z1 = zoom
    frames = max(1, round(n * FPS))
    z = f"({z0}+({z1}-{z0})*on/{frames})"
    fx, fy = focus
    draw = []
    for (a, b, bx) in boxes:
        x, y, w, h = (bx["x"] * 2 - 10, bx["y"] * 2 - 10, bx["width"] * 2 + 20, bx["height"] * 2 + 20)
        draw.append(f"drawbox=x={x:.0f}:y={y:.0f}:w={w:.0f}:h={h:.0f}:color=0x1B2FBF@0.95:t=8:"
                    f"enable='between(t,{a:.3f},{b:.3f})'")
    vf = ",".join(
        [f"trim={v0:.3f}:{v1:.3f}", "setpts=PTS-STARTPTS",
         f"tpad=stop_mode=clone:stop_duration={hold:.3f}"] + draw +
        # zoompan, not crop: crop evaluates its size once, so it cannot push.
        [f"zoompan=z='{z}':x='{fx}*(1-1/zoom)':y='{fy}*(1-1/zoom)':d=1:"
         f"s={SLOT[2]}x{SLOT[3]}:fps={FPS}", "setsar=1", "format=yuv420p"])
    ff("-i", str(src), "-vf", vf, "-t", f"{n:.3f}", "-c:v", "libx264", "-crf", "12", str(out))
    return out


def framed(frame_png, parts, out):
    """Concatenate recording segments and set them into the paper frame."""
    lst = CL / (out.stem + "_parts.txt")
    lst.write_text("".join(f"file '{p}'\n" for p in parts))
    ff("-f", "concat", "-safe", "0", "-i", str(lst), "-loop", "1", "-i", str(frame_png),
       "-filter_complex", f"[1:v][0:v]overlay={SLOT[0]}:{SLOT[1]}:shortest=1,format=yuv420p[v]",
       "-map", "[v]", "-r", str(FPS), "-c:v", "libx264", "-crf", "12", str(out))
    return out


def xfade2(a, b, at, out, d=0.35):
    ff("-i", str(a), "-i", str(b), "-filter_complex",
       f"[0:v][1:v]xfade=transition=fade:duration={d}:offset={at:.3f},format=yuv420p[v]",
       "-map", "[v]", "-c:v", "libx264", "-crf", "12", str(out))
    return out


def b6(tl):
    """v2 recording (capture/b6_frames): the review opens scrolled to
    "Current medications", so the demographics card is never in the cut."""
    beat = next(b for b in tl["beats"] if b["id"] == "b6")
    src = cfr("b6")
    off, m = marks("b6")
    def v(t):                                  # recorder clock -> video seconds
        return t - off
    card = m["click_card"]["box"]
    focus_card = (card["x"] * 2 + card["width"], card["y"] * 2 + card["height"])
    # approvals, held to 3.8 s; the proposed form ringed from 2.9 s
    s1 = segment(src, v(0.25), v(2.85), 3.8 - 2.6, (1.0, 1.14), focus_card,
                 [(2.9, 3.8, card)], CL / "b6_s1.mp4")
    # Review, from the first frame already scrolled to the medications
    # (checked: 3.076 s is the first). The screencast sends nothing between
    # the last pre-click frame (8.34 s, "Ready to generate", Approve on
    # screen where the click mark measured it) and the answer (9.62 s,
    # "Review recorded"), because the page did not change; the Approve ring
    # sits on that stretch.
    r0, r1, done = 3.10, 9.00, 9.62
    ring = [(v(m[k]["t"]) - r0 - 0.3, v(m[k]["t"]) - r0 + 0.15, m[k]["box"])
            for k in ("med_0", "nka")]
    ring.append((8.35 - r0, r1 - r0, m["approve"]["box"]))
    s2a = segment(src, r0, r1, 0, (1.08, 1.12), (1280, 1300), ring, CL / "b6_s2a.mp4")
    tail = beat["dur"] + tl["xfade"] - 1.85 + 0.35 - 3.8 - (r1 - r0)
    s2b = segment(src, done, done + 0.3, tail - 0.3, (1.12, 1.13), (1280, 1300), [],
                  CL / "b6_s2b.mp4")
    ui = framed(B / "frame_b6.png", [s1, s2a, s2b], CL / "b6_ui.mp4")
    ui_len = 3.8 + (r1 - r0) + tail
    out = xfade2(ui, CL / "slide_b6pdf.mp4", ui_len - 0.35, CL / "beat_b6.mp4")
    print("b6 Approve ring at beat", round(3.8 + 8.35 - r0, 2), "-", round(3.8 + r1 - r0, 2),
          "| VO 'Approve' cue", beat["cues"]["approve_word"])
    return out


def b7(tl):
    src = cfr("b7")
    off, m = marks("b7")
    def v(t):
        return t - off
    s1_0, s1_1 = 2.0, 4.5                     # the question typed and sent
    send = m["send"]
    s1 = segment(src, v(s1_0), v(s1_1), 0, (1.06, 1.10), (1280, 1100),
                 [(send["t"] - s1_0 - 0.6, send["t"] - s1_0 + 0.12, send["box"])], CL / "b7_s1.mp4")
    beat = next(b for b in tl["beats"] if b["id"] == "b7")
    total = beat["dur"] + tl["xfade"]
    chat_len = total - 4.95
    s2_len = chat_len - (s1_1 - s1_0)
    s2_0 = m["answer_text"]["t"] - 0.35       # typewriter already running
    s2 = segment(src, v(s2_0), v(s2_0 + s2_len), 0, (1.06, 1.10), (1280, 1100), [], CL / "b7_s2.mp4")
    ui = framed(B / "frame_b7.png", [s1, s2], CL / "b7_ui.mp4")
    print("b7 cut: reply wait removed =", round(s2_0 - s1_1, 1), "s")
    # the slide is 5.30 s; the chat fades in over its last 0.35 s
    ff("-i", str(CL / "slide_b7a.mp4"), "-i", str(ui), "-filter_complex",
       "[0:v][1:v]xfade=transition=fade:duration=0.35:offset=4.95,format=yuv420p[v]",
       "-map", "[v]", "-c:v", "libx264", "-crf", "12", str(CL / "beat_b7.mp4"))
    return CL / "beat_b7.mp4"


def audio(tl):
    ins, parts = [], []
    for i, b in enumerate(tl["beats"]):
        alt = B / "vo" / f"vo_{b['id']}.wav"
        ins += ["-i", str(alt if alt.exists() else V / f"vo_{b['id']}.wav")]
        ms = int(round((b["start"] + b["lead"]) * 1000))
        parts.append(f"[{i}:a]aresample=48000,aformat=channel_layouts=stereo,adelay={ms}|{ms}[a{i}]")
    n = len(tl["beats"])
    fc = ";".join(parts) + ";" + "".join(f"[a{i}]" for i in range(n)) + \
        f"amix=inputs={n}:normalize=0:dropout_transition=0,apad,atrim=0:{tl['total']:.3f}[a]"
    raw = CL / "vo_mix.wav"
    ff(*ins, "-filter_complex", fc, "-map", "[a]", "-c:a", "pcm_s16le", str(raw))
    # two-pass loudnorm to -16 LUFS integrated, -1.5 dBTP
    r = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(raw), "-af",
                        "loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json", "-f", "null", "-"],
                       capture_output=True, text=True, check=True).stderr
    js = json.loads(r[r.rindex("{"):r.rindex("}") + 1])
    out = CL / "vo_norm.wav"
    ff("-i", str(raw), "-af",
       "loudnorm=I=-16:TP=-1.5:LRA=11:linear=true:"
       f"measured_I={js['input_i']}:measured_TP={js['input_tp']}:measured_LRA={js['input_lra']}:"
       f"measured_thresh={js['input_thresh']}:offset={js['target_offset']},aresample=48000",
       "-c:a", "pcm_s16le", str(out))
    return out


def main():
    CL.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(exist_ok=True)
    sh("python3", str(B / "plan.py"))
    tl = json.loads((B / "timeline.json").read_text())
    if "--skip-slides" not in sys.argv:
        slides(tl)
        sh("node", str(B / "render_captions.mjs"))
        sh("node", str(B / "render_frames.mjs"))
    beats = {bid: CL / f"slide_{bid}.mp4" for bid in ("b1", "b2", "b3", "b4", "b5", "b8", "b9")}
    beats["b6"] = b6(tl)
    beats["b7"] = b7(tl)
    for i, b in enumerate(tl["beats"]):
        want = b["dur"] + (tl["xfade"] if i + 1 < len(tl["beats"]) else 0)
        got = float(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries",
                    "format=duration", "-of", "csv=p=0", str(beats[b["id"]])]))
        assert abs(got - want) < 0.07, f"{b['id']}: clip {got:.3f}s, timeline wants {want:.3f}s"

    # join: every beat but the last is padded by xfade; offset = next beat's start
    ins, fc, prev = [], [], "0:v"
    order = [b["id"] for b in tl["beats"]]
    for i, bid in enumerate(order):
        ins += ["-i", str(beats[bid])]
    for i in range(1, len(order)):
        at = tl["beats"][i]["start"]
        fc.append(f"[{prev}][{i}:v]xfade=transition=fade:duration={tl['xfade']}:offset={at:.3f}[x{i}]")
        prev = f"x{i}"
    caps = json.loads((B / "caps" / "captions.json").read_text())
    k = len(order)
    for j, c in enumerate(caps):
        ins += ["-loop", "1", "-t", f"{tl['total']:.3f}", "-i", c["file"]]
        fc.append(f"[{prev}][{k + j}:v]overlay=0:0:enable='between(t,{c['start']:.3f},{c['end']:.3f})'[c{j}]")
        prev = f"c{j}"
    fc.append(f"[{prev}]format=yuv420p[v]")
    vid = CL / "video_captioned.mp4"
    ff(*ins, "-filter_complex", ";".join(fc), "-map", "[v]", "-t", f"{tl['total']:.3f}",
       "-r", str(FPS), "-c:v", "libx264", "-crf", "12", "-preset", "medium", str(vid))

    a = audio(tl)
    final = OUT / "healthclaw-2.0-demo.mp4"
    crf = sys.argv[sys.argv.index("--crf") + 1] if "--crf" in sys.argv else "22"
    # The slides are JPEG frames (full range); deliver ordinary TV-range 4:2:0.
    ff("-i", str(vid), "-i", str(a), "-map", "0:v", "-map", "1:a",
       "-vf", "scale=out_range=tv,format=yuv420p", "-color_range", "tv",
       "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
       "-c:v", "libx264", "-crf", crf, "-preset", "slow", "-pix_fmt", "yuv420p",
       "-profile:v", "high", "-tune", "stillimage", "-g", "60",
       "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-movflags", "+faststart",
       "-t", f"{tl['total']:.3f}", str(final))
    print(final, final.stat().st_size / 1e6, "MB")


if __name__ == "__main__":
    main()
