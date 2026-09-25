// Paper frames that hold the real recordings (b6, b7): tag text + border.
import { createRequire } from 'module';
import path from 'path';
const require = createRequire(path.join(process.env.HC_REPO_ROOT || process.cwd(), 'e2e', 'package.json'));
const { chromium } = require('playwright');
const V = path.resolve(path.dirname(new URL(import.meta.url).pathname), '..');
const frames = {
  b6: ['<b>RECORDED</b> · CareAgents, local · synthetic record · form proposed by test script · pauses trimmed', '6 / 9'],
  b7: ['<b>RECORDED</b> · CareAgents chat, local · model: Gemini 3.5 Flash · reply wait cut · best of 4 takes', '7 / 9'],
};
const b = await chromium.launch();
const p = await b.newPage({ viewport: { width: 1920, height: 1080 } });
for (const [id, [l, r]] of Object.entries(frames)) {
  await p.goto('file://' + path.join(V, 'cards', 'uiframe.html') + `?l=${encodeURIComponent(l)}&r=${encodeURIComponent(r)}`);
  await p.evaluate(() => document.fonts.ready);
  await p.screenshot({ path: path.join(V, 'build', `frame_${id}.png`) });
}
await b.close();
