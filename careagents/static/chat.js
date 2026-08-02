/* CareAgents chat — SSE over fetch, tool chips, review/PDF cards,
   typewriter render. No frameworks. */
(function () {
  const AGENT = window.CARE_AGENT || "";
  const CONVERSATION = window.CARE_CONVERSATION || "";
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
    a.href = "/review/" + AGENT + "/" + actionId;
    a.target = "_blank"; a.rel = "noopener";
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
        const r = await fetch("/api/form/" + actionId + "?agent=" + encodeURIComponent(AGENT));
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

  function pause(ms) { return new Promise((resolve) => setTimeout(resolve, ms)); }

  async function consumeEvents(resp, state, typing) {
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
        const lines = frame.split("\n");
        const idLine = lines.find((line) => line.startsWith("id: "));
        const dataLine = lines.find((line) => line.startsWith("data: "));
        if (idLine) state.cursor = Math.max(
          state.cursor, parseInt(idLine.slice(4), 10) || 0);
        if (!dataLine) continue;
        let ev;
        try { ev = JSON.parse(dataLine.slice(6)); } catch (e) { continue; }
        if (ev.type === "accepted") {
          state.runId = ev.run_id;
          state.cursor = Math.max(state.cursor, ev.next_cursor || 0);
        } else if (ev.type === "tool") addChip(ev.label);
        else if (ev.type === "card" && ev.kind === "review")
          addReviewCard(ev.action_id, ev.review_url);
        else if (ev.type === "card" && ev.kind === "pdf")
          addPdfCard(ev.url);
        else if (ev.type === "text") {
          typing.remove(); addAgentText(ev.text);
        } else if (ev.type === "error") {
          typing.remove(); addAgentText("⚠️ " + ev.text);
        } else if (ev.type === "done") {
          state.done = true;
        }
      }
    }
  }

  async function send(text) {
    if (busy || !text.trim()) return;
    busy = true; sendBtn.disabled = true;
    if (starters) starters.remove();
    addUser(text);
    box.value = "";
    const typing = addTyping();
    const requestId = (window.crypto && window.crypto.randomUUID)
      ? window.crypto.randomUUID()
      : Date.now().toString(36) + "-" + Math.random().toString(36).slice(2);

    try {
      const state = { runId: null, cursor: 0, done: false };
      let initial = true;
      let reconnectFailures = 0;
      while (!state.done) {
        try {
          let resp;
          if (initial || !state.runId) {
            resp = await fetch("/api/chat", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ message: text, agent_id: AGENT,
                                     conversation_id: CONVERSATION,
                                     request_id: requestId,
                                     after: state.cursor }),
            });
          } else {
            const query = new URLSearchParams({
              agent_id: AGENT, after: String(state.cursor),
            });
            resp = await fetch("/api/chat/runs/" +
              encodeURIComponent(state.runId) + "/events?" + query.toString());
          }
          initial = false;
          if (resp.status === 429) {
            typing.remove();
            addAgentText("You’ve hit the pace limit for now — give it a few minutes.");
            return;
          }
          if (!resp.ok || !resp.body) throw new Error("event stream unavailable");
          const responseRunId = resp.headers.get("X-CareAgents-Run-ID");
          if (responseRunId) state.runId = responseRunId;
          await consumeEvents(resp, state, typing);
          reconnectFailures = 0;
        } catch (streamError) {
          reconnectFailures += 1;
          // If the POST reached the server but its response was lost before
          // the accepted event/header arrived, retrying with the same durable
          // request ID retrieves the same message/run instead of inferring
          // twice. Once the run ID is known, reconnect with GET only.
          if (reconnectFailures > 5) throw streamError;
        }
        if (!state.done) await pause(400);
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

  fetch("/api/trust").then((r) => r.json()).then((d) => {
    const pill = document.getElementById("trust-pill");
    const grade = d.badge && d.badge !== "unavailable" ? d.badge.split(" ")[0] : "—";
    pill.textContent = "guardrails " + grade;
  }).catch(() => {});

  box.focus();
})();
