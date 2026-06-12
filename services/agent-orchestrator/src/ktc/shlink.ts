// Vendored from https://github.com/jmandel/kill-the-clipboard-skill
// @ fa0020dcc03e5daa2470462c404a1e69c637b474 (MIT). Local changes: import-extension
// adjustments only — keep diffable against upstream.

// SHLink payload construction/parsing (base spec) + owner-link fragments (docs/DESIGN.md §3).

import { b64url, b64urlDecode, utf8, utf8Decode } from './encoding';

export interface ShlinkPayload {
  url: string;
  key: string;
  exp?: number; // Epoch SECONDS (KTC profile: required)
  flag?: string; // single chars, alphabetical order: L, P, U
  label?: string; // ≤80 chars
  v?: number;
  [k: string]: unknown;
}

export function buildShlink(p: ShlinkPayload): string {
  if (p.url.length > 128) throw new Error(`payload url exceeds 128 chars (${p.url.length})`);
  if (b64urlDecode(p.key).length !== 32) throw new Error('key must decode to 32 bytes');
  if (p.label !== undefined && p.label.length > 80) throw new Error(`label exceeds 80 chars (${p.label.length})`);
  if (p.flag !== undefined) {
    if (p.flag.includes('U') && p.flag.includes('P')) throw new Error('flags U and P cannot be combined');
    const sorted = [...p.flag].sort().join('');
    if (sorted !== p.flag) throw new Error(`flags must be in alphabetical order ("${sorted}", not "${p.flag}")`);
  }
  return 'shlink:/' + b64url(utf8(JSON.stringify(p)));
}

/** Accepts a bare `shlink:/...` or a viewer-prefixed `https://...#shlink:/...`. */
export function parseShlink(s: string): ShlinkPayload {
  const m = s.trim().match(/shlink:\/(.+)$/);
  if (!m?.[1]) throw new Error('not a shlink');
  const payload = JSON.parse(utf8Decode(b64urlDecode(m[1]))) as ShlinkPayload;
  if (typeof payload.url !== 'string' || typeof payload.key !== 'string') {
    throw new Error('shlink payload missing url/key');
  }
  return payload;
}

// --- Handoff-page fragments ------------------------------------------------------------
// Owner page:  <base>/m#<base64url(M)>            (43-char token → manage + reconstruct QR)
// Viewer page: <base>/v#shlink:/...               (standard SHL viewer-prefix convention)
// The app routes by fragment shape, so /s remains a working legacy alias server-side.

export function buildOwnerLink(baseUrl: string, masterSecret: Uint8Array): string {
  return `${baseUrl.replace(/\/$/, '')}/m#${b64url(masterSecret)}`;
}

export function buildViewerLink(baseUrl: string, shlink: string): string {
  return `${baseUrl.replace(/\/$/, '')}/v#${shlink}`;
}

export type FragmentParse =
  | { mode: 'owner'; masterSecret: Uint8Array; api?: string }
  | { mode: 'viewer'; payload: ShlinkPayload };

/** Parse a handoff-page fragment (without leading '#'). */
export function parseFragment(fragment: string): FragmentParse {
  const frag = fragment.replace(/^#/, '');
  if (frag.includes('shlink:/')) return { mode: 'viewer', payload: parseShlink(frag) };
  const params = frag.includes('&') ? frag.split('&') : [frag];
  const token = params[0] ?? '';
  const api = params.slice(1).find((p) => p.startsWith('api='))?.slice(4);
  const masterSecret = b64urlDecode(token);
  if (masterSecret.length !== 32) throw new Error('owner token must decode to 32 bytes');
  return { mode: 'owner', masterSecret, ...(api ? { api: decodeURIComponent(api) } : {}) };
}
