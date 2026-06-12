// Vendored from https://github.com/jmandel/kill-the-clipboard-skill
// @ fa0020dcc03e5daa2470462c404a1e69c637b474 (MIT). Local changes: import-extension
// adjustments only — keep diffable against upstream.

// base64url helpers shared by every component (server, app, skill scripts).

export function b64url(bytes: Uint8Array): string {
  let bin = '';
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin).replaceAll('+', '-').replaceAll('/', '_').replaceAll('=', '');
}

export function b64urlDecode(s: string): Uint8Array {
  const b64 = s.replaceAll('-', '+').replaceAll('_', '/');
  const pad = b64.length % 4 === 0 ? '' : '='.repeat(4 - (b64.length % 4));
  const bin = atob(b64 + pad);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

export const utf8 = (s: string): Uint8Array => new TextEncoder().encode(s);
export const utf8Decode = (b: Uint8Array): string => new TextDecoder().decode(b);
