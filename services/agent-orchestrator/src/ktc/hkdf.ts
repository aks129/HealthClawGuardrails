// Vendored from https://github.com/jmandel/kill-the-clipboard-skill
// @ fa0020dcc03e5daa2470462c404a1e69c637b474 (MIT). Local changes: import-extension
// adjustments only — keep diffable against upstream.

// Capability derivation (docs/DESIGN.md §3).
//
// One 32-byte owner master secret M. Two one-way derivations:
//   auth = HKDF-SHA256(M, info="ktc-shl/v1/auth")  → control capability, registered with server
//   key  = HKDF-SHA256(M, info="ktc-shl/v1/key")   → SHL encryption key, never leaves the client
// Empty salt; 32-byte outputs; base64url encoding (43 chars).

import { b64url, utf8 } from './encoding';

export const INFO_AUTH = 'ktc-shl/v1/auth';
export const INFO_KEY = 'ktc-shl/v1/key';

export function generateMasterSecret(): Uint8Array {
  return crypto.getRandomValues(new Uint8Array(32));
}

export async function hkdf(ikm: Uint8Array, info: string, length = 32): Promise<Uint8Array> {
  const keyMaterial = await crypto.subtle.importKey('raw', ikm as BufferSource, 'HKDF', false, ['deriveBits']);
  const bits = await crypto.subtle.deriveBits(
    { name: 'HKDF', hash: 'SHA-256', salt: new Uint8Array(0), info: utf8(info) as BufferSource },
    keyMaterial,
    length * 8,
  );
  return new Uint8Array(bits);
}

/** Control capability presented to the server (server stores only its sha256). */
export async function deriveAuth(m: Uint8Array): Promise<string> {
  return b64url(await hkdf(m, INFO_AUTH));
}

/** SHL encryption key (the `key` field of the shlink payload). */
export async function deriveKey(m: Uint8Array): Promise<string> {
  return b64url(await hkdf(m, INFO_KEY));
}

/** What the server stores for lookup: sha256(auth), hex. */
export async function authHash(auth: string): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', utf8(auth) as BufferSource);
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, '0')).join('');
}
