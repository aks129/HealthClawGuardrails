/**
 * Crypto round-trip tests for the vendored kill-the-clipboard crypto lib.
 */

import { encryptJWE, decryptJWE } from "./jwe";
import { deriveAuth, deriveKey, generateMasterSecret } from "./hkdf";
import { utf8, utf8Decode } from "./encoding";

describe("ktc crypto round-trip", () => {
  it("encryptJWE then decryptJWE returns original bytes", async () => {
    const M = generateMasterSecret();
    const key = await deriveKey(M);

    const originalText = "Hello, SMART Health Link!";
    const plaintext = utf8(originalText);

    const jwe = await encryptJWE(plaintext, key);
    const { plaintext: decrypted } = await decryptJWE(jwe, key);

    expect(utf8Decode(decrypted)).toBe(originalText);
  });

  it("encryptJWE with deflate round-trips correctly", async () => {
    const M = generateMasterSecret();
    const key = await deriveKey(M);

    const originalText = JSON.stringify({ resourceType: "Bundle", type: "collection", entry: [] });
    const plaintext = utf8(originalText);

    const jwe = await encryptJWE(plaintext, key, { cty: "application/fhir+json", deflate: true });

    // JWE must be 5 segments
    expect(jwe.split(".").length).toBe(5);

    const { plaintext: decrypted, header } = await decryptJWE(jwe, key);
    expect(utf8Decode(decrypted)).toBe(originalText);
    expect(header.zip).toBe("DEF");
    expect(header.cty).toBe("application/fhir+json");
  });

  it("JWE compact serialization has exactly 5 segments (dir alg with empty encrypted-key slot)", async () => {
    const M = generateMasterSecret();
    const key = await deriveKey(M);

    const jwe = await encryptJWE(utf8("test payload"), key);
    const segments = jwe.split(".");
    expect(segments.length).toBe(5);
    // segment[1] is the empty encrypted-key slot for alg=dir
    expect(segments[1]).toBe("");
  });

  it("deriveAuth and deriveKey produce different 43-char base64url outputs from the same M", async () => {
    const M = generateMasterSecret();

    const auth = await deriveAuth(M);
    const key = await deriveKey(M);

    // Both should be 43 chars (256 bits in base64url without padding)
    expect(auth.length).toBe(43);
    expect(key.length).toBe(43);

    // They must be different
    expect(auth).not.toBe(key);

    // Must be valid base64url (no +, /, or =)
    expect(auth).toMatch(/^[A-Za-z0-9_-]+$/);
    expect(key).toMatch(/^[A-Za-z0-9_-]+$/);
  });

  it("deriveAuth and deriveKey are deterministic for the same M", async () => {
    const M = new Uint8Array(32).fill(42); // fixed M for determinism

    const auth1 = await deriveAuth(M);
    const auth2 = await deriveAuth(M);
    const key1 = await deriveKey(M);
    const key2 = await deriveKey(M);

    expect(auth1).toBe(auth2);
    expect(key1).toBe(key2);
  });

  it("different M values produce different auth and key", async () => {
    const M1 = generateMasterSecret();
    const M2 = generateMasterSecret();

    const auth1 = await deriveAuth(M1);
    const auth2 = await deriveAuth(M2);
    const key1 = await deriveKey(M1);
    const key2 = await deriveKey(M2);

    expect(auth1).not.toBe(auth2);
    expect(key1).not.toBe(key2);
  });
});
