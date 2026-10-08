/* CareAgents hub and settings: add records, the assistant's menu, the
   waiting band. Small vanilla JS; the server is authoritative. */
(function () {
  const $ = (id) => document.getElementById(id);
  async function post(url, body) {
    const r = await fetch(url, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    return { ok: r.ok, d: await r.json().catch(() => ({})) };
  }

  // The terms version the consent cards on this page show, as rendered.
  // Every consent sends it back so the server records the wording the
  // person read. A 428 to a consent that carried it means the terms changed
  // after this page loaded: reload to show the current card. Never resend
  // the version the 428 names; that would accept wording nobody saw.
  const versioned = document.querySelector("[data-consent-version]");
  const shownConsentVersion = versioned ? versioned.dataset.consentVersion : "";
  const termsChanged = (res) =>
    !res.ok && res.d.error === "consent_required";

  // --- connector marketplace: one handler for every tile ---
  document.querySelectorAll(".connector-tile").forEach((tile) => {
    tile.addEventListener("click", async () => {
      const id = tile.dataset.connector;
      let body = {};
      $("connect-msg").hidden = true;
      if (tile.dataset.providers) {
        const provider = await pickProvider(JSON.parse(tile.dataset.providers));
        if (!provider) return;
        body.provider = provider;
      }
      // Real-record sources: informed consent before anything happens. The
      // server refuses (428) without it, so this card is UX, not the gate.
      if (tile.dataset.consent) {
        const agreed = await showConsentCard();
        if (!agreed) return;
        body.consent = true;
        body.consent_version = shownConsentVersion;
      }
      tile.disabled = true;
      const res = await post("/api/connections/" + id, body);
      tile.disabled = false;
      if (body.consent && termsChanged(res)) { location.reload(); return; }
      if (!res.ok) {
        // Inline, directly under the tile that was tapped: never blocks, never
        // needs dismissing, and the page stays usable.
        return say(tile, $("connect-msg"),
                   res.d.error || "Couldn't connect that source.");
      }
      if (res.d.redirect) { location.assign(res.d.redirect); return; }
      if (res.d.connect_url) window.open(res.d.connect_url, "_blank", "noopener");
      location.reload();
    });
  });

  // Consent card: resolves true only on an explicit "I agree".
  function showConsentCard() {
    return new Promise((resolve) => {
      const modal = document.getElementById("consent-modal");
      const agree = document.getElementById("consent-agree");
      const cancel = document.getElementById("consent-cancel");
      const done = (v) => { modal.hidden = true; resolve(v); };
      agree.onclick = () => done(true);
      cancel.onclick = () => done(false);
      modal.hidden = false;
    });
  }

  // Accept the current terms for real connections made under older ones
  // (beta spec 4.3). Its own card, not the first-connect one; the server
  // refuses (428) without an explicit `consent: true`.
  function showReconsentCard() {
    return new Promise((resolve) => {
      const modal = $("reconsent-modal");
      const done = (v) => { modal.hidden = true; resolve(v); };
      $("reconsent-agree").onclick = () => done(true);
      $("reconsent-cancel").onclick = () => done(false);
      modal.hidden = false;
    });
  }
  document.querySelectorAll("[data-reconsent]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const agreed = await showReconsentCard();
      if (!agreed) return;
      btn.disabled = true;
      const res = await post(
        `/api/connections/${btn.dataset.reconsent}/consent`,
        { consent: true, consent_version: shownConsentVersion });
      if (termsChanged(res)) { location.reload(); return; }
      if (!res.ok) {
        btn.disabled = false;
        return announce(btn.parentElement.querySelector(".inline-msg"),
                        "That didn't work. Try again.");
      }
      location.reload();
    });
  });

  // One shared primitive for the dialogs below: unhide a static modal, resolve
  // once when it closes. ESC and a backdrop tap both abandon it — every one of
  // these is safe to walk away from. The consent card deliberately does NOT go
  // through this: its gate is explicit buttons only.
  function openDialog(modal) {
    let resolve;
    const result = new Promise((r) => { resolve = r; });
    const invoker = document.activeElement;
    let closed = false;
    const onKey = (e) => { if (e.key === "Escape") close(null); };
    const onBackdrop = (e) => { if (e.target === modal) close(null); };
    function close(value) {
      if (closed) return;
      closed = true;
      document.removeEventListener("keydown", onKey);
      modal.removeEventListener("click", onBackdrop);
      modal.hidden = true;
      if (invoker && document.contains(invoker)) invoker.focus();
      resolve(value);
    }
    document.addEventListener("keydown", onKey);
    modal.addEventListener("click", onBackdrop);
    modal.hidden = false;
    return { close, result };
  }

  // How long the empty live region sits in the page before its text lands.
  //
  // A wall-clock number, not a frame count. What has to elapse is time for
  // assistive technology to register the region; requestAnimationFrame
  // measures paints, so the two nested frames this replaces were ~33ms on a
  // 60Hz phone, ~16ms at 120Hz and never at all in a backgrounded tab —
  // under the ~100ms that accessibility libraries conventionally allow. 150ms
  // clears that and stays below the ~200ms where a person feels a wait, and
  // `:empty` in the stylesheet keeps the window from drawing anything.
  //
  // Chosen from documented practice, not measured against a screen reader —
  // no screen reader has been run against any of this (#590).
  const ANNOUNCE_DELAY_MS = 150;
  const announceTimers = new WeakMap();

  // Put `text` into a message element so a live region actually announces it.
  //
  // A live region announces only a change made while it is IN the
  // accessibility tree, and `hidden` is display:none. Text written in the same
  // task as the unhide is a change nothing was listening for, which is exactly
  // the FIRST message on any of these elements — the one a tester meets. So a
  // hidden region is revealed empty, and the text follows a beat later.
  //
  // An already-settled visible region is written straight into: that is the
  // textbook live-region update, and deferring it would blank the message
  // between "Checking…" and its result for no gain. It also keeps the two 5s
  // pollers quiet — while the record store is down they repeat one sentence
  // every tick, and clearing the region for each would blink the card and
  // re-announce it.
  //
  // A pending write that is overtaken is cancelled and re-armed with the newer
  // text. Delete arms "Deleting…" and then awaits the request; a failure that
  // lands inside the wait would otherwise be papered over by the stale
  // "Deleting…" arriving on top of "Your records were not deleted."
  // After a write: make the contact address a mailto link, from nodes.
  const CONTACT = "contactus@healthclaw.io";
  function linkContact(el) {
    const text = el.textContent;
    if (text.indexOf(CONTACT) < 0) return;
    el.textContent = "";
    text.split(CONTACT).forEach((part, i) => {
      if (i) {
        const a = document.createElement("a");
        a.href = "mailto:" + CONTACT;
        a.textContent = CONTACT;
        el.appendChild(a);
      }
      if (part) el.appendChild(document.createTextNode(part));
    });
  }

  function announce(el, text, after) {
    const pending = announceTimers.get(el);
    if (pending) clearTimeout(pending);
    const write = () => {
      el.textContent = text; linkContact(el); if (after) after(); };
    if (!el.hidden && !pending) return write();
    el.textContent = "";
    el.hidden = false;
    announceTimers.set(el, setTimeout(() => {
      announceTimers.delete(el);
      write();
    }, ANNOUNCE_DELAY_MS));
  }

  // The one sentence for "how many records you can now read", shared by the
  // refresh poll and the upload card so the two counters on this page cannot
  // drift again (#226). `note` is the server's uncounted_note, a complete
  // sentence about documents the number leaves out.
  function readableCountLine(n, note) {
    const lead = n > 0
      ? `${n} new record${n === 1 ? "" : "s"} added.`
      : note ? "No new records you can read." : "No new records added.";
    return lead + (note ? ` ${note}` : "");
  }

  // Inline message: shown in the page beside what the user touched.
  //
  // The element is MOVED next to `anchor` before it is shown. A message that
  // renders in its template position can land hundreds of pixels below the
  // fold on a phone — which looks exactly like the dead browser dialog this
  // replaced, so the position is part of the fix, not decoration. Moving the
  // one element keeps the id stable for anything addressing it.
  function say(anchor, el, text) {
    if (anchor && el.previousElementSibling !== anchor) {
      // The move is a remove-then-insert, so the region leaves the
      // accessibility tree even when it was already on screen. Hide it first
      // and announce() brings it back the way it brings back any newly shown
      // region — present and empty before the text arrives.
      el.hidden = true;
      anchor.insertAdjacentElement("afterend", el);
    } else if (!el.hidden && el.textContent === text) {
      // Re-tapping the same closed tile repeats the same sentence, and
      // rewriting a string with itself is not a change anything has to
      // announce. Send it back through the reveal, the way a moved one goes.
      el.hidden = true;
    }
    announce(el, text, () => {
      // Only scrolls if it isn't already fully visible, and only as far as it
      // has to — no jump when the message is already under the user's thumb.
      // Never while a dialog is up: the message is behind the overlay, so the
      // scroll moves nothing the user can see and everything they come back to
      // (#269). Open state is the absence of `hidden` — that attribute is what
      // openDialog() and the consent card toggle.
      if (!document.querySelector(".modal:not([hidden])")) {
        el.scrollIntoView({ block: "nearest" });
      }
    });
  }

  // Scroll a section into view and pulse it — the in-page way to point at the
  // step that has to happen first. The message rides along to the section
  // being scrolled to, or the words explaining the flash end up off-screen in
  // the section the user just left.
  function flashSection(el, msgEl, text) {
    if (!el) { if (msgEl) say(null, msgEl, text); return; }
    if (msgEl) {
      // Moved, so out of the accessibility tree and back in — hidden across
      // the move for the same reason say() hides.
      msgEl.hidden = true;
      el.insertAdjacentElement("afterend", msgEl);
      announce(msgEl, text);
    }
    el.scrollIntoView({ behavior: "smooth", block: "center" });
    el.classList.add("flash");
    setTimeout(() => el.classList.remove("flash"), 1400);
  }

  // Provider picker: one large row per provider, one tap to choose.
  function pickProvider(provs) {
    const rows = $("picker-rows");
    rows.textContent = "";
    const dlg = openDialog($("provider-picker"));
    provs.forEach((p) => {
      const row = document.createElement("button");
      row.type = "button";
      row.className = "picker-row";
      row.dataset.providerId = p.id;
      row.textContent = p.label;   // server-supplied label: text, never markup
      row.addEventListener("click", () => dlg.close(p.id));
      rows.appendChild(row);
    });
    $("picker-cancel").onclick = () => dlg.close(null);
    const first = rows.querySelector(".picker-row");
    if (first) first.focus();
    return dlg.result;
  }

  // Pairing-code card. The visible string and the clipboard string are the
  // same string: if the clipboard is blocked the user copies the selection by
  // hand, and that must not be a different code.
  function showCodeCard(codeString, instructions) {
    const codeEl = $("pair-code");
    const state = $("copy-state");
    codeEl.textContent = codeString;
    $("code-instructions").textContent = instructions;
    state.textContent = "Copy";
    let revert = 0;
    const dlg = openDialog($("code-card"));
    $("copy-code").onclick = async () => {
      // navigator.clipboard is missing or blocked in several in-app browsers
      // (Telegram's among them). Falling back to a selection keeps the flow
      // alive instead of dead-ending on a silent failure.
      clearTimeout(revert);
      try {
        await navigator.clipboard.writeText(codeString);
        // Confirmation is transient; the instruction it replaces is not.
        state.textContent = "Copied ✓";
        revert = setTimeout(() => { state.textContent = "Copy"; }, 1500);
      } catch (err) {
        // This IS the recovery instruction — it stays until the card closes,
        // or it disappears while the user is still pressing and holding.
        selectContents(codeEl);
        state.textContent = "Press and hold to copy";
      }
    };
    $("code-done").onclick = () => dlg.close(null);
    $("code-done").focus();  // never the code itself — that pops the keyboard
    // A pairing code is a short-lived credential; don't leave it in the DOM
    // after the card that needed it is gone. The iMessage instructions quote
    // the code (and the handle), so they have to go with it.
    dlg.result.then(() => {
      clearTimeout(revert);
      codeEl.textContent = "";
      $("code-instructions").textContent = "";
    });
  }

  function selectContents(el) {
    const sel = window.getSelection();
    if (!sel) return;
    const range = document.createRange();
    range.selectNodeContents(el);
    sel.removeAllRanges();
    sel.addRange(range);
  }

  // Poll pending connection cards until active.
  document.querySelectorAll('.conn-card .status-pending').forEach((el) => {
    const card = el.closest(".conn-card");
    const tenant = card.dataset.tenant;
    const msg = card.querySelector(".conn-refresh-msg");
    let saidUnavailable = false;
    const iv = setInterval(async () => {
      const r = await fetch(`/api/connections/${tenant}/poll`);
      const d = await r.json().catch(() => ({}));
      // 503 here means the server could not look, which is not the same as
      // "not here yet". Say so rather than leaving the card spinning on
      // "pending" forever, and keep polling so it clears itself when the
      // record store comes back.
      if (r.status === 503 && d.error === "records_unavailable") {
        announce(msg, d.message);
        saidUnavailable = true;
        return;
      }
      if (!r.ok) return;
      // Only clear what this poller wrote — the refresh flow shares this
      // element and its message must survive.
      if (saidUnavailable) { msg.hidden = true; saidUnavailable = false; }
      // Disconnected (perhaps in another tab): stop and show the real state.
      if (d.status === "active" || d.status === "revoked") {
        clearInterval(iv); location.reload();
      }
    }, 5000);
  });

  // --- refresh an existing connection: check the provider for new records ---
  // Same server endpoint every surface uses, so the consent rules and the
  // "what did we actually pull" reporting can't drift between web and chat.
  document.querySelectorAll(".conn-refresh").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const card = btn.closest(".conn-card");
      const msg = card.querySelector(".conn-refresh-msg");
      // Named apart from the module-scope say(anchor, el, text): this one is
      // card-local and takes only the text.
      const report = (t) => announce(msg, t);
      btn.disabled = true;
      report("Checking…");
      let res = await post(`/api/connections/${btn.dataset.conn}/refresh`);
      // 428 means this deployment wants consent re-affirmed for the re-pull.
      if (!res.ok && res.d.error === "consent_required") {
        const agreed = await showConsentCard();
        if (!agreed) { btn.disabled = false; msg.hidden = true; return; }
        res = await post(`/api/connections/${btn.dataset.conn}/refresh`,
                         { consent: true, consent_version: shownConsentVersion });
        if (termsChanged(res)) { location.reload(); return; }
      }
      btn.disabled = false;
      // `message` first: a coded refusal (records_paused) carries its
      // sentence there, and the code itself is never shown (#856 review).
      if (!res.ok) return report(res.d.message || res.d.error ||
                                 "Couldn't refresh right now.");
      if (res.d.unsupported) return report(res.d.reason);
      if (res.d.reauth_url) {
        window.open(res.d.reauth_url, "_blank", "noopener");
        report("Finish signing in to your provider — new records appear here.");
        watchForNewRecords(card, msg);
      }
    });
  });

  // --- upload: paste your own FHIR Bundle into a `direct` connection (#227) ---
  // Every code the engine or CareAgents can return maps to a short,
  // patient-facing sentence. We never surface raw exception text or the
  // internal tenant id — the user only ever sees an actionable message
  // and (when applicable) a support-quotable correlation id.
  const UPLOAD_MSG = {
    payload_too_large:
      "That file is larger than the 5 MB limit — export a smaller range " +
      "or split into smaller bundles.",
    content_type_required:
      "Please upload a FHIR JSON file — check that the filename ends " +
      "in .json.",
    invalid_json:
      "That file isn't valid JSON. Try re-exporting from your provider.",
    invalid_body:
      "We couldn't read the file. Try re-exporting from your provider.",
    not_a_bundle:
      "That file doesn't look like a FHIR Bundle. Export a Bundle from " +
      "your provider or app and try again.",
    invalid_bundle:
      "That FHIR Bundle looks incomplete. Try re-exporting a full " +
      "Bundle from your provider and upload again.",
    too_many_entries:
      "That bundle has more than 500 records. Split it into smaller " +
      "bundles and upload each.",
    wrong_connector_kind:
      "This connection doesn't accept file uploads.",
    unknown_connection:
      "That connection is no longer available. Refresh and try again.",
    legacy_body_selector:
      "Upload was rejected. Please retry — if it repeats, refresh this page.",
    commit_failed:
      "Something went wrong saving the records. Try again in a moment.",
    ingest_failed:
      "The records service couldn't accept this upload. Try again in a " +
      "moment.",
    records_paused:
      "Your records are paused, so new records can't be added right now. " +
      "If you didn't expect this, write to contactus@healthclaw.io.",
  };
  // Failures that may repeat get a way to reach us, and a support code only
  // when the server sent one: a sentence never promises a code it lacks.
  const UPLOAD_SUPPORT = { commit_failed: 1, ingest_failed: 1 };
  const uploadErrorLine = (code, supportCode) =>
    (UPLOAD_MSG[code] || "The upload didn't go through. Try again in a moment.")
    + ((UPLOAD_SUPPORT[code] || !UPLOAD_MSG[code])
      ? (" If it keeps happening, write to contactus@healthclaw.io" +
         (supportCode ? " and quote this code: " + supportCode + "." : "."))
      : "");
  function messageForError(code) { return uploadErrorLine(code, ""); }

  // Reused file input — the current owner card is tracked here.
  const fileInput = $("upload-file");
  let currentUploadCard = null;

  function sayUpload(msg, text, cls) {
    // Class first, text through announce(): the result box is styled while it
    // is still empty, and `:empty` keeps it from drawing until it has words.
    msg.className = "conn-refresh-msg" + (cls ? " " + cls : "");
    announce(msg, text);
  }

  document.querySelectorAll(".conn-upload").forEach((btn) => {
    btn.addEventListener("click", () => {
      const card = btn.closest(".conn-card");
      currentUploadCard = { card, btn };
      fileInput.value = "";
      fileInput.click();
    });
  });

  if (fileInput) {
    fileInput.addEventListener("change", async () => {
      const owner = currentUploadCard;
      currentUploadCard = null;
      const file = fileInput.files && fileInput.files[0];
      if (!owner || !file) return;
      const { card, btn } = owner;
      const msg = card.querySelector(".conn-refresh-msg");
      // Front-line size check so we never send a request we already know
      // will be refused (server enforces the same cap).
      const MAX = 5 * 1024 * 1024;
      if (file.size > MAX) {
        return sayUpload(msg, messageForError("payload_too_large"), "form-error");
      }
      btn.disabled = true;
      sayUpload(msg, "Uploading " + file.name + "…");
      let text;
      try {
        text = await file.text();
      } catch (e) {
        btn.disabled = false;
        return sayUpload(msg, messageForError("invalid_body"), "form-error");
      }
      let r, d;
      try {
        r = await fetch(`/api/connections/${btn.dataset.conn}/upload`, {
          method: "POST",
          headers: { "Content-Type": "application/fhir+json" },
          body: text,
        });
        d = await r.json().catch(() => ({}));
      } catch (e) {
        btn.disabled = false;
        return sayUpload(msg, messageForError("ingest_failed"), "form-error");
      }
      btn.disabled = false;
      if (!r.ok) {
        return sayUpload(msg, uploadErrorLine(d.error, d.correlation_id),
                         "form-error");
      }
      // Success or partial success — show a plain-language summary of
      // what actually landed. When entries failed, surface the unique
      // opaque correlation ids from `errors[]` (never the raw messages
      // or objects — they can carry PHI-shaped SQL fragments) so the
      // user has a support-quotable code per distinct failure.
      // `ingested` also counts documents nothing here can open, so the line
      // leads with `records_added` and the same sentence a refresh uses.
      const ing = d.ingested | 0;
      const skp = d.skipped | 0;
      const fld = d.failed | 0;
      const readable = typeof d.records_added === "number" ? d.records_added : ing;
      const parts = [readableCountLine(readable, d.uncounted_note)];
      // Every part is a whole sentence, because the lead is one.
      if (skp) parts.push(`${skp} not saved (unsupported record types).`);
      if (fld) parts.push(`${fld} could not be saved.`);
      if (fld > 0) {
        const codes = Array.from(new Set(
          (d.errors || [])
            .map((e) => e && e.correlation_id)
            .filter(Boolean)));
        if (codes.length) {
          parts.push("Support code" + (codes.length === 1 ? "" : "s")
                     + ": " + codes.join(", ") + ".");
        }
      }
      sayUpload(msg, parts.join(" "),
          (fld || skp) ? "form-warn" : "form-ok");
      if (ing > 0) {
        // Reload so the card flips from `empty` to `active` and the
        // agent picker sees the new record count.
        setTimeout(() => location.reload(), 1400);
      }
    });
  }

  // --- what the last action did, across its reload (#847) ---
  // Delete and disconnect reload the hub so every card is redrawn from the
  // server, and the reload threw away the one sentence saying what happened.
  // The sentence rides through sessionStorage, which only this tab reads;
  // it holds a count and a sentence, never a record. Storage that is
  // blocked (private mode, some in-app browsers) costs only the sentence.
  const NOTICE_KEY = "careagents.hubNotice";
  function carryNotice(text) {
    try { if (text) sessionStorage.setItem(NOTICE_KEY, text); } catch (e) { /* reload anyway */ }
    // The reload would otherwise restore the scroll position of the card
    // that was tapped, far below the notice (seen in the 375px walk).
    try { history.scrollRestoration = "manual"; } catch (e) { /* older browsers */ }
    location.reload();
  }
  (function showCarriedNotice() {
    const el = $("hub-notice");
    if (!el) return;
    let text = null;
    try {
      text = sessionStorage.getItem(NOTICE_KEY);
      sessionStorage.removeItem(NOTICE_KEY);
    } catch (e) { text = null; }
    if (text) announce(el, text, () => el.scrollIntoView({ block: "center" }));
  })();

  // --- disconnect: ask first, stop new records, keep what's already here ---
  function askToDisconnect(label, readers) {
    $("disconnect-name").textContent = label;   // the person's label: text only
    // A disconnected connection is no longer a way to its requests (#215),
    // so an assistant reading it keeps its chat but loses its approvals.
    const who = $("disconnect-readers");
    who.textContent = readers
      ? readers + " reads these records. It can still answer questions " +
        "about them, but anything it prepares can't be approved."
      : "";
    who.hidden = !readers;
    const dlg = openDialog($("disconnect-modal"));
    $("disconnect-confirm").onclick = () => dlg.close(true);
    $("disconnect-cancel").onclick = () => dlg.close(false);
    $("disconnect-cancel").focus();
    return dlg.result;
  }
  document.querySelectorAll(".conn-disconnect").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const card = btn.closest(".conn-card");
      const msg = card.querySelector(".conn-refresh-msg");
      if (!(await askToDisconnect(btn.dataset.label || "these records",
                                  btn.dataset.readers || ""))) return;
      btn.disabled = true;
      const res = await post(`/api/connections/${btn.dataset.conn}/disconnect`);
      if (!res.ok) {
        btn.disabled = false;
        announce(msg, res.d.error || "Couldn't disconnect.");
        return;
      }
      carryNotice(res.d.message || "Disconnected.");
    });
  });

  // --- delete: purge the records themselves ---
  // Typed confirmation rather than a one-tap OK: deletion is irreversible and
  // the patient should not be able to do it by reflex.
  document.querySelectorAll(".conn-delete").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const card = btn.closest(".conn-card");
      const msg = card.querySelector(".conn-refresh-msg");
      const agreed = await askToDelete(btn.dataset.label || "these records",
                                       btn.dataset.readers || "");
      if (!agreed) return;

      btn.disabled = true;
      announce(msg, "Deleting…");
      const r = await fetch(`/api/connections/${btn.dataset.conn}`,
                            { method: "DELETE" });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) {
        btn.disabled = false;
        // Never imply a partial wipe — and never assert a whole one either
        // way. This fallback said "Your records were not deleted", which the
        // server itself can no longer say: a purge that ran and lost its
        // answer reaches here too. Announced, not just set, so the live
        // region reads it (#590).
        announce(msg, d.message ||
          "We couldn't confirm your records were deleted.");
        return;
      }
      // The count the consent box promises, shown after the reload (#847).
      carryNotice(d.message || "Your records were deleted.");
    });
  });

  // --- delete the account itself (#554): the same typed gate, then the
  // server purges every connection before the row goes. ---
  const acctBtn = $("account-delete");
  if (acctBtn) acctBtn.addEventListener("click", async () => {
    const msg = $("account-msg");
    // settings.html renders the account wording; there is no label to fill.
    const agreed = await askToDelete();
    if (!agreed) return;
    acctBtn.disabled = true;
    announce(msg, "Deleting…");
    const r = await fetch("/api/account/delete", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirm: "DELETE" }) });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) {
      acctBtn.disabled = false;
      announce(msg, d.message || "We couldn't confirm your records were deleted.");
      return;
    }
    // replace, not assign: Back must not return to the settings of an
    // account that is gone (#884 G7).
    location.replace("/?deleted=1");
  });

  // The word, in any case, with the whitespace trimmed: phones capitalise
  // the first letter, and "Delete" is plainly the person agreeing.
  const deleteTyped = (v) => v.trim().toUpperCase() === "DELETE";

  // Resolves true only after the patient types DELETE. Two gates on
  // purpose: the button ships disabled and is only enabled on a match,
  // and the click handler checks the value again — so a future markup change
  // that drops `disabled` still can't turn this into a one-tap delete.
  function askToDelete(label, readers) {
    const input = $("delete-input");
    const ok = $("delete-confirm");
    const named = $("delete-label");
    if (named) named.textContent = label;
    // Deleting a connection deletes the assistants that read it. Said
    // before the tap, by name (#853). Absent on the account page.
    const who = $("delete-readers");
    if (who) {
      who.textContent = readers
        ? readers + " reads these records and will be deleted too."
        : "";
      who.hidden = !readers;
    }
    input.value = "";
    ok.disabled = true;
    const dlg = openDialog($("delete-modal"));
    input.oninput = () => { ok.disabled = !deleteTyped(input.value); };
    ok.onclick = () => { if (deleteTyped(input.value)) dlg.close(true); };
    // Enter goes through the same check; there is no form here to submit.
    input.onkeydown = (e) => { if (e.key === "Enter") ok.onclick(); };
    $("delete-cancel").onclick = () => dlg.close(false);
    input.focus();
    return dlg.result;
  }

  // After a re-authorization, poll until the provider delivers, then report
  // the growth the server measured against the pre-refresh baseline.
  function watchForNewRecords(card, msg) {
    const tenant = card.dataset.tenant;
    let ticks = 0;
    const iv = setInterval(async () => {
      if (++ticks > 60) return clearInterval(iv);  // ~5 min, then stop quietly
      const r = await fetch(`/api/connections/${tenant}/poll`);
      const d = await r.json().catch(() => ({}));
      // Same distinction as the pending poller: an unreachable record store
      // is reported, never rendered as "nothing new yet".
      if (r.status === 503 && d.error === "records_unavailable") {
        announce(msg, d.message);
        return;
      }
      if (!r.ok) return;
      if (typeof d.new_records !== "number") return;
      // Four outcomes, not two. Gating the whole render on new_records > 0
      // dropped the case where a refresh delivers only documents: the page
      // said nothing, which a person cannot tell from a sync that did
      // nothing (#226). `uncounted_note` is a complete sentence the server
      // sends only when it established something worth saying.
      if (d.new_records === 0 && !d.uncounted_note) return;   // genuinely quiet
      announce(msg, readableCountLine(d.new_records, d.uncounted_note));
      // Only a readable record ends the watch. The document-only and
      // could-not-check messages are interim: readable records may still
      // land, and the message should upgrade rather than freeze.
      if (d.new_records > 0) clearInterval(iv);
    }, 5000);
  }

  // --- waiting for you (spec section 3) ---
  // "Checking" until the count answers. A failed or malformed answer says
  // so; it is never rendered as zero (#215, #403).
  const waiting = $("waiting");
  const requests = (n) => n + (n === 1 ? " request" : " requests");
  // A queue whose assistant was deleted has no page to open yet: say how
  // to reach it instead of linking nowhere. Names are the person's own
  // words, so they go in as text, never markup.
  // With an assistant on other records, the one Chat button opens a chat
  // that cannot see these requests: the step is to switch it (#847).
  const orphanLine = (q) => requests(q.count) + " waiting on " + q.name + ". " +
    (q.has_assistant
      ? "To review " + (q.count === 1 ? "it" : "them") + ", switch your " +
        "assistant to " + q.name + ": More, then Change records."
      : "Start a chat to review " + (q.count === 1 ? "it" : "them") + ".");

  // What became of requests already answered (#847). Before this, an
  // approval that finished left the band at "Nothing yet" with no way to the
  // result, and one that failed vanished. Each line says what happened and,
  // where there is one, the next step. Labels and names go in as text.
  // The one contact address (#856), linked by showRecent.
  const SUPPORT = "Email " + CONTACT + " to check.";
  const recentLine = (r) => {
    const what = r.label + (r.to ? " to " + r.to : "") + ": ";
    // A form with its PDF is "ready": it went nowhere, it waits for the
    // person to save, print or send it (#853).
    if (r.state === "done") return { text: what + (r.link ? "Ready." : "Done."),
      link: r.link ? { href: r.link, text: "Open the PDF", away: true } : null };
    if (r.state === "failed") return { text: what + "Didn't finish.",
      link: r.chat ? { href: r.chat, text: "Ask " + r.agent_name + " to try again" } : null,
      after: r.chat ? ""
        : r.has_assistant
          ? " To try again, switch your assistant to " + r.records +
            ": More, then Change records."
          : " Start a chat to try again." };
    if (r.state === "in_progress") return { text: what + "In progress." };
    if (r.state === "needs_review") return { text: what +
      "Didn't finish. We couldn't confirm it went through. " + SUPPORT };
    if (r.state === "unknown") return { text: what +
      "Didn't finish. It may have gone out, so it won't be sent again. " + SUPPORT };
    return null;
  };
  function showRecent(d) {
    if (d.recent_unavailable) {
      const p = document.createElement("p");
      p.className = "recent-note";
      p.textContent = "Couldn't check what happened to earlier requests.";
      waiting.appendChild(p);
      return;
    }
    if (!Array.isArray(d.recent) || !d.recent.length) return;
    const head = document.createElement("h3");
    head.className = "recent-head";
    head.textContent = "Recently";
    const list = document.createElement("ul");
    list.className = "recent-list";
    d.recent.forEach((r) => {
      const line = recentLine(r);
      if (!line) return;
      const li = document.createElement("li");
      li.textContent = line.text;
      linkContact(li);   // before any link below: it rewrites the text
      if (line.link && /^(https?:\/\/|\/)/.test(line.link.href)) {
        const a = document.createElement("a");
        a.href = line.link.href;
        a.textContent = line.link.text;
        if (line.link.away) { a.target = "_blank"; a.rel = "noopener"; }
        li.append(" ", a);
      }
      if (line.after) li.append(line.after);
      list.appendChild(li);
    });
    if (list.children.length) waiting.append(head, list);
  }

  function showWaiting(state, d) {
    const line = waiting.querySelector(".waiting-line");
    waiting.dataset.state = state;
    if (state === "fail") { line.textContent = "Couldn't check for requests."; return; }
    showRecent(d);
    if (state === "none") {
      // "Nothing yet" above a list of finished requests reads as though
      // they never happened.
      line.textContent = Array.isArray(d.recent) && d.recent.length
        ? "Nothing waiting for you now."
        : "Nothing yet. Anything your assistant prepares, " +
          "like a form or a reminder, waits here for your OK.";
      return;
    }
    // The approvals page reads one assistant's records, so each assistant
    // with something waiting is its own link. One queue reads as one line.
    const queues = Array.isArray(d.queues) && d.queues.length
      ? d.queues : [{ href: d.href, count: d.count }];
    if (queues.length === 1) {
      if (!queues[0].href) { line.textContent = orphanLine(queues[0]); return; }
      const a = document.createElement("a");
      a.href = queues[0].href;
      a.textContent = requests(d.count) + " waiting for your approval";
      line.replaceChildren(a);
      return;
    }
    line.textContent = requests(d.count) + " waiting for your approval:";
    const list = document.createElement("ul");
    list.className = "waiting-queues";
    queues.forEach((q) => {
      const li = document.createElement("li");
      if (q.href) {
        const a = document.createElement("a");
        a.href = q.href;
        a.textContent = q.name + ": " + requests(q.count);
        li.appendChild(a);
      } else {
        li.textContent = orphanLine(q);
      }
      list.appendChild(li);
    });
    line.after(list);
  }
  if (waiting) {
    fetch("/api/approvals/count").then(async (r) => {
      const d = await r.json().catch(() => ({}));
      if (!r.ok || typeof d.count !== "number") return showWaiting("fail", d);
      showWaiting(d.count > 0 ? "pending" : "none", d);
    }).catch(() => showWaiting("fail", {}));
  }

  // --- switch to your records (spec section 5) ---
  const sw = $("switch-prompt");
  if (sw) {
    const answer = async (a) => {
      const res = await post("/api/hub/switch-prompt", {
        answer: a, agent_id: sw.dataset.agent, connection_id: sw.dataset.conn });
      if (!res.ok) return announce($("switch-msg"), "That didn't work. Try again.");
      location.reload();
    };
    $("switch-yes").addEventListener("click", () => answer("switch"));
    $("switch-later").addEventListener("click", () => answer("later"));
  }

  // --- your assistant: start, rename, change records, delete ---
  // Start a chat: an account with records and no assistant (its first one
  // was deleted) gets one with the first-run defaults. The server checks
  // the connection is this account's, as for any new assistant.
  const startChat = $("start-chat");
  if (startChat) startChat.addEventListener("click", async () => {
    startChat.disabled = true;
    const res = await post("/api/agents", {
      name: "Juniper", persona: "calm", connection_id: startChat.dataset.conn });
    if (res.ok) { location.href = "/chat?agent=" + res.d.id; return; }
    startChat.disabled = false;
    announce($("start-chat-msg"), "Couldn't start a chat. Refresh and try again.");
  });

  const agentMsg = (btn) => btn.closest(".agent-card").querySelector(".agent-msg");

  function askForName(current) {
    const input = $("rename-input");
    input.value = current || "";
    const dlg = openDialog($("rename-modal"));
    $("rename-save").onclick = () => dlg.close(input.value.trim() || null);
    input.onkeydown = (e) => { if (e.key === "Enter") $("rename-save").onclick(); };
    $("rename-cancel").onclick = () => dlg.close(null);
    input.focus();
    return dlg.result;
  }
  document.querySelectorAll(".agent-rename").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const name = await askForName(btn.dataset.name);
      if (!name) return;
      const res = await post(`/api/agents/${btn.dataset.agent}/rename`, { name });
      if (!res.ok) return announce(agentMsg(btn), res.d.error || "Couldn't rename.");
      location.reload();
    });
  });

  let moveChoices = [];
  try { moveChoices = JSON.parse(($("agents") && $("agents").dataset.records) || "[]"); }
  catch (e) { moveChoices = []; }
  function pickRecords(currentId) {
    const rows = $("records-rows");
    rows.textContent = "";
    const dlg = openDialog($("records-picker"));
    moveChoices.filter((c) => c.id !== currentId).forEach((c) => {
      const row = document.createElement("button");
      row.type = "button";
      row.className = "picker-row";
      row.textContent = c.label;   // server-supplied label: text, never markup
      row.addEventListener("click", () => dlg.close(c.id));
      rows.appendChild(row);
    });
    $("records-cancel").onclick = () => dlg.close(null);
    const first = rows.querySelector(".picker-row");
    if (first) first.focus();
    return dlg.result;
  }
  document.querySelectorAll(".agent-move").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const conn = await pickRecords(btn.dataset.conn);
      if (!conn) return;
      const res = await post(`/api/agents/${btn.dataset.agent}/connection`,
                             { connection_id: conn });
      if (!res.ok) {
        return announce(agentMsg(btn), "Those records aren't available. Refresh and try again.");
      }
      location.reload();
    });
  });

  function askToRemoveAgent(name) {
    $("agent-delete-name").textContent = name;
    const dlg = openDialog($("agent-delete-modal"));
    $("agent-delete-confirm").onclick = () => dlg.close(true);
    $("agent-delete-cancel").onclick = () => dlg.close(false);
    $("agent-delete-cancel").focus();
    return dlg.result;
  }
  document.querySelectorAll(".agent-delete").forEach((btn) => {
    btn.addEventListener("click", async () => {
      if (!(await askToRemoveAgent(btn.dataset.name))) return;
      const r = await fetch(`/api/agents/${btn.dataset.agent}`, { method: "DELETE" });
      if (!r.ok) return announce(agentMsg(btn), "Couldn't delete. Try again.");
      location.reload();
    });
  });

  // --- iMessage surface (settings page) ---
  // The page names the assistant to bind on the tile itself: settings has
  // no assistant cards to read one from.
  const im = $("im-surface");
  if (im) im.addEventListener("click", async () => {
    $("surfaces-msg").hidden = true;
    $("surfaces-msg").classList.remove("is-ok");
    const agentId = im.dataset.agent;
    if (!agentId) {
      return say(im, $("surfaces-msg"),
        "Start a chat with your assistant first, then connect iMessage.");
    }
    const res = await post("/api/surfaces/imessage", { agent_id: agentId });
    if (!res.ok) return say(im, $("surfaces-msg"), res.d.error || "Failed");
    $("im-state").textContent = "pending — text to finish";
    // iMessage needs the whole "care <code>" line as the text body.
    showCodeCard("care " + res.d.code, res.d.instructions || "Text this code to connect:");
  });
  // Disconnect, one phone per tile: its texts stop reaching the assistant.
  // Texting again later sends a fresh sign-in link. Success is said in the
  // calm style; only a failure uses the error colour.
  document.querySelectorAll(".im-disconnect").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const tile = btn.closest(".surface");
      const msg = $("surfaces-msg");
      btn.disabled = true;
      const res = await post("/api/surfaces/imessage/disconnect",
                             { surface_id: btn.dataset.surface });
      if (!res.ok) {
        btn.disabled = false;
        msg.classList.remove("is-ok");
        return say(tile, msg, "Couldn't disconnect. Try again.");
      }
      btn.remove();
      tile.classList.remove("on");
      tile.querySelector(".im-state").textContent = "disconnected";
      msg.classList.add("is-ok");
      say(tile, msg,
        "Disconnected. Texts from that phone won't reach your assistant.");
    });
  });
  // --- grants: revoke a consent given to a third-party agent (spec §13.4) ---
  // HealthClaw is asked first; the card changes only on its yes, and a
  // failure is announced on the card's live region, never assumed away.
  document.querySelectorAll(".grant-revoke").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const card = btn.closest(".grant-card");
      const msg = card && card.querySelector(".grant-msg");
      btn.disabled = true;
      const res = await post(`/api/grants/${btn.dataset.grant}/revoke`, {});
      if (res.ok && res.d.revoked) {
        btn.remove();
        const status = card && card.querySelector(".status");
        if (status) { status.textContent = "revoked"; status.className = "status status-revoked"; }
      } else {
        btn.disabled = false;
        if (msg) announce(msg, res.d.message || res.d.error || "That didn't work.");
      }
    });
  });
})();
