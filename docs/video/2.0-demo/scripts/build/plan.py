"""The cut's single source of timing: beat lengths, VO offsets, animation cues
and caption cues. Writes build/timeline.json and out/captions.vtt.

Sentence starts are VO-local seconds from `silencedetect` on each vo_bN.wav
(n=-40dB, d=0.18; b8/b9 re-checked at -35dB/0.08). Each beat is
lead + VO + tail; the VO files already carry ~0.33 s of lead-in and ~0.5 s of
tail silence of their own.

Captions use the script's words exactly, with two spoken-for-TTS spellings
written the way a reader expects: "two point oh" -> "2.0", "healthclaw dot
I O" -> "healthclaw.io".

v2: b9 uses build/vo/vo_b9.wav, the delivered vo_b9.wav with 0.5 s of silence
inserted at 6.18 s (between "healthclaw dot I O" and "The AI helps"), where
the delivered file ran the two sentences together. A caption may carry an
explicit end time as a third element.
"""
import json
import subprocess
from pathlib import Path

V = Path(__file__).resolve().parent.parent
OUT = V / "out"
LEAD, TAIL = 0.15, 0.45
XF = 0.35        # crossfade; every clip but the last is padded by this much

# beat: (lead, tail, [(caption text, VO-local start)], {cue: VO-local time})
BEATS = {
    "b1": (0.40, TAIL, [
        ("Your health record is private.", 0.34),
        ("It holds your name, your medicines, and your test results.", 2.26),
    ], {"s2": 2.26}),
    "b2": (LEAD, TAIL, [
        ("AI helpers can now read records like this.", 0.36),
        ("They can answer your questions, and even act for you.", 3.56),
        ("That is useful. It is also risky.", 6.41),
    ], {"s2": 3.56, "useful": 6.41, "risky": 7.64}),
    "b3": (LEAD, TAIL, [
        ("HealthClaw is a guard that sits between the AI and your record.", 0.32),
        ("It keeps your record apart from everyone else's.", 4.66),
        ("And it makes three promises.", 7.58),
    ], {"s2": 4.66, "s3": 7.58}),
    "b4": (LEAD, TAIL, [
        ("Promise one. The AI gets your health facts, not your contact details.", 0.32),
        ("Your name shrinks to initials.", 5.09),
        ("Your phone number and address are taken out.", 7.12),
    ], {"facts": 1.53, "name": 5.09, "contact": 7.12}),
    "b5": (LEAD, TAIL, [
        ("Promise two. Every look is written down.", 0.32),
        ("If the note can't be written, the look doesn't happen.", 3.16),
    ], {"look": 1.36, "s3": 3.16}),
    "b6": (LEAD, 1.60, [
        ("Promise three. The AI can't send a form, a call, or a text on its own.", 0.32),
        ("It can only suggest one.", 5.61),
        ("Nothing is sent until you tap Approve.", 7.35),
    ], {"suggest": 5.61, "approve_word": 8.95}),
    "b7": (LEAD, TAIL, [
        ("Automatic tests check these promises every time the code changes.", 0.36),
        ("Here, a patient asks about their blood tests, and gets a plain answer.", 4.80),
    ], {"s2": 4.80}),
    "b8": (LEAD, TAIL, [
        ("Everything you saw used made-up records.", 0.33),
        ("In our app, real records are invite-only for now.", 3.08),
        ("And no doctor has signed off on the medical parts yet.", 6.69),
    ], {"s1": 0.33, "s2": 3.08, "s3": 6.69}),
    "b9": (LEAD, 1.90, [
        ("HealthClaw 2.0 is free, and anyone can read its code.", 0.33),
        ("See how it works at healthclaw.io.", 4.34, 6.25),
        ("The AI helps. You stay in charge.", 6.68),
    ], {"s2": 4.34, "s3": 6.68}),
}


def vo_path(bid):
    alt = V / "build" / "vo" / f"vo_{bid}.wav"
    return alt if alt.exists() else V / f"vo_{bid}.wav"


def dur(path):
    return float(subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)]).strip())


def vtt_time(t):
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{s:06.3f}"


def main():
    timeline, t, cues = [], 0.0, []
    for bid, (lead, tail, caps, marks) in BEATS.items():
        vo = dur(vo_path(bid))
        length = round(lead + vo + tail, 3)
        beat_caps = []
        for i, (text, start, *given) in enumerate(caps):
            end = given[0] if given else (
                caps[i + 1][1] - 0.08 if i + 1 < len(caps) else vo - 0.25)
            beat_caps.append({"text": text, "start": round(lead + start, 3),
                              "end": round(lead + end + (0.35 if i + 1 == len(caps) and not given else 0), 3)})
            cues.append((t + lead + start, t + beat_caps[-1]["end"], text))
        timeline.append({"id": bid, "start": round(t, 3), "dur": length,
                         "lead": lead, "vo": vo,
                         "cues": {k: round(lead + v, 3) for k, v in marks.items()},
                         "captions": beat_caps})
        t += length
    (V / "build" / "timeline.json").write_text(json.dumps(
        {"total": round(t, 3), "xfade": XF, "beats": timeline}, indent=1))
    lines = ["WEBVTT", ""]
    for i, (a, b, text) in enumerate(cues, 1):
        lines += [str(i), f"{vtt_time(a)} --> {vtt_time(b)}", text, ""]
    OUT.mkdir(exist_ok=True)
    (OUT / "captions.vtt").write_text("\n".join(lines))
    print("total", round(t, 2))
    for b in timeline:
        print(b["id"], b["start"], b["dur"])


if __name__ == "__main__":
    main()
