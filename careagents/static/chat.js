/* CareAgents chat — SSE over fetch, tool chips, review/PDF cards,
   typewriter render. No frameworks. */
(function () {
  const AGENT = window.CARE_AGENT || "";
  const CONVERSATION = window.CARE_CONVERSATION || "";
  const AGENT_NAME = window.CARE_AGENT_NAME || "Your assistant";
  // "Mom's records" in the chat of someone the holder cares for, empty in
  // the holder's own (FHI-44). Page text only, never sent anywhere.
  const WHOSE = window.CARE_WHOSE || "";
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
      // Both hrefs are fixed strings, never built from the matched text:
      // a replayed bubble's text comes from the page (CodeQL 124).
      a.href = m[0] === "your hub" ? "/home" : "mailto:contactus@healthclaw.io";
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
      // A newline, not a <br>: .msg is white-space: pre-wrap, so it draws
      // the same break, and it stays in textContent, so a screen reader or
      // a copy does not run two paragraphs into one sentence (#878).
      if (next !== undefined && !LIST_ITEM.test(next))
        node.appendChild(document.createTextNode("\n"));
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
  const PACE_TEXT = "You've hit the pace limit for now — give it a few minutes.";
  function limitText(d) {
    return (d && typeof d.message === "string" && d.message) || PACE_TEXT;
  }

  // Under the daily limit's sentence, a way to say the limit got in the
  // way (#864, FHI-68 item 7): /feedback with the screen and the error
  // code. Pointers only, never the message that was refused.
  function addLimitFeedback() {
    const q = new URLSearchParams({ screen: "chat", error: "rate_limited" });
    const p = el("p", "chat-feedback limit-feedback", "Need more? ");
    const a = el("a", null, "Tell us");
    a.href = "/feedback?" + q.toString();
    p.appendChild(a);
    p.appendChild(document.createTextNode("."));
    log.appendChild(p);
  }

  // The sample's made-up-records line is the sticky banner above the log,
  // so a bubble does not say it again. The worker still opens every stored
  // and texted answer with it; only that exact leading copy is dropped,
  // only while the banner is on this page, and never down to nothing
  // (careagents/beta.py under_banner does the same for the history).
  // A crisis answer carries it at its end, so it never comes before the
  // 911 guidance (worker._finish); that copy, or one between paragraphs,
  // a paragraph of its own, is dropped the same way.
  function withoutFrame(text) {
    const banner = document.querySelector(".sample-banner");
    const frame = banner ? banner.textContent.trim() : "";
    if (!frame) return text;
    let rest = "";
    const inline = "\n\n" + frame + "\n\n";
    if (text.indexOf(frame + "\n\n") === 0) {
      rest = text.slice(frame.length + 2);
    } else if (text.indexOf(inline) > 0) {
      rest = text.replace(inline, "\n\n");
    } else if (text.length > frame.length + 2
               && text.slice(-(frame.length + 2)) === "\n\n" + frame) {
      rest = text.slice(0, -(frame.length + 2));
    }
    return rest.trim() ? rest : text;
  }

  // `runId` alone marks a model's answer, which then gets "This wasn't
  // right" under it once it has finished typing. With `failed` (an error
  // code) the bubble is a failure, and gets "Report a problem" instead.
  function addAgentText(text, runId, failed) {
    text = withoutFrame(text);
    const m = el("div", "msg agent");
    log.appendChild(m);
    typewrite(m, withoutMarkers(text), function () {
      renderMarkdown(m, text);
      if (failed) addReport(m, runId, failed);
      else if (runId) addFlag(m, runId);
      scroll();
    });
  }

  // "Report a problem" beside a failed turn: /feedback with the screen,
  // the error code and the run id. Pointers only; the server checks each
  // against its fixed lists (careagents/feedback.py read_context).
  function addReport(bubble, runId, code) {
    const q = new URLSearchParams({ screen: "chat", error: code });
    if (runId) q.set("run_id", runId);
    const a = el("a", "report-link", "Report a problem");
    a.href = "/feedback?" + q.toString();
    bubble.insertAdjacentElement("afterend", a);
  }

  // --- "This wasn't right" (careagents/feedback.py feedback_flag) --------
  // What a flag sends is built from three values and nothing else: the
  // answer's run id (a pointer), a reason code and the person's own short
  // note. Never the answer or the question: flagBody is never handed the
  // bubble, and the server refuses any other field.
  // The same codes and words as careagents/funnel.py FLAG_LABELS.
  const FLAG_REASONS = [["wrong", "Wrong"], ["confusing", "Confusing"],
                        ["not_what_asked", "Didn't do what I asked"],
                        ["unsafe", "Felt unsafe or worrying"],
                        ["other", "Something else"]];
  const NOTE_MAX = 280;
  function flagBody(runId, reason, note) {
    return JSON.stringify({ run_id: runId, reason: reason,
                            note: String(note || "").slice(0, NOTE_MAX) });
  }
  const FLAG_ERRORS = {
    rate_limited: "You've flagged a lot this hour. Please try again later.",
    not_sent: "We couldn't send that just now. Please try again.",
    too_long: "That note is too long. Please make it shorter.",
  };

  // A quiet link under the bubble, beside it rather than inside it, so the
  // answer's own text is untouched. One tap opens a small sheet in place.
  function addFlag(bubble, runId) {
    const wrap = el("div", "flag");
    const open = el("button", "flag-open", "This wasn't right");
    open.type = "button";
    wrap.appendChild(open);
    bubble.insertAdjacentElement("afterend", wrap);
    open.addEventListener("click", function () {
      open.hidden = true;
      const sheet = el("div", "flag-sheet");
      sheet.appendChild(el("p", "flag-ask", "What was wrong with this answer?"));
      const reasons = el("div", "flag-reasons");
      reasons.setAttribute("role", "group");
      reasons.setAttribute("aria-label", "Reason");
      const send = el("button", "btn-primary flag-send", "Send");
      send.type = "button"; send.disabled = true;
      let reason = null;
      FLAG_REASONS.forEach(function (pair) {
        const b = el("button", "flag-reason", pair[1]);
        b.type = "button";
        b.setAttribute("aria-pressed", "false");
        b.addEventListener("click", function () {
          reason = pair[0];
          reasons.querySelectorAll(".flag-reason").forEach(function (x) {
            x.setAttribute("aria-pressed", String(x === b));
          });
          send.disabled = false;
        });
        reasons.appendChild(b);
      });
      sheet.appendChild(reasons);
      const note = el("textarea", "field flag-note");
      note.rows = 2; note.maxLength = NOTE_MAX;
      note.setAttribute("aria-label", "Optional note");
      note.placeholder = "Optional. Please don't include health details.";
      sheet.appendChild(note);
      const row = el("div", "flag-actions");
      const cancel = el("button", "btn-secondary flag-cancel", "Cancel");
      cancel.type = "button";
      row.appendChild(send); row.appendChild(cancel);
      sheet.appendChild(row);
      const status = el("p", "flag-status");
      status.setAttribute("role", "status");
      sheet.appendChild(status);
      wrap.appendChild(sheet);
      // Send in view at 375px (QA on #980): the row is sticky to the
      // bottom of the log (careagents.css), and is brought into view on
      // open and again when the keyboard comes up for the note.
      const showSend = () => row.scrollIntoView({ block: "nearest" });
      showSend();
      note.addEventListener("focus", () => setTimeout(showSend, 300));
      cancel.addEventListener("click", function () {
        sheet.remove(); open.hidden = false;
      });
      send.addEventListener("click", async function () {
        if (!reason) return;
        send.disabled = true; status.textContent = "";
        try {
          const r = await fetch("/feedback/flag", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: flagBody(runId, reason, note.value),
          });
          const d = await r.json().catch(() => ({}));
          if (r.ok && d.ok) {
            wrap.textContent = "";
            wrap.appendChild(el("p", "flag-done",
              "Thanks. We'll look at this answer."));
            return;
          }
          status.textContent = FLAG_ERRORS[d.error] ||
            "Something went wrong. Please try again.";
        } catch (e) {
          status.textContent = "Couldn't reach us. Please try again.";
        }
        send.disabled = false;
      });
    });
  }

  // A chat reopened from history arrives as the server's plain text
  // (chat.html), so its markdown showed as literal asterisks (#942). The
  // same renderer lays it out and adds back the same two links. A bubble
  // with nothing to lay out is left as written: the greetings carry their
  // own <strong>, which re-rendering from text would drop.
  function renderReplayed(bubble) {
    const text = bubble.textContent;
    BOLD_OR_ITALIC.lastIndex = 0;
    if (!BOLD_OR_ITALIC.test(text) &&
        !text.split("\n").some((line) => LIST_ITEM.test(line))) return;
    renderMarkdown(bubble, text);
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
      c.appendChild(el("h4", null, WHOSE ? "Review & approve the intake form"
                                         : "Review & approve your intake form"));
      c.appendChild(el("p", null,
        AGENT_NAME + " filled it in from " + (WHOSE || "your records") +
        ". Check each medication and allergy. Nothing is made until you " +
        "approve."));
    }
    const a = el("a", "btn-primary", "Open the review");
    a.href = "/review/" + AGENT + "/" + actionId;
    // Same tab on a phone: a new tab in an in-app browser can lose the
    // sign-in (#876). A wide screen keeps the chat open beside it.
    if (window.matchMedia && window.matchMedia("(min-width: 700px)").matches) {
      a.target = "_blank"; a.rel = "noopener";
    }
    c.appendChild(a);
    log.appendChild(c); scroll();
    // Only the intake form ends in a PDF; polling for one on any other
    // request would run until the page closed.
    if (!label) watchForm(actionId);
  }

  function addPdfCard(url) {
    const c = el("div", "card pdf");
    c.appendChild(el("h4", null, WHOSE ? "The intake form is ready"
                                       : "Your intake form is ready"));
    c.appendChild(el("p", null,
      "You checked it, and the PDF says so. The link works for one day."));
    const a = el("a", "btn-primary", "Open the PDF");
    a.href = url; a.target = "_blank"; a.rel = "noopener";
    c.appendChild(a);
    log.appendChild(c); scroll();
  }

  // A form approved before this page loaded (data-done-form). Its review
  // page shows it finished with the PDF, so no bearer link is put here.
  function addFormDoneCard(actionId) {
    const c = el("div", "card pdf");
    c.appendChild(el("h4", null, WHOSE ? "The intake form is ready"
                                       : "Your intake form is ready"));
    c.appendChild(el("p", null, "You checked and approved it."));
    const a = el("a", "btn-primary", "Open the form");
    a.href = "/review/" + AGENT + "/" + encodeURIComponent(actionId);
    c.appendChild(a);
    log.appendChild(c); scroll();
  }

  // The answer summarizes the brief and points at the page; this is the
  // way there (patient walk 2026-10-18).
  function addBriefCard() {
    const c = el("div", "card");
    c.appendChild(el("h4", null, "Visit brief"));
    c.appendChild(el("p", null, "The full brief, to read or show your doctor."));
    const a = el("a", "btn-primary", "Open the visit brief");
    a.href = "/brief?agent=" + AGENT;
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

  // The three y-axis labels, top to bottom, with the fewest decimals (up to
  // two) that keep them apart. Whole numbers read an A1c chart as
  // "9 / 9 / 8" (#942).
  function axisLabels(lo, hi) {
    const values = [hi, (hi + lo) / 2, lo];
    let labels = [];
    for (let digits = 0; digits <= 2; digits++) {
      labels = values.map((v) => v.toFixed(digits));
      if (new Set(labels).size === labels.length) break;
    }
    return labels;
  }

  // "Apr 21, 2025" for a real YYYY-MM-DD, anything else as it came. A fixed
  // month list, so the words do not change with the browser's locale.
  const SHORT_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul",
                        "Aug", "Sep", "Oct", "Nov", "Dec"];
  function shortDate(iso) {
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso || "");
    if (!m || +m[2] < 1 || +m[2] > 12 || +m[3] < 1 || +m[3] > 31) return iso;
    return SHORT_MONTHS[+m[2] - 1] + " " + (+m[3]) + ", " + m[1];
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

    const yLabels = axisLabels(lo, hi);
    [0, 0.5, 1].forEach((frac, i) => {
      const gy = padT + frac * (H - padT - padB);
      svg.appendChild(svgEl("line", {
        class: "spark-grid", x1: padL, x2: W - padR, y1: gy, y2: gy }));
      const label = svgEl("text", { class: "spark-axis", x: 2, y: gy + 3 });
      label.textContent = yLabels[i];
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
      title.textContent = shortDate(p.date) + ": " + p.value + " " + (p.unit || "") +
        " (" + (p.flag || "IND") + ")";
      dot.appendChild(title);
      svg.appendChild(dot);
    });

    [[t0, "start"], [t1, "end"]].forEach((pair) => {
      const label = svgEl("text", {
        class: "spark-axis", x: x(pair[0]), y: H - 5,
        "text-anchor": pair[1] === "end" ? "end" : "start" });
      label.textContent = shortDate(new Date(pair[0]).toISOString().slice(0, 10));
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
          addReviewCard(ev.action_id, ev.review_url, ev.label);
        else if (ev.type === "card" && ev.kind === "pdf")
          addPdfCard(ev.url);
        // Drawn once the answer is in (on done), beside its "the full
        // brief is on the Visit brief page", not above the answer.
        else if (ev.type === "card" && ev.kind === "brief")
          state.brief = true;
        else if (ev.type === "card" && ev.kind === "lab-timeline")
          addLabTimelineCard(ev.topic || "");
        else if (ev.type === "text") {
          typing.remove(); addAgentText(ev.text, state.runId);
        } else if (ev.type === "error") {
          // Terminal, exactly like `done`. Every producer of an error frame
          // ends the run on it: agent.py returns after each one, and the SSE
          // replay loop returns after the stream-failure frame. Without
          // `done` here the outer loop reconnects — and because that stream
          // now ends CLEANLY it also resets `reconnectFailures`, so a
          // persistent event-poll failure becomes an unbounded ~2.5 req/s
          // retry loop that prints a fresh error on every pass, aimed at the
          // engine that also serves clinicians.
          typing.remove(); addAgentText(ev.text, state.runId, "chat_error");
          state.done = true;
          state.failed = true;
        } else if (ev.type === "done") {
          state.done = true;
          // An answer came: the next step, if the page offers one.
          if (ev.status === "completed") {
            const next = document.getElementById("connect-next");
            if (next) next.hidden = false;
          }
          if (state.brief) addBriefCard();
        }
      }
    }
  }

  // The greeting's "Yes" and the ideas under it. Hidden while a turn runs,
  // and back under an error, so trying again is one tap.
  const suggestions = [document.getElementById("quick"), starters]
    .filter(Boolean);
  function hideSuggestions() {
    suggestions.forEach((n) => { n.hidden = true; });
  }
  function showSuggestions() {
    // The greeting's answer goes back to the end of the log; the starters
    // live above the message box and stay there.
    suggestions.forEach((n) => {
      n.hidden = false;
      if (n !== starters) log.appendChild(n);
    });
    scroll();
  }

  async function send(text) {
    if (busy || !text.trim()) return;
    busy = true; sendBtn.disabled = true;
    hideSuggestions();
    let failed = false;
    addUser(text);
    box.value = "";
    const typing = addTyping();
    const requestId = (window.crypto && window.crypto.randomUUID)
      ? window.crypto.randomUUID()
      : Date.now().toString(36) + "-" + Math.random().toString(36).slice(2);
    // The last sentence a 503 refusing this turn carried (it ends with the
    // 911 line): shown instead of "Connection hiccup" once retries run out.
    let refusalText = "";

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
            const limit = await resp.json().catch(() => ({}));
            addAgentText(limitText(limit));
            if (limit && limit.error === "daily_limit_reached")
              addLimitFeedback();
            return;
          }
          // A chat turn the server could not take carries its sentence and
          // a 911 line (app.py, _with_emergency). Crisis wording is said at
          // once. Any other is retried silently, as a blip usually passes,
          // and said instead of "Connection hiccup" if the retries run out.
          // Only for the POST that asked: a reconnect to a run in progress
          // keeps its own retries.
          if (resp.status === 503 && !state.runId) {
            const refusal = await resp.clone().json().catch(() => ({}));
            if (refusal && typeof refusal.message === "string"
                && refusal.message) {
              if (refusal.emergency === true) {
                typing.remove();
                addAgentText(refusal.message);
                return;
              }
              refusalText = refusal.message;
            }
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
      failed = state.failed === true;
      if (failed) showSuggestions();
    } catch (e) {
      if (typing.parentNode) typing.remove();
      if (refusalText) addAgentText(refusalText, null, "connection");
      // No server sentence reached the page, so it adds the 911 line itself
      // (beta.EMERGENCY_TRAILER): this is all a hard outage shows.
      else addAgentText("Connection hiccup — try that again.\n\nIf this is an emergency, call 911. Si es una emergencia, llame al 911.", null, "connection");
      showSuggestions();
    } finally {
      busy = false; sendBtn.disabled = false; box.focus();
    }
  }

  // The answers replayed from history, laid out as they were live (#942).
  log.querySelectorAll(".msg.agent").forEach(renderReplayed);

  // Requests still waiting when the page loaded, read from the engine by
  // the server: history keeps only text, so without this a reload lost the
  // card the reply above promised (#847).
  let pendingReviews = [];
  try { pendingReviews = JSON.parse(log.dataset.pendingReviews || "[]"); }
  catch (e) { pendingReviews = []; }
  pendingReviews.forEach((r) => addReviewCard(r.id, null, r.form ? null : r.label));
  if (log.dataset.doneForm) addFormDoneCard(log.dataset.doneForm);

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
  document.querySelectorAll(".quick-reply").forEach((b) =>
    b.addEventListener("click", () => send(b.dataset.ask || b.textContent)));

  // The ⋯ menu closes on a tap anywhere else, as a phone menu does.
  const menu = document.querySelector(".chat-menu");
  if (menu) document.addEventListener("click", (e) => {
    if (menu.open && !menu.contains(e.target)) menu.open = false;
  });

  box.focus();
})();
