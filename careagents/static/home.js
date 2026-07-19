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
      if (tile.dataset.providers) {
        const provs = JSON.parse(tile.dataset.providers);
        const pick = prompt("Which do you use?\n" +
          provs.map((p, i) => `${i + 1}. ${p.label}`).join("\n"), "1");
        const idx = parseInt(pick, 10) - 1;
        if (isNaN(idx) || !provs[idx]) return;
        body.provider = provs[idx].id;
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
      if (!res.ok) return alert(res.d.error || "Couldn't connect that source.");
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

  // --- new agent modal ---
  const modal = $("agent-modal");
  const hasConn = () => $("a-conn") && $("a-conn").options.length > 0;

  // With no records connected there's nothing to build an agent on — send the
  // user to the connect step (highlight it) instead of opening a dead modal.
  function needConnection() {
    const sec = $("connect-section");
    if (sec) {
      sec.scrollIntoView({ behavior: "smooth", block: "center" });
      sec.classList.add("flash");
      setTimeout(() => sec.classList.remove("flash"), 1400);
    }
  }
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
    if (!firstAgent) { alert("Create an agent first, then connect Telegram."); return; }
    const agentId = new URL(firstAgent.href).searchParams.get("agent");
    const res = await post("/api/surfaces/telegram", { agent_id: agentId });
    if (!res.ok) return alert(res.d.error || "Failed");
    if (res.d.deep_link) { $("tg-state").textContent = "opening…"; window.open(res.d.deep_link, "_blank", "noopener"); }
    else prompt("Send this code to the CareAgents bot with /start:", res.d.code);
    $("tg-state").textContent = "pending — finish in Telegram";
  });

  // --- iMessage surface ---
  const im = $("im-surface");
  if (im) im.addEventListener("click", async () => {
    const firstAgent = document.querySelector(".agent-card");
    if (!firstAgent) { alert("Create an agent first, then connect iMessage."); return; }
    const agentId = new URL(firstAgent.href).searchParams.get("agent");
    const res = await post("/api/surfaces/imessage", { agent_id: agentId });
    if (!res.ok) return alert(res.d.error || "Failed");
    $("im-state").textContent = "pending — text to finish";
    prompt(res.d.instructions || "Text this code to connect:", "care " + res.d.code);
  });
})();
