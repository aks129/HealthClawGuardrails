/**
 * Write `adapters/tools.manifest.json` from the live tool catalogue.
 *
 *   cd services/agent-orchestrator && npm run manifest
 *
 * `manifest.test.ts` fails CI when the committed file stops matching this
 * output, so the manifest is a build artifact rather than a file someone
 * remembers to hand-edit.
 */

import * as fs from "fs";
import { MANIFEST_PATH, serializeToolManifest } from "./manifest";

const next = serializeToolManifest();
const previous = fs.existsSync(MANIFEST_PATH)
  ? fs.readFileSync(MANIFEST_PATH, "utf-8")
  : null;

if (previous === next) {
  console.log(`tools.manifest.json already up to date (${MANIFEST_PATH})`);
} else {
  fs.writeFileSync(MANIFEST_PATH, next);
  console.log(`wrote ${MANIFEST_PATH}`);
}
