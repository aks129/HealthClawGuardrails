/* CareAgents /feedback — four optional questions. JSON, as every other form
   here. On an error the answers stay in the boxes. */
(function () {
  const $ = (id) => document.getElementById(id);
  const form = $("feedback-form");
  const err = $("feedback-err");
  const NAMES = ["stuck", "change", "trust", "real"];
  const MESSAGES = {
    empty: "Please answer at least one question.",
    too_long: "One answer is too long. Please make it shorter.",
    rate_limited: "You've sent a lot this hour. Please wait and try again.",
    not_sent: "We couldn't send that just now. Your answers are still " +
      "here. Please try again in a few minutes.",
  };
  // Shown next to Send, and scrolled to, so it is never off-screen.
  function fail(msg) {
    err.textContent = msg;
    err.hidden = false;
    err.scrollIntoView({ block: "center", behavior: "smooth" });
  }

  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    err.hidden = true;
    const body = {};
    NAMES.forEach((n) => { body[n] = form.elements[n].value; });
    if (!NAMES.some((n) => body[n].trim())) return fail(MESSAGES.empty);
    const button = form.querySelector("button[type=submit]");
    button.disabled = true;
    try {
      const r = await fetch("/feedback", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const d = await r.json().catch(() => ({}));
      if (r.ok && d.redirect === "/feedback/thanks") {
        location.assign(d.redirect);
        return;
      }
      fail(MESSAGES[d.error] || "Something went wrong. Please try again.");
    } catch (e) {
      fail("Couldn't reach us. Check your connection and try again.");
    } finally {
      button.disabled = false;
    }
  });
})();
