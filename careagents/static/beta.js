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
  const FAILED = "Something went wrong. Please try again.";
  const OFFLINE = "Couldn't reach us. Please try again.";
  // The other way in, said wherever an email is late or not coming.
  const OTHER_WAY = " Or tap Get started below to sign in with a code " +
    "we email you instead.";
  // Shown next to Send, and scrolled to, so it is never off-screen.
  function fail(msg) {
    err.textContent = msg;
    err.hidden = false;
    err.scrollIntoView({ block: "center", behavior: "smooth" });
  }
  // "in 45 seconds", "in 12 minutes", "in about an hour", "in about 5
  // hours": never a raw count like 3540 seconds.
  function wait(seconds) {
    if (seconds <= 90) return "in " + seconds + " seconds";
    if (seconds < 50 * 60) return "in " + Math.ceil(seconds / 60) + " minutes";
    if (seconds <= 90 * 60) return "in about an hour";
    return "in about " + Math.ceil(seconds / 3600) + " hours";
  }

  // What was sent last, so "Send it again" asks for the same thing, and
  // the line now shown, so a failed re-send adds to it, never replaces it.
  let last = null;
  let shown = "";
  let timer = null;
  // Offer "Send it again" once the wait is over, never before.
  function againAfter(seconds) {
    clearTimeout(timer);
    again.hidden = true;
    if (seconds > 3600) return;
    timer = setTimeout(() => { again.hidden = false; }, seconds * 1000);
  }
  // Green only for a send that happened; anything else is a warning.
  function say(line, good, extra) {
    if (!extra) shown = line;
    doneText.textContent = extra ? shown + " " + extra : line;
    done.classList.toggle("form-ok", good);
    done.classList.toggle("form-warn", !good);
  }

  // Four answers, and only `sent === true` earns "We sent". Compared by
  // identity, as auth.js does: `false` (nothing sent just now) and `null`
  // (we never saw it leave) are different facts.
  function show(d, status) {
    // textContent: the name and address are shown back, never parsed.
    const name = last.first_name.trim();
    const email = last.email.trim();
    if (status === 502) {
      say("Sorry, " + name + ". We couldn't send the email to " + email +
        " just now. Please try again in a minute." + OTHER_WAY, false);
      againAfter(0);
    } else if (d.sent === true) {
      say("Thanks, " + name + ". We sent an email to " + email +
        ". Tap Confirm in it. If it isn't there in a few minutes, check " +
        "spam." + OTHER_WAY, true);
      againAfter(60);
    } else if (d.sent === false && d.retry_after > 60) {
      say("We already sent several emails to " + email + " today. " +
        "Use the newest one we sent, or ask for another " +
        wait(d.retry_after) + "." + OTHER_WAY, false);
      againAfter(d.retry_after);
    } else if (d.sent === false) {
      say("We sent an email to " + email + " a moment ago, so we didn't " +
        "send another yet. Check your inbox and spam folder. You can ask " +
        "for a new one " + wait(d.retry_after) + "." + OTHER_WAY, false);
      againAfter(d.retry_after);
    } else {
      say("We couldn't confirm the email reached " + email + ". Check " +
        "your inbox in a few minutes. If nothing arrives, you can ask " +
        "again " + wait(d.retry_after) + "." + OTHER_WAY, false);
      againAfter(d.retry_after);
    }
    form.hidden = true;
    done.hidden = false;
    done.scrollIntoView({ block: "center", behavior: "smooth" });
  }

  // A refusal on the form goes next to Send. On a re-send it is added to
  // what the panel said, in the warning style, and the button comes back.
  function refused(msg) {
    if (!form.hidden) return fail(msg);
    say(shown, false, msg);
    again.hidden = false;
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
      refused(MESSAGES[d.error] || FAILED);
    } catch (e) {
      refused(OFFLINE);
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
