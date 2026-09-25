// Burned-in captions as transparent 1920x1080 PNGs, one per cue in
// build/timeline.json (the ffmpeg here has no drawtext/libass, so captions are
// overlaid as images). Fails if any cue needs more than two lines.
import { createRequire } from 'module';
import fs from 'fs';
import path from 'path';
const require = createRequire(path.join(process.env.HC_REPO_ROOT || process.cwd(), 'e2e', 'package.json'));
const { chromium } = require('playwright');
const V = path.resolve(path.dirname(new URL(import.meta.url).pathname), '..');
const tl = JSON.parse(fs.readFileSync(path.join(V, 'build', 'timeline.json'), 'utf8'));
const out = path.join(V, 'build', 'caps');
fs.rmSync(out, { recursive: true, force: true }); fs.mkdirSync(out, { recursive: true });
const b = await chromium.launch();
const p = await b.newPage({ viewport: { width: 1920, height: 1080 } });
await p.goto('file://' + path.join(V, 'cards', 'caption.html'));
await p.evaluate(() => document.fonts.ready);
const list = [];
for (const beat of tl.beats) {
  for (const [i, c] of beat.captions.entries()) {
    const lines = await p.evaluate((txt) => {
      const el = document.getElementById('cap'); el.textContent = txt;
      return Math.round((el.getBoundingClientRect().height - 26) / (44 * 1.3));
    }, c.text);
    if (lines > 2) throw new Error(`caption over 2 lines: ${c.text}`);
    const file = path.join(out, `${beat.id}_${i}.png`);
    await p.screenshot({ path: file, omitBackground: true });
    list.push({ file, start: +(beat.start + c.start).toFixed(3), end: +(beat.start + c.end).toFixed(3), lines, text: c.text });
  }
}
fs.writeFileSync(path.join(out, 'captions.json'), JSON.stringify(list, null, 1));
console.log(list.map((c) => `${c.start}-${c.end} [${c.lines}] ${c.text}`).join('\n'));
await b.close();
