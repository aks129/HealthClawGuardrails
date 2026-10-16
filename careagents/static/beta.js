/* CareAgents /beta — the request form. JSON, as every other form here. */
(function () {
  const $ = (id) => document.getElementById(id);
  const form = $("beta-form");
  const err = $("beta-err");
  const consent = $("beta-consent");
  const done = $("beta-done");
  const doneText = $("beta-done-text");
  const again = $("beta-again");
  const MESSAGES = {
    consent_required: "Please tick the box so we can contact you.",
    first_name: "Please enter your first name.",
    email: "Please check your email address.",
    mobile: "Please check the mobile number, or leave it empty.",
    rate_limited: "Too many tries. Please wait a few minutes.",
  };
  // The other way in, said wherever an email is late or not coming.
  const OTHER_WAY = " Or tap Get started below to sign in with a code " +
    "we email you instead.";
  // Shown next to Send, and scrolled to, so it is never off-screen.
  function fail(msg) {
    err.textContent = msg;
    err.hidden = false;
    err.scrollIntoView({ block: "center", behavior: "smooth" });
  }
  function wait(seconds) {
    if (seconds > 3600) {
      const hours = Math.ceil(seconds / 3600);
      return "in about " + hours + (hours === 1 ? " hour" : " hours");
    }
    return "in " + seconds + " seconds";
  }

  // What was sent last, so "Send it again" asks for the same thing.
  let last = null;
  let timer = null;
  // Offer "Send it again" once the wait is over, never before.
  function againAfter(seconds) {
    clearTimeout(timer);
    again.hidden = true;
    if (seconds > 3600) return;
    timer = setTimeout(() => { again.hidden = false; }, seconds * 1000);
  }

  // Four answers, and only `sent === true` earns "We sent". Compared by
  // identity, as auth.js does: `false` (nothing sent just now) and `null`
  // (we never saw it leave) are different facts.
  function show(d, status) {
    // textContent: the name and address are shown back, never parsed.
    const name = last.first_name.trim();
    const email = last.email.trim();
    let line;
    if (status === 502) {
      line = "Sorry, " + name + ". We couldn't send the email to " + email +
        " just now. Please try again in a minute." + OTHER_WAY;
      againAfter(0);
    } else if (d.sent === true) {
      line = "Thanks, " + name + ". We sent an email to " + email +
        ". Tap Confirm in it. If it isn't there in a few minutes, check " +
        "spam." + OTHER_WAY;
      againAfter(60);
    } else if (d.sent === false && d.retry_after > 60) {
      line = "We already sent several emails to " + email + " today, so " +
        "we can't send another until " + wait(d.retry_after).slice(3) +
        " from now. Use the newest one we sent." + OTHER_WAY;
      againAfter(d.retry_after);
    } else if (d.sent === false) {
      line = "We sent an email to " + email + " a moment ago, so we didn't " +
        "send another yet. Check your inbox and spam folder. You can ask " +
        "for a new one " + wait(d.retry_after) + "." + OTHER_WAY;
      againAfter(d.retry_after);
    } else {
      line = "We couldn't confirm the email reached " + email + ". Check " +
        "your inbox in a few minutes. If nothing arrives, you can ask " +
        "again " + wait(d.retry_after) + "." + OTHER_WAY;
      againAfter(d.retry_after);
    }
    doneText.textContent = line;
    // Green only for a send that happened; a failure is not good news.
    done.classList.toggle("form-ok", d.sent === true);
    done.classList.toggle("form-warn", d.sent !== true);
    form.hidden = true;
    done.hidden = false;
    done.scrollIntoView({ block: "center", behavior: "smooth" });
  }

  async function send(body, button) {
    button.disabled = true;
    try {
      const r = await fetch("/beta", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const d = await r.json().catch(() => ({}));
      if (r.ok || r.status === 502) return show(d, r.status);
      if (!form.hidden) {
        return fail(MESSAGES[d.error] || "Something went wrong. Please try again.");
      }
      doneText.textContent = MESSAGES[d.error] ||
        "Something went wrong. Please try again.";
    } catch (e) {
      if (!form.hidden) return fail("Couldn't reach us. Please try again.");
      doneText.textContent = "Couldn't reach us. Please try again.";
    } finally {
      button.disabled = false;
    }
  }

  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    err.hidden = true;
    if (!consent.checked) return fail(MESSAGES.consent_required);
    const field = (name) => form.elements[name].value;
    last = {
      first_name: field("first_name"), email: field("email"),
      mobile: field("mobile"), ref: field("ref"),
      invite: field("invite"),
      website: field("website"), consent: consent.checked,
    };
    await send(last, form.querySelector("button[type=submit]"));
  });

  again.addEventListener("click", () => { if (last) send(last, again); });
})();
