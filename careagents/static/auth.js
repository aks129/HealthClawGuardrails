/* CareAgents auth — passkey (WebAuthn) first, email code fallback + passkey
   enrollment. Uses base64url<->ArrayBuffer helpers for the WebAuthn wire. */
(function () {
  const $ = (id) => document.getElementById(id);
  const err = $("err");
  const show = (id) => ["step-start", "step-code", "step-passkey"]
    .forEach((s) => ($(s).hidden = s !== id));
  function fail(msg) { err.textContent = msg; err.hidden = false; }
  function clear() { err.hidden = true; $("resend-code").hidden = true; }

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
  // Where adding (or skipping) a passkey returns to: the chat it was
  // asked from, set by the server only for this account's own chat.
  const back = $("auth-card").dataset.back || "";
  // A page tapped while signed out is waiting (#1016): after the code, go
  // straight there; the chat menu and Settings offer a passkey later.
  const resume = $("auth-card").dataset.resume === "1";

  // Not drawn on the way to adding a first passkey (auth.html).
  if ($("passkey-btn")) $("passkey-btn").addEventListener("click", async () => {
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
    // Three answers, and only `true` earns "We sent". Compared by identity,
    // never truthiness: `false` and `null` are different facts and collapsing
    // them is how one of these two defects gets reintroduced.
    //
    //   true  — the mail left. Say so.
    //   false — the resend cooldown held it (#262). The code from a moment ago
    //           is still live, so we still ask for it, but we do not claim a
    //           send that did not happen.
    //   null  — the code is live and we never saw the mail leave (#220). We do
    //           not know, so we say we do not know.
    let lede = "We sent an 8-digit code to";
    let note = "";
    stopCountdown();
    if (res.d.sent === false) {
      lede = "We sent a code moments ago to";
      countdown(res.d.retry_after);
    } else if (res.d.sent !== true) {
      lede = "We couldn't confirm the code reached";
      note = res.d.notice || "";
    }
    $("code-lede").textContent = lede;
    if (res.d.sent !== false) {
      $("code-note").textContent = note;
      $("code-note").hidden = !note;
    }
    show("step-code"); $("code").focus();
  });

  // The resend wait, counted down where the person can see it, and then
  // the way to a new code: the screen never stops at "in 22 seconds".
  let tick = null;
  function stopCountdown() { clearInterval(tick); tick = null; }
  function countdown(seconds) {
    let left = Math.max(0, Math.ceil(Number(seconds) || 0));
    const note = $("code-note");
    const say = () => {
      note.textContent = left > 0
        ? `Check your inbox and spam folder. You can request a new one in ${left} seconds.`
        : "Check your inbox and spam folder. You can ask for a new code now.";
      note.hidden = false;
      $("resend-code").hidden = left > 0;
    };
    say();
    if (left <= 0) return;
    tick = setInterval(() => {
      left -= 1;
      say();
      if (left <= 0) stopCountdown();
    }, 1000);
  }
  // The same send as "Email me a code", to the same address.
  $("resend-code").addEventListener("click", () => {
    $("email").value = pendingEmail;
    $("code").value = "";
    $("email-btn").click();
  });
  $("back-btn").addEventListener("click", () => {
    clear(); stopCountdown(); $("resend-code").hidden = true;
    show("step-start");
  });

  $("verify-btn").addEventListener("click", async () => {
    clear();
    const res = await post("/api/auth/verify", { email: pendingEmail, code: $("code").value.trim() });
    if (!res.ok) {
      fail(res.d.error || "Wrong code.");
      // A wrong, expired or used-up code: offer a new one right there.
      $("resend-code").hidden = false;
      return;
    }
    // Said only now, after the code: before it, the answer is the same
    // with or without an account (10-20 patient walk). The hub shows it
    // (home.js showCarriedNotice); blocked storage costs only the line.
    if (res.d.new_account === true) {
      try {
        sessionStorage.setItem("careagents.hubNotice",
          "No account with this email yet, so we made a new one.");
      } catch (e) { /* the hub opens anyway */ }
    }
    // `first_run`: nothing connected yet. The records come first; the
    // passkey waits in the chat's menu rather than standing in the way.
    if (res.d.has_passkey || res.d.first_run === true ||
        !window.PublicKeyCredential) {
      location.href = back || "/home"; return;
    }
    if (resume) { location.href = back || "/home"; return; }
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
      if (res.ok) location.href = back ? back + "&passkey=added" : "/home";
      else fail(res.d.error || "Couldn't save the passkey.");
    } catch (e) { fail("Passkey setup was cancelled."); }
  }
  $("add-passkey-btn").addEventListener("click", enroll);
  $("skip-passkey-btn").addEventListener("click", () =>
    (location.href = back || "/home"));

  $("email").addEventListener("keydown", (e) => { if (e.key === "Enter") $("email-btn").click(); });
})();
