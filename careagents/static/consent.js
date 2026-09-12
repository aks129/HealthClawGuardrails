/* CareAgents consent — approve with a fresh, user-verified passkey, or deny.
   Either way the signed decision goes back to HealthClaw through the browser. */
(function () {
  const $ = (id) => document.getElementById(id);
  const err = $("err");
  function fail(msg) { if (err) { err.textContent = msg; err.hidden = false; } }

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
      body: JSON.stringify(body || {}),
    });
    const d = await r.json().catch(() => ({}));
    return { ok: r.ok, status: r.status, d };
  }

  const approve = $("approve-btn");
  if (approve) approve.addEventListener("click", async () => {
    if (err) err.hidden = true;
    if (!window.PublicKeyCredential) return fail("This browser doesn't support passkeys.");
    const chosen = document.querySelector('input[name="connection"]:checked');
    if (!chosen) return fail("Choose which records to share.");
    approve.disabled = true;
    try {
      const { d: opts } = await post("/webauthn/consent/options");
      opts.challenge = b64uToBuf(opts.challenge);
      if (opts.allowCredentials)
        opts.allowCredentials = opts.allowCredentials.map((c) => ({ ...c, id: b64uToBuf(c.id) }));
      const cred = await navigator.credentials.get({ publicKey: opts });
      const passkey = {
        id: cred.id, rawId: bufToB64u(cred.rawId), type: cred.type,
        response: {
          authenticatorData: bufToB64u(cred.response.authenticatorData),
          clientDataJSON: bufToB64u(cred.response.clientDataJSON),
          signature: bufToB64u(cred.response.signature),
          userHandle: cred.response.userHandle ? bufToB64u(cred.response.userHandle) : null,
        },
      };
      const res = await post("/authorize/decide", {
        req: approve.dataset.req, decision: "approved",
        connection_id: chosen.value, passkey,
      });
      if (res.ok && res.d.redirect) location.href = res.d.redirect;
      else { fail(res.d.error || "That didn't work."); approve.disabled = false; }
    } catch (e) {
      fail("No passkey found on this device.");
      approve.disabled = false;
    }
  });

  const deny = $("deny-btn");
  if (deny) deny.addEventListener("click", async () => {
    deny.disabled = true;
    const res = await post("/authorize/decide", { req: deny.dataset.req, decision: "denied" });
    if (res.ok && res.d.redirect) location.href = res.d.redirect;
    else { fail(res.d.error || "That didn't work."); deny.disabled = false; }
  });
})();
