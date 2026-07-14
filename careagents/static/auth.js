/* CareAgents auth — passkey (WebAuthn) first, email code fallback + passkey
   enrollment. Uses base64url<->ArrayBuffer helpers for the WebAuthn wire. */
(function () {
  const $ = (id) => document.getElementById(id);
  const err = $("err");
  const show = (id) => ["step-start", "step-code", "step-passkey"]
    .forEach((s) => ($(s).hidden = s !== id));
  function fail(msg) { err.textContent = msg; err.hidden = false; }
  function clear() { err.hidden = true; }

  const b64uToBuf = (s) => {
    s = s.replace(/-/g, "+").replace(/_/g, "/");
    const pad = s.length % 4 ? "=".repeat(4 - (s.length % 4)) : "";
    const bin = atob(s + pad);
    return Uint8Array.from(bin, (c) => c.charCodeAt(0)).buffer;
  };
  const bufToB64u = (buf) => btoa(String.fromCharCode(...new Uint8Array(buf)))
    .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");

  async function post(url, body) {
    const r = await fetch(url, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: body === undefined ? "{}" : JSON.stringify(body),
    });
    const d = await r.json().catch(() => ({}));
    return { ok: r.ok, status: r.status, d };
  }

  // --- passkey sign-in ---
  $("passkey-btn").addEventListener("click", async () => {
    clear();
    if (!window.PublicKeyCredential) return fail("This browser doesn't support passkeys — use email.");
    try {
      const { d: opts } = await post("/webauthn/login/options");
      opts.challenge = b64uToBuf(opts.challenge);
      if (opts.allowCredentials)
        opts.allowCredentials = opts.allowCredentials.map((c) => ({ ...c, id: b64uToBuf(c.id) }));
      const cred = await navigator.credentials.get({ publicKey: opts });
      const payload = {
        id: cred.id, rawId: bufToB64u(cred.rawId), type: cred.type,
        response: {
          authenticatorData: bufToB64u(cred.response.authenticatorData),
          clientDataJSON: bufToB64u(cred.response.clientDataJSON),
          signature: bufToB64u(cred.response.signature),
          userHandle: cred.response.userHandle ? bufToB64u(cred.response.userHandle) : null,
        },
      };
      const res = await post("/webauthn/login/verify", payload);
      if (res.ok) location.href = "/home";
      else fail(res.d.error || "Sign-in failed.");
    } catch (e) { fail("No passkey found on this device — try email."); }
  });

  // --- email code ---
  let pendingEmail = "";
  $("email-btn").addEventListener("click", async () => {
    clear();
    const email = $("email").value.trim();
    if (!email) return fail("Enter your email.");
    const res = await post("/api/auth/email", { email });
    if (!res.ok) return fail(res.d.error || "Couldn't send a code.");
    pendingEmail = email;
    $("code-email").textContent = email;
    show("step-code"); $("code").focus();
  });
  $("back-btn").addEventListener("click", () => { clear(); show("step-start"); });

  $("verify-btn").addEventListener("click", async () => {
    clear();
    const res = await post("/api/auth/verify", { email: pendingEmail, code: $("code").value.trim() });
    if (!res.ok) return fail(res.d.error || "Wrong code.");
    if (res.d.has_passkey || !window.PublicKeyCredential) { location.href = "/home"; return; }
    show("step-passkey");
  });
  $("code").addEventListener("keydown", (e) => { if (e.key === "Enter") $("verify-btn").click(); });

  // --- add passkey ---
  async function enroll() {
    clear();
    try {
      const { d: opts } = await post("/webauthn/register/options");
      opts.challenge = b64uToBuf(opts.challenge);
      opts.user.id = b64uToBuf(opts.user.id);
      if (opts.excludeCredentials)
        opts.excludeCredentials = opts.excludeCredentials.map((c) => ({ ...c, id: b64uToBuf(c.id) }));
      const cred = await navigator.credentials.create({ publicKey: opts });
      const payload = {
        id: cred.id, rawId: bufToB64u(cred.rawId), type: cred.type,
        response: {
          attestationObject: bufToB64u(cred.response.attestationObject),
          clientDataJSON: bufToB64u(cred.response.clientDataJSON),
        },
      };
      const res = await post("/webauthn/register/verify", payload);
      if (res.ok) location.href = "/home";
      else fail(res.d.error || "Couldn't save the passkey.");
    } catch (e) { fail("Passkey setup was cancelled."); }
  }
  $("add-passkey-btn").addEventListener("click", enroll);
  $("skip-passkey-btn").addEventListener("click", () => (location.href = "/home"));

  $("email").addEventListener("keydown", (e) => { if (e.key === "Enter") $("email-btn").click(); });
})();
