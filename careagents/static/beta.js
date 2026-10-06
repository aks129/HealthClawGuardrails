/* CareAgents /beta — the request form. JSON, as every other form here. */
(function () {
  const $ = (id) => document.getElementById(id);
  const form = $("beta-form");
  const err = $("beta-err");
  const consent = $("beta-consent");
  const MESSAGES = {
    consent_required: "Please tick the box so we can contact you.",
    first_name: "Please enter your first name.",
    email: "Please check your email address.",
    mobile: "Please check the mobile number, or leave it empty.",
    rate_limited: "Too many tries. Please wait a few minutes.",
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
    if (!consent.checked) return fail(MESSAGES.consent_required);
    const field = (name) => form.elements[name].value;
    const button = form.querySelector("button[type=submit]");
    button.disabled = true;
    try {
      const r = await fetch("/beta", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          first_name: field("first_name"), email: field("email"),
          mobile: field("mobile"), ref: field("ref"),
          website: field("website"), consent: consent.checked,
        }),
      });
      const d = await r.json().catch(() => ({}));
      if (r.ok) {
        // textContent: the name and address are shown back, never parsed.
        const done = $("beta-done");
        done.textContent = "Thanks, " + field("first_name").trim() +
          ". Check your email to confirm. We sent an email to " +
          field("email").trim() +
          ". If it isn't there in a few minutes, check spam.";
        form.hidden = true;
        done.hidden = false;
        done.scrollIntoView({ block: "center", behavior: "smooth" });
        return;
      }
      fail(MESSAGES[d.error] || "Something went wrong. Please try again.");
    } catch (e) {
      fail("Couldn't reach us. Please try again.");
    } finally {
      button.disabled = false;
    }
  });
})();
