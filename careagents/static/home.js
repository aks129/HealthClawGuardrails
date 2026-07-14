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

  // --- add sample connection ---
  $("add-sample").addEventListener("click", async (e) => {
    e.target.disabled = true; e.target.textContent = "Creating…";
    const res = await post("/api/connections/sample");
    if (res.ok) location.reload();
    else { e.target.disabled = false; e.target.textContent = "Add sample records"; alert(res.d.error || "Failed"); }
  });

  // --- connect real records (Fasten, verified provider) ---
  $("add-fasten").addEventListener("click", async (e) => {
    e.target.disabled = true;
    const res = await post("/api/connections/fasten");
    e.target.disabled = false;
    if (!res.ok) return alert(res.d.error || "Real-records connect isn't set up here yet.");
    // open the provider picker; the pending card (after reload) polls to active
    window.open(res.d.connect_url, "_blank", "noopener");
    location.reload();
  });

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
  $("new-agent-btn").addEventListener("click", () => {
    if (!$("a-conn").options.length) { alert("Add a connection first."); return; }
    modal.hidden = false;
  });
  $("close-modal").addEventListener("click", () => (modal.hidden = true));
  $("create-agent").addEventListener("click", async () => {
    const persona = document.querySelector('input[name="ag-persona"]:checked');
    const res = await post("/api/agents", {
      name: $("a-name").value.trim() || "Juniper",
      persona: persona ? persona.value : "calm",
      connection_id: $("a-conn").value,
    });
    if (res.ok) location.href = "/chat?agent=" + res.d.id;
    else { const e = $("modal-err"); e.textContent = res.d.error || "Failed"; e.hidden = false; }
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
})();
