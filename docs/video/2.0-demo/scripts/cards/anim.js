// Frame-deterministic animation helpers. The renderer sets window.BEAT
// ({dur, cues}) and calls window.render(t) once per output frame, then
// screenshots. Nothing depends on wall-clock time or CSS transitions.
const clamp = (x) => Math.max(0, Math.min(1, x));
const ease = (x) => { x = clamp(x); return x * x * (3 - 2 * x); };
const ramp = (t, start, len = 0.5) => ease((t - start) / len);
const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];

// Fade + small rise in, starting at `start`.
function fade(el, t, start, len = 0.5, rise = 18) {
  if (!el) return;
  const k = ramp(t, start, len);
  el.style.opacity = k;
  el.style.transform = `translateY(${(1 - k) * rise}px)`;
}

// Slow push on the whole slide (Ken Burns): 1.0 -> 1.0 + amount over the beat.
function push(t, amount = 0.035) {
  const d = (window.BEAT && window.BEAT.dur) || 10;
  $('.stage').style.transform = `scale(${1 + amount * clamp(t / d)})`;
}

const cue = (name, fallback = 0) =>
  (window.BEAT && window.BEAT.cues && name in window.BEAT.cues)
    ? window.BEAT.cues[name] : fallback;
