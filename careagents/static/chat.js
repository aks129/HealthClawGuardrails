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

  function typewrite(node, text, done) {
    let i = 0;
    const step = Math.max(1, Math.round(text.length / 120));
    (function tick() {
      i = Math.min(text.length, i + step);
      node.textContent = text.slice(0, i);
      scroll();
      if (i < text.length) requestAnimationFrame(tick);
      else if (done) done();
    })();
  }

  // Model markdown, minimal subset: **bold**, *italic*, "- " / "* " / "1. "
  // list lines, line breaks. Built from text nodes and fixed elements only,
  // so nothing the model writes is ever parsed as HTML.
  const BOLD_OR_ITALIC = /\*\*([^*\n]+)\*\*|\*(\S[^*\n]*?)\*/g;
  const LIST_ITEM = /^\s*(?:[-*]|(\d+)\.)\s+(.*)$/;

  // Two fixed links, never anything the model chooses: the contact address
  // and "your hub". The href is ours; the text node is the matched words.
  const LINKS = /contactus@healthclaw\.io|your hub/g;
  function appendLinked(parent, text) {
    let last = 0, m;
    LINKS.lastIndex = 0;
    while ((m = LINKS.exec(text))) {
      if (m.index > last)
        parent.appendChild(document.createTextNode(text.slice(last, m.index)));
      const a = el("a", null, m[0]);
      a.href = m[0] === "your hub" ? "/home" : "mailto:" + m[0];
      parent.appendChild(a);
      last = LINKS.lastIndex;
    }
    if (last < text.length)
      parent.appendChild(document.createTextNode(text.slice(last)));
  }

  function appendInline(parent, line) {
    let last = 0, m;
    BOLD_OR_ITALIC.lastIndex = 0;
    while ((m = BOLD_OR_ITALIC.exec(line))) {
      if (m.index > last) appendLinked(parent, line.slice(last, m.index));
      parent.appendChild(m[1] !== undefined
        ? el("strong", null, m[1]) : el("em", null, m[2]));
      last = BOLD_OR_ITALIC.lastIndex;
    }
    if (last < line.length) appendLinked(parent, line.slice(last));
  }

  function renderMarkdown(node, text) {
    node.textContent = "";
    const lines = text.split("\n");
    let list = null;
    lines.forEach(function (line, i) {
      const item = LIST_ITEM.exec(line);
      if (item) {
        const kind = item[1] ? "ol" : "ul";
        if (!list || list.tagName.toLowerCase() !== kind) {
          list = el(kind);
          node.appendChild(list);
        }
        const li = el("li");
        appendInline(li, item[2]);
        list.appendChild(li);
        return;
      }
      list = null;
      appendInline(node, line);
      const next = lines[i + 1];
      if (next !== undefined && !LIST_ITEM.test(next))
        node.appendChild(el("br"));
    });
  }

  // What the typewriter shows before the markdown is laid out: the same
  // text without the emphasis markers, so they never flash on screen.
  function withoutMarkers(text) {
    return text.replace(/\*\*([^*\n]+)\*\*/g, "$1")
      .replace(/\*(\S[^*\n]*?)\*/g, "$1");
  }

  // A 429 is two different limits (#862). The burst limiter sends only an
  // error code, and the pace sentence is true for it. The daily limit sends
  // its own sentence, and "a few minutes" would be false there.
  const PACE_TEXT = "You’ve hit the pace limit for now — give it a few minutes.";
  function limitText(d) {
    return (d && typeof d.message === "string" && d.message) || PACE_TEXT;
  }

  function addAgentText(text) {
    const m = el("div", "msg agent");
    log.appendChild(m);
    typewrite(m, withoutMarkers(text), function () {
      renderMarkdown(m, text);
      scroll();
    });
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

  // One card per request: the page draws the ones still waiting on load,
  // and a turn may announce the same request again.
  const reviewCards = new Set();

  // `label` is set only for a request that is not the intake form, which
  // is the one a chat turn proposes; requests from elsewhere are named by
  // their kind.
  function addReviewCard(actionId, url, label) {
    if (reviewCards.has(actionId)) return;
    reviewCards.add(actionId);
    const c = el("div", "card");
    if (label) {
      c.appendChild(el("h4", null, "Review & approve: " + label));
      c.appendChild(el("p", null,
        "This waits for your answer. Nothing happens until you approve it."));
    } else {
      c.appendChild(el("h4", null, "Review & approve your intake form"));
      c.appendChild(el("p", null,
        "Your agent filled it from the records — now every medication and " +
        "allergy waits for your say-so. Nothing is generated until you approve."));
    }
    const a = el("a", "btn-primary", "Open the review");
    a.href = "/review/" + AGENT + "/" + actionId;
    a.target = "_blank"; a.rel = "noopener";
    c.appendChild(a);
    log.appendChild(c); scroll();
    // Only the intake form ends in a PDF; polling for one on any other
    // request would run until the page closed.
    if (!label) watchForm(actionId);
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
      return c;
    }

    body.textContent = "";
    const series = (data && data.series) || [];
    if (!series.length) {
      // Never "you have no results": this reads the CONNECTED record.
      body.appendChild(el("p", "muted",
        "No readings for that test in the records connected here. That's " +
        "not the same as never having had one."));
      return c;
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
    return c;
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
            addAgentText(limitText(await resp.json().catch(() => ({}))));
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

  // Requests still waiting when the page loaded, read from the engine by
  // the server: history keeps only text, so without this a reload lost the
  // card the reply above promised (#847).
  let pendingReviews = [];
  try { pendingReviews = JSON.parse(log.dataset.pendingReviews || "[]"); }
  catch (e) { pendingReviews = []; }
  pendingReviews.forEach((r) => addReviewCard(r.id, null, r.form ? null : r.label));

  // A texted answer cannot carry a chart, so it links here with ?chart=
  // (careagents/imessage.py run_reply). The topic only narrows the series;
  // the readings still come from /api/labs/timeline under this session.
  // The server renders the history hidden (data-chart-pending) so it does
  // not paint at the top first; it is shown once the chart has drawn, or
  // failed to, with the chart in view.
  const chartTopic = new URLSearchParams(window.location.search).get("chart");
  if (chartTopic !== null) {
    const chartPending = log.hasAttribute("data-chart-pending");
    // The log scrolls smoothly (careagents.css). On this first render that
    // animated from the top of the history to the chart, so jump instead,
    // then give new messages their smooth scroll back.
    log.style.scrollBehavior = "auto";
    const reveal = (card) => {
      if (chartPending) {
        log.style.visibility = "";
        log.removeAttribute("data-chart-pending");
      }
      if (card) card.scrollIntoView({ block: "start", behavior: "auto" });
      requestAnimationFrame(() => { log.style.scrollBehavior = ""; });
    };
    addLabTimelineCard(chartTopic.slice(0, 64)).then(reveal, () => reveal(null));
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
