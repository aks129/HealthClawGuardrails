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

  // --- lab timeline ------------------------------------------------------
  // Rendered inline, NOT as an iframe of HealthClaw's lab-trends MCP App.
  // That app is same-origin to the engine and authenticates with a step-up
  // token; embedded here it would either 401 or need a credential in a URL.
  // The server fetches with the credentials it already holds and hands back
  // series only.

  const SVG_NS = "http://www.w3.org/2000/svg";

  function svgEl(name, attrs) {
    const node = document.createElementNS(SVG_NS, name);
    for (const key in attrs) node.setAttribute(key, attrs[key]);
    return node;
  }

  function drawSeries(series) {
    const W = 520, H = 150, padL = 40, padR = 10, padT = 12, padB = 22;
    const pts = series.readings.filter((r) => r.date);
    const svg = svgEl("svg", {
      class: "spark", viewBox: "0 0 " + W + " " + H,
      preserveAspectRatio: "none", role: "img",
      "aria-label": series.name + " over time"
    });

    const values = pts.map((p) => p.value);
    let lo = Math.min.apply(null, values), hi = Math.max.apply(null, values);
    if (hi === lo) { hi = lo + 1; lo = lo - 1; }
    const span = (hi - lo) * 0.15; lo -= span; hi += span;
    const times = pts.map((p) => new Date(p.date + "T00:00:00Z").getTime());
    const t0 = Math.min.apply(null, times);
    const t1 = Math.max.apply(null, times) === t0
      ? t0 + 1 : Math.max.apply(null, times);
    const x = (t) => padL + ((t - t0) / (t1 - t0)) * (W - padL - padR);
    const y = (v) => padT + ((hi - v) / (hi - lo)) * (H - padT - padB);

    [0, 0.5, 1].forEach((frac) => {
      const gy = padT + frac * (H - padT - padB);
      svg.appendChild(svgEl("line", {
        class: "spark-grid", x1: padL, x2: W - padR, y1: gy, y2: gy }));
      const label = svgEl("text", { class: "spark-axis", x: 2, y: gy + 3 });
      label.textContent = (hi - frac * (hi - lo)).toFixed(0);
      svg.appendChild(label);
    });

    // A single reading has no direction: draw the point, never a line.
    if (series.trend_plottable) {
      svg.appendChild(svgEl("path", {
        class: "spark-line",
        d: pts.map((p, i) => (i ? "L" : "M") + x(times[i]).toFixed(1) +
          " " + y(p.value).toFixed(1)).join(" ")
      }));
    }
    pts.forEach((p, i) => {
      const dot = svgEl("circle", {
        class: "spark-pt " + (p.flag || "IND"),
        cx: x(times[i]).toFixed(1), cy: y(p.value).toFixed(1), r: 4 });
      const title = document.createElementNS(SVG_NS, "title");
      title.textContent = p.date + ": " + p.value + " " + (p.unit || "") +
        " (" + (p.flag || "IND") + ")";
      dot.appendChild(title);
      svg.appendChild(dot);
    });

    [[t0, "start"], [t1, "end"]].forEach((pair) => {
      const label = svgEl("text", {
        class: "spark-axis", x: x(pair[0]), y: H - 5,
        "text-anchor": pair[1] === "end" ? "end" : "start" });
      label.textContent = new Date(pair[0]).toISOString().slice(0, 10);
      svg.appendChild(label);
    });
    return svg;
  }

  async function addLabTimelineCard(topic) {
    const c = el("div", "card timeline");
    c.appendChild(el("h4", null, "Your results over time"));
    const body = el("div", "timeline-body");
    body.appendChild(el("p", "muted", "Loading your readings…"));
    c.appendChild(body);
    log.appendChild(c); scroll();

    let data;
    try {
      const url = "/api/labs/timeline?agent=" + encodeURIComponent(AGENT) +
        (topic ? "&topic=" + encodeURIComponent(topic) : "");
      const res = await fetch(url, { headers: { "Accept": "application/json" } });
      if (!res.ok) throw new Error(String(res.status));
      data = await res.json();
    } catch (e) {
      body.textContent = "";
      body.appendChild(el("p", "muted",
        "Couldn't load the chart just now — your records are fine, this " +
        "view isn't. Ask again in a moment."));
      return;
    }

    body.textContent = "";
    const series = (data && data.series) || [];
    if (!series.length) {
      // Never "you have no results": this reads the CONNECTED record.
      body.appendChild(el("p", "muted",
        "No readings for that test in the records connected here. That's " +
        "not the same as never having had one."));
      return;
    }
    series.forEach((s) => {
      const panel = el("div", "timeline-series");
      const count = s.readings.length;
      panel.appendChild(el("div", "timeline-name",
        s.name + " · " + count + " reading" + (count === 1 ? "" : "s") +
        (s.unit ? " · " + s.unit : "")));
      panel.appendChild(drawSeries(s));
      if (!s.trend_plottable) {
        panel.appendChild(el("p", "muted",
          "Only one reading on file — not enough to show a trend."));
      }
      body.appendChild(panel);
    });
    if (data.disclaimer) {
      body.appendChild(el("p", "muted small", data.disclaimer));
    }
    scroll();
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
        else if (ev.type === "card" && ev.kind === "lab-timeline")
          addLabTimelineCard(ev.topic || "");
        else if (ev.type === "text") {
          typing.remove(); addAgentText(ev.text);
        } else if (ev.type === "error") {
          // Terminal, exactly like `done`. Every producer of an error frame
          // ends the run on it: agent.py returns after each one, and the SSE
          // replay loop returns after the stream-failure frame. Without
          // `done` here the outer loop reconnects — and because that stream
          // now ends CLEANLY it also resets `reconnectFailures`, so a
          // persistent event-poll failure becomes an unbounded ~2.5 req/s
          // retry loop that prints a fresh ⚠️ on every pass, aimed at the
          // engine that also serves clinicians.
          typing.remove(); addAgentText("⚠️ " + ev.text); state.done = true;
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
