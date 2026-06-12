// Vendored from https://github.com/jmandel/kill-the-clipboard-skill
// @ fa0020dcc03e5daa2470462c404a1e69c637b474 (MIT). Local changes: import-extension
// adjustments only — keep diffable against upstream.

// JWE compact serialization for SHL files (base spec: alg "dir", enc "A256GCM";
// optional zip "DEF" = raw DEFLATE before encryption). Hand-rolled on WebCrypto so the
// skill zip carries no crypto dependency; cross-checked against `jose` in dev tests only.
//
// Compact form with direct key agreement: <protected>..<iv>.<ciphertext>.<tag>
// (encrypted-key segment is empty). AAD = ASCII bytes of the protected header segment.
// The same key is reused across files/updates of one SHL → a fresh random IV per
// encryption operation is mandatory (spec SHALL).

import { b64url, b64urlDecode, utf8, utf8Decode } from './encoding';

export interface JweHeader {
  alg: 'dir';
  enc: 'A256GCM';
  cty?: string;
  zip?: 'DEF';
  [k: string]: unknown;
}

async function importKey(keyB64url: string): Promise<CryptoKey> {
  const raw = b64urlDecode(keyB64url);
  if (raw.length !== 32) throw new Error(`SHL key must be 32 bytes (got ${raw.length})`);
  return crypto.subtle.importKey('raw', raw as BufferSource, 'AES-GCM', false, ['encrypt', 'decrypt']);
}

async function pipe(data: Uint8Array, stream: CompressionStream | DecompressionStream): Promise<Uint8Array> {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const out = await new Response(new Blob([data as any]).stream().pipeThrough(stream as any)).arrayBuffer();
  return new Uint8Array(out);
}

export async function encryptJWE(
  plaintext: Uint8Array,
  keyB64url: string,
  opts: { cty?: string; deflate?: boolean } = {},
): Promise<string> {
  const header: JweHeader = { alg: 'dir', enc: 'A256GCM' };
  if (opts.cty) header.cty = opts.cty;
  let body = plaintext;
  if (opts.deflate) {
    header.zip = 'DEF';
    body = await pipe(plaintext, new CompressionStream('deflate-raw'));
  }
  const protectedB64 = b64url(utf8(JSON.stringify(header)));
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const key = await importKey(keyB64url);
  const sealed = new Uint8Array(
    await crypto.subtle.encrypt(
      { name: 'AES-GCM', iv: iv as BufferSource, additionalData: utf8(protectedB64) as BufferSource, tagLength: 128 },
      key,
      body as BufferSource,
    ),
  );
  const ct = sealed.slice(0, -16);
  const tag = sealed.slice(-16);
  return `${protectedB64}..${b64url(iv)}.${b64url(ct)}.${b64url(tag)}`;
}

export async function decryptJWE(
  jwe: string,
  keyB64url: string,
): Promise<{ plaintext: Uint8Array; header: JweHeader }> {
  const parts = jwe.trim().split('.');
  if (parts.length !== 5) throw new Error('not a compact JWE (expected 5 segments)');
  const [protectedB64, encKey, ivB64, ctB64, tagB64] = parts as [string, string, string, string, string];
  if (encKey !== '') throw new Error('expected empty encrypted-key segment (alg dir)');
  const header = JSON.parse(utf8Decode(b64urlDecode(protectedB64))) as JweHeader;
  if (header.alg !== 'dir' || header.enc !== 'A256GCM') {
    throw new Error(`unsupported JWE alg/enc: ${header.alg}/${header.enc}`);
  }
  const key = await importKey(keyB64url);
  const ct = b64urlDecode(ctB64);
  const tag = b64urlDecode(tagB64);
  const sealed = new Uint8Array(ct.length + tag.length);
  sealed.set(ct);
  sealed.set(tag, ct.length);
  let plaintext: Uint8Array = new Uint8Array(
    await crypto.subtle.decrypt(
      { name: 'AES-GCM', iv: b64urlDecode(ivB64) as BufferSource, additionalData: utf8(protectedB64) as BufferSource, tagLength: 128 },
      key,
      sealed as BufferSource,
    ),
  );
  if (header.zip === 'DEF') plaintext = await pipe(plaintext, new DecompressionStream('deflate-raw'));
  return { plaintext, header };
}
