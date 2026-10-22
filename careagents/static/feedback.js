/* CareAgents /feedback — four optional questions. JSON, as every other form
   here. On an error the answers stay in the boxes. */
(function () {
  const $ = (id) => document.getElementById(id);
  const form = $("feedback-form");
  const err = $("feedback-err");
  const NAMES = ["stuck", "change", "trust", "real"];
  const CONTEXT = ["screen", "error", "run_id"];
  const MESSAGES = {
    empty: "Please answer at least one question.",
    too_long: "One answer is too long. Please make it shorter.",
    bad_email: "That email doesn't look right. Please check it, or leave " +
      "it empty.",
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
    // The context a "Report a problem" link carried, as the server wrote
    // it onto the form after checking it. Never read from the URL here.
    const context = {};
    CONTEXT.forEach((n) => {
      const v = form.dataset["ctx" + n.replace(/(^|_)(\w)/g,
        (_, _s, c) => c.toUpperCase())];
      if (v) context[n] = v;
    });
    if (Object.keys(context).length) body.context = context;
    // Signed out only: where to answer them, if they want an answer.
    const replyBox = form.elements.reply_email;
    const replyEmail = replyBox ? replyBox.value.trim() : "";
    if (replyEmail) body.reply_email = replyEmail;
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
        // Back where they came from (the server checks `back` again), and
        // whether a reply is coming: signed in, or an address was left.
        const q = new URLSearchParams();
        if (form.dataset.back) q.set("back", form.dataset.back);
        if (replyEmail || $("feedback-reply")) q.set("reply", "1");
        const rest = q.toString();
        location.assign(d.redirect + (rest ? "?" + rest : ""));
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
