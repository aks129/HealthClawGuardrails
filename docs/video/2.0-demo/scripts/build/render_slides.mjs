// Render slide HTML to video, one screenshot per output frame.
//
//   node render_slides.mjs <card.html> <beat id> <out.mp4> [dur] [--preview t1,t2,..]
//
// Loads cards/<card.html>, sets window.BEAT = {dur, cues, data} from
// build/timeline.json (+ any capture JSON the card asks for via
// window.DATA_FILES), then for each frame calls window.render(t) and
// screenshots. `dur` overrides the beat length (for a slide that fills only
// part of a beat). --preview writes PNG stills at the given times instead.
import { createRequire } from 'module';
import fs from 'fs';
import path from 'path';
import { execFileSync } from 'child_process';
const require = createRequire(path.join(process.env.HC_REPO_ROOT || process.cwd(), 'e2e', 'package.json'));
const { chromium } = require('playwright');

const V = path.resolve(path.dirname(new URL(import.meta.url).pathname), '..');
const [card, beatId, out, durArg] = process.argv.slice(2);
const pi = process.argv.indexOf('--preview');
const preview = pi > 0 ? process.argv[pi + 1].split(',').map(Number) : null;
const timeline = JSON.parse(fs.readFileSync(path.join(V, 'build', 'timeline.json'), 'utf8'));
const beat = timeline.beats.find((b) => b.id === beatId) || { dur: 5, cues: {} };
const FPS = 30;
const dur = durArg && !durArg.startsWith('--') ? Number(durArg) : beat.dur + timeline.xfade;

const data = {
  b4: JSON.parse(fs.readFileSync(path.join(V, 'capture', 'b4_redaction.json'), 'utf8')),
  b5: JSON.parse(fs.readFileSync(path.join(V, 'capture', 'b5_audit.json'), 'utf8')),
  pytest: fs.readFileSync(path.join(V, 'capture', 'b7_pytest.txt'), 'utf8'),
  conformance: fs.readFileSync(path.join(V, 'capture', 'b7_conformance.txt'), 'utf8'),
  formStatus: JSON.parse(fs.readFileSync(path.join(V, 'capture', 'b6_form_status.json'), 'utf8')).status,
  noaudit: { status: fs.readFileSync(path.join(V, 'capture', 'b5_noaudit_status.txt'), 'utf8').trim(),
             error: fs.readFileSync(path.join(V, 'capture', 'b5_noaudit_error.txt'), 'utf8').trim() },
};

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1920, height: 1080 }, deviceScaleFactor: 1 });
await page.addInitScript(([b, d]) => { window.BEAT = b; window.DATA = d; },
  [{ dur, cues: beat.cues }, data]);
await page.goto('file://' + path.join(V, 'cards', card));
await page.evaluate(() => document.fonts.ready);
await page.evaluate(() => window.init && window.init());

if (preview) {
  for (const t of preview) {
    await page.evaluate((t) => window.render(t), t);
    await page.screenshot({ path: out.replace(/\.png$/, '') + `_${t}.png` });
  }
} else {
  const dir = out + '.frames';
  fs.rmSync(dir, { recursive: true, force: true });
  fs.mkdirSync(dir, { recursive: true });
  const n = Math.round(dur * FPS);
  for (let i = 0; i < n; i++) {
    await page.evaluate((t) => window.render(t), i / FPS);
    await page.screenshot({ path: path.join(dir, `f${String(i).padStart(5, '0')}.jpg`),
                            type: 'jpeg', quality: 95 });
  }
  execFileSync('ffmpeg', ['-v', 'error', '-y', '-framerate', String(FPS),
    '-i', path.join(dir, 'f%05d.jpg'), '-c:v', 'libx264', '-crf', '12',
    '-pix_fmt', 'yuv420p', '-preset', 'medium', out]);
  fs.rmSync(dir, { recursive: true, force: true });
  console.log(out, n, 'frames');
}
await browser.close();
