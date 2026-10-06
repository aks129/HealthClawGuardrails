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
  function fail(msg) { err.textContent = msg; err.hidden = false; }

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
        form.hidden = true;
        $("beta-done").textContent = "Thanks. Check your email.";
        $("beta-done").hidden = false;
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
