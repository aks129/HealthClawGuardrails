/* CareAgents hub — add connections (sample/Fasten), create agents, connect
   Telegram. Small vanilla JS; the server is authoritative. */
(function () {
  const $ = (id) => document.getElementById(id);
  async function post(url, body) {
    const r = await fetch(url, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    return { ok: r.ok, d: await r.json().catch(() => ({})) };
  }

  // --- connector marketplace: one handler for every tile ---
  document.querySelectorAll(".connector-tile").forEach((tile) => {
    tile.addEventListener("click", async () => {
      const id = tile.dataset.connector;
      if (tile.dataset.soon) {
        await post("/api/connections/" + id);  // records intent, never errors
        tile.querySelector(".connector-tag").textContent = "we'll let you know";
        return;
      }
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
      }
      tile.disabled = true;
      const res = await post("/api/connections/" + id, body);
      tile.disabled = false;
      if (!res.ok) {
        // Inline, directly under the tile that was tapped: never blocks, never
        // needs dismissing, and the page stays usable.
        return say(tile, $("connect-msg"),
                   res.d.error || "Couldn't connect that source.");
      }
      if (res.d.soon) { tile.querySelector(".connector-tag").textContent = "we'll let you know"; return; }
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

  // Inline message: shown in the page beside what the user touched.
  //
  // The element is MOVED next to `anchor` before it is shown. A message that
  // renders in its template position can land hundreds of pixels below the
  // fold on a phone — which looks exactly like the dead browser dialog this
  // replaced, so the position is part of the fix, not decoration. Moving the
  // one element keeps the id stable for anything addressing it.
  function say(anchor, el, text) {
    if (anchor && el.previousElementSibling !== anchor) {
      anchor.insertAdjacentElement("afterend", el);
    }
    el.textContent = text;
    el.hidden = false;
    // Only scrolls if it isn't already fully visible, and only as far as it
    // has to — no jump when the message is already under the user's thumb.
    // Never while a dialog is up: the message is behind the overlay, so the
    // scroll moves nothing the user can see and everything they come back to
    // (#269). Open state is the absence of `hidden` — that attribute is what
    // openDialog() and the consent card toggle.
    if (!document.querySelector(".modal:not([hidden])")) {
      el.scrollIntoView({ block: "nearest" });
    }
  }

  // Scroll a section into view and pulse it — the in-page way to point at the
  // step that has to happen first. The message rides along to the section
  // being scrolled to, or the words explaining the flash end up off-screen in
  // the section the user just left.
  function flashSection(el, msgEl, text) {
    if (!el) { if (msgEl) say(null, msgEl, text); return; }
    if (msgEl) {
      el.insertAdjacentElement("afterend", msgEl);
      msgEl.textContent = text;
      msgEl.hidden = false;
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
    const iv = setInterval(async () => {
      const r = await fetch(`/api/connections/${tenant}/poll`);
      if (!r.ok) return;
      const d = await r.json();
      if (d.status === "active") { clearInterval(iv); location.reload(); }
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
      const report = (t) => { msg.textContent = t; msg.hidden = false; };
      btn.disabled = true;
      report("Checking…");
      let res = await post(`/api/connections/${btn.dataset.conn}/refresh`);
      // 428 means this deployment wants consent re-affirmed for the re-pull.
      if (!res.ok && res.d.error === "consent_required") {
        const agreed = await showConsentCard();
        if (!agreed) { btn.disabled = false; msg.hidden = true; return; }
        res = await post(`/api/connections/${btn.dataset.conn}/refresh`,
                         { consent: true });
      }
      btn.disabled = false;
      if (!res.ok) return report(res.d.error || "Couldn't refresh right now.");
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
      "Something went wrong saving the records. Quote the code below to " +
      "support if you need to reach us.",
    ingest_failed:
      "The records service couldn't accept this upload. Try again in a " +
      "moment or contact support with the code below.",
  };
  function messageForError(code) {
    return UPLOAD_MSG[code] || (
      "The upload didn't go through. If this keeps happening, quote the " +
      "code below to support.");
  }

  // Reused file input — the current owner card is tracked here.
  const fileInput = $("upload-file");
  let currentUploadCard = null;

  function sayUpload(msg, text, cls) {
    msg.textContent = text;
    msg.className = "conn-refresh-msg" + (cls ? " " + cls : "");
    msg.hidden = false;
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
        let line = messageForError(d.error);
        if (d.correlation_id) line += " Support code: " + d.correlation_id + ".";
        return sayUpload(msg, line, "form-error");
      }
      // Success or partial success — show a plain-language summary of
      // what actually landed. When entries failed, surface the unique
      // opaque correlation ids from `errors[]` (never the raw messages
      // or objects — they can carry PHI-shaped SQL fragments) so the
      // user has a support-quotable code per distinct failure.
      const ing = d.ingested | 0;
      const skp = d.skipped | 0;
      const fld = d.failed | 0;
      const parts = [`${ing} record${ing === 1 ? "" : "s"} added`];
      if (skp) parts.push(`${skp} not saved (unsupported record types)`);
      if (fld) parts.push(`${fld} could not be saved`);
      if (fld > 0) {
        const codes = Array.from(new Set(
          (d.errors || [])
            .map((e) => e && e.correlation_id)
            .filter(Boolean)));
        if (codes.length) {
          parts.push("Support code" + (codes.length === 1 ? "" : "s")
                     + ": " + codes.join(", "));
        }
      }
      sayUpload(msg, parts.join(" · "),
          (fld || skp) ? "form-warn" : "form-ok");
      if (ing > 0) {
        // Reload so the card flips from `empty` to `active` and the
        // agent picker sees the new record count.
        setTimeout(() => location.reload(), 1400);
      }
    });
  }

  // --- disconnect: stop new records, keep what's already here ---
  document.querySelectorAll(".conn-disconnect").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const card = btn.closest(".conn-card");
      const msg = card.querySelector(".conn-refresh-msg");
      btn.disabled = true;
      const res = await post(`/api/connections/${btn.dataset.conn}/disconnect`);
      if (!res.ok) {
        btn.disabled = false;
        msg.textContent = res.d.error || "Couldn't disconnect.";
        msg.hidden = false;
        return;
      }
      location.reload();
    });
  });

  // --- delete: purge the records themselves ---
  // Typed confirmation rather than a one-tap OK: deletion is irreversible and
  // the patient should not be able to do it by reflex.
  document.querySelectorAll(".conn-delete").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const card = btn.closest(".conn-card");
      const msg = card.querySelector(".conn-refresh-msg");
      const agreed = await askToDelete(btn.dataset.label || "these records");
      if (!agreed) return;

      btn.disabled = true;
      msg.textContent = "Deleting…";
      msg.hidden = false;
      const r = await fetch(`/api/connections/${btn.dataset.conn}`,
                            { method: "DELETE" });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) {
        btn.disabled = false;
        // Say plainly that nothing was deleted — never imply a partial wipe.
        msg.textContent = d.message || "Your records were not deleted.";
        return;
      }
      location.reload();
    });
  });

  // Resolves true only after the patient types DELETE exactly. Two gates on
  // purpose: the button ships disabled and is only enabled on an exact match,
  // and the click handler checks the value again — so a future markup change
  // that drops `disabled` still can't turn this into a one-tap delete.
  function askToDelete(label) {
    const input = $("delete-input");
    const ok = $("delete-confirm");
    $("delete-label").textContent = label;
    input.value = "";
    ok.disabled = true;
    const dlg = openDialog($("delete-modal"));
    input.oninput = () => { ok.disabled = input.value !== "DELETE"; };
    ok.onclick = () => { if (input.value === "DELETE") dlg.close(true); };
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
      if (!r.ok) return;
      const d = await r.json();
      if (typeof d.new_records === "number" && d.new_records > 0) {
        clearInterval(iv);
        msg.textContent = `${d.new_records} new record` +
          (d.new_records === 1 ? "" : "s") + " added.";
      }
    }, 5000);
  }

  // --- new agent modal ---
  const modal = $("agent-modal");
  const hasConn = () => $("a-conn") && $("a-conn").options.length > 0;

  // With no records connected there's nothing to build an agent on — send the
  // user to the connect step (highlight it) instead of opening a dead modal.
  function needConnection() { flashSection($("connect-section"), null, ""); }
  function openAgentModal() {
    if (!hasConn()) { needConnection(); return; }
    $("modal-err").hidden = true;
    modal.hidden = false;
    $("a-name").focus();
  }
  $("new-agent-btn").addEventListener("click", openAgentModal);
  const emptyCta = $("empty-new-agent");
  if (emptyCta) emptyCta.addEventListener("click", openAgentModal);

  $("close-modal").addEventListener("click", () => (modal.hidden = true));
  modal.addEventListener("click", (e) => { if (e.target === modal) modal.hidden = true; });
  $("create-agent").addEventListener("click", async () => {
    const conn = $("a-conn").value;
    if (!conn) { const e = $("modal-err"); e.textContent = "Connect records first."; e.hidden = false; return; }
    const persona = document.querySelector('input[name="ag-persona"]:checked');
    const btn = $("create-agent");
    btn.disabled = true; btn.textContent = "Creating…";
    const res = await post("/api/agents", {
      name: $("a-name").value.trim() || "Juniper",
      persona: persona ? persona.value : "calm",
      advisor: ($("a-advisor") && $("a-advisor").value) || "general",
      connection_id: conn,
    });
    if (res.ok) { location.href = "/chat?agent=" + res.d.id; return; }
    btn.disabled = false; btn.textContent = "Create";
    const e = $("modal-err"); e.textContent = res.d.error || "Failed"; e.hidden = false;
  });

  // --- Telegram surface ---
  const tg = $("tg-surface");
  if (tg) tg.addEventListener("click", async () => {
    const firstAgent = document.querySelector(".agent-card");
    $("surfaces-msg").hidden = true;
    if (!firstAgent) {
      return flashSection($("agents"), $("surfaces-msg"),
        "Create an agent first, then connect Telegram.");
    }
    const agentId = new URL(firstAgent.href).searchParams.get("agent");
    const res = await post("/api/surfaces/telegram", { agent_id: agentId });
    if (!res.ok) return say(tg, $("surfaces-msg"), res.d.error || "Failed");
    if (res.d.deep_link) { $("tg-state").textContent = "opening…"; window.open(res.d.deep_link, "_blank", "noopener"); }
    // Telegram pairs on the bare code, so that is what we show and copy.
    else showCodeCard(res.d.code, "Send this code to the CareAgents bot with /start:");
    $("tg-state").textContent = "pending — finish in Telegram";
  });

  // --- iMessage surface ---
  const im = $("im-surface");
  if (im) im.addEventListener("click", async () => {
    const firstAgent = document.querySelector(".agent-card");
    $("surfaces-msg").hidden = true;
    if (!firstAgent) {
      return flashSection($("agents"), $("surfaces-msg"),
        "Create an agent first, then connect iMessage.");
    }
    const agentId = new URL(firstAgent.href).searchParams.get("agent");
    const res = await post("/api/surfaces/imessage", { agent_id: agentId });
    if (!res.ok) return say(im, $("surfaces-msg"), res.d.error || "Failed");
    $("im-state").textContent = "pending — text to finish";
    // iMessage needs the whole "care <code>" line as the text body.
    showCodeCard("care " + res.d.code, res.d.instructions || "Text this code to connect:");
  });
})();
