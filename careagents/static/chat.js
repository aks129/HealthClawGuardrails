/* CareAgents chat — SSE over fetch, tool chips, review/PDF cards,
   typewriter render. No frameworks. */
(function () {
  const log = document.getElementById("log");
  const box = document.getElementById("box");
  const composer = document.getElementById("composer");
  const sendBtn = document.getElementById("send");
  const starters = document.getElementById("starters");
  let busy = false;
  const pollers = {};

  function scroll() { log.scrollTop = log.scrollHeight; }

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text) n.textContent = text;
    return n;
  }

  function addUser(text) {
    log.appendChild(el("div", "msg user", text));
    scroll();
  }

  function addTyping() {
    const t = el("div", "msg agent typing");
    t.innerHTML = "<i></i><i></i><i></i>";
    log.appendChild(t); scroll();
    return t;
  }

  function typewrite(node, text) {
    let i = 0;
    const step = Math.max(1, Math.round(text.length / 120));
    (function tick() {
      i = Math.min(text.length, i + step);
      node.textContent = text.slice(0, i);
      scroll();
      if (i < text.length) requestAnimationFrame(tick);
    })();
  }

  function addAgentText(text) {
    const m = el("div", "msg agent");
    log.appendChild(m);
    typewrite(m, text);
  }

  function addChip(label) {
    let chips = log.lastElementChild;
    if (!chips || !chips.classList.contains("chips")) {
      chips = el("div", "chips");
      log.appendChild(chips);
    }
    chips.appendChild(el("span", "chip", label));
    scroll();
  }

  function addReviewCard(actionId, url) {
    const c = el("div", "card");
    c.appendChild(el("h4", null, "Review & approve your intake form"));
    c.appendChild(el("p", null,
      "Your agent filled it from the records — now every medication and " +
      "allergy waits for your say-so. Nothing is generated until you approve."));
    const a = el("a", "btn-primary", "Open the review");
    a.href = url; a.target = "_blank"; a.rel = "noopener";
    c.appendChild(a);
    log.appendChild(c); scroll();
    watchForm(actionId);
  }

  function addPdfCard(url) {
    const c = el("div", "card pdf");
    c.appendChild(el("h4", null, "Your intake form is ready"));
    c.appendChild(el("p", null,
      "Reviewed by you, provenance-stamped, and delivered over a signed link."));
    const a = el("a", "btn-primary", "Open the PDF");
    a.href = url; a.target = "_blank"; a.rel = "noopener";
    c.appendChild(a);
    log.appendChild(c); scroll();
  }

  function watchForm(actionId) {
    if (pollers[actionId]) return;
    pollers[actionId] = setInterval(async () => {
      try {
        const r = await fetch("/api/form/" + actionId);
        if (!r.ok) return;
        const d = await r.json();
        if (d.status === "completed" && d.delivery_link) {
          clearInterval(pollers[actionId]);
          addPdfCard(d.delivery_link);
          addAgentText("All set — you approved it, so I generated the PDF. " +
                       "It’s stamped with how it was made and that you reviewed it.");
        }
      } catch (e) { /* keep polling */ }
    }, 4000);
  }

  async function send(text) {
    if (busy || !text.trim()) return;
    busy = true; sendBtn.disabled = true;
    if (starters) starters.remove();
    addUser(text);
    box.value = "";
    const typing = addTyping();

    try {
      const resp = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text }),
      });
      if (resp.status === 429) {
        typing.remove();
        addAgentText("You’ve hit the pace limit for now — give it a few minutes.");
        return;
      }
      if (!resp.ok || !resp.body) {
        typing.remove();
        addAgentText("I couldn’t reach my tools just now. Try again in a moment.");
        return;
      }
      const reader = resp.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let idx;
        while ((idx = buf.indexOf("\n\n")) >= 0) {
          const frame = buf.slice(0, idx); buf = buf.slice(idx + 2);
          if (!frame.startsWith("data: ")) continue;
          let ev;
          try { ev = JSON.parse(frame.slice(6)); } catch (e) { continue; }
          if (ev.type === "tool") addChip(ev.label);
          else if (ev.type === "card" && ev.kind === "review")
            addReviewCard(ev.action_id, ev.review_url);
          else if (ev.type === "card" && ev.kind === "pdf")
            addPdfCard(ev.url);
          else if (ev.type === "text") { typing.remove(); addAgentText(ev.text); }
          else if (ev.type === "error") { typing.remove(); addAgentText("⚠️ " + ev.text); }
        }
      }
      if (typing.parentNode) typing.remove();
    } catch (e) {
      if (typing.parentNode) typing.remove();
      addAgentText("Connection hiccup — try that again.");
    } finally {
      busy = false; sendBtn.disabled = false; box.focus();
    }
  }

  composer.addEventListener("submit", (e) => { e.preventDefault(); send(box.value); });
  document.querySelectorAll(".starter").forEach((b) =>
    b.addEventListener("click", () => send(b.textContent)));

  const saveBtn = document.getElementById("save-btn");
  if (saveBtn) saveBtn.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(saveBtn.dataset.link);
      saveBtn.textContent = "link copied ✓";
      setTimeout(() => (saveBtn.textContent = "save my agent"), 2500);
    } catch (e) { window.prompt("Your private agent link:", saveBtn.dataset.link); }
  });

  fetch("/api/trust").then((r) => r.json()).then((d) => {
    const pill = document.getElementById("trust-pill");
    const grade = d.badge && d.badge !== "unavailable" ? d.badge.split(" ")[0] : "—";
    pill.textContent = "guardrails " + grade +
      (d.audit_events ? " · " + d.audit_events + " audited events" : "");
  }).catch(() => {});

  box.focus();
})();
