/* SMART Health Check-in web wallet page (careagents/checkin_wallet.py).

   Order is the point. serveWebWallet posts `ready` to the opener the moment
   it starts, and the check-in page sends its request on `ready`. So sign-in
   finishes here first, in this tab, without navigating (a redirect would
   lose the request); only then does the library start listening.

   Nothing about the request or the answer goes to the CareAgents server.
   The request arrives by postMessage, the answer is built and sealed here
   and leaves by postMessage. The server is asked only for the kinds of
   record the request names (POST /checkin/wallet/records, a list of type
   names and nothing else: not the request, not the website's origin), and
   answers with the account's own records from the engine's checkin export.
   The other calls are the existing sign-in endpoints. */
import {
  checkWalletResponse, declineAll, serveWebWallet,
} from "./vendor/smart-health-checkin-client/0.4.4/wallet.js";
// Loaded with this file's own ?v= build stamp, so a cached copy from an
// earlier deploy never runs against this page (#909). A static import
// cannot carry it.
const {
  buildResponse, itemHeading, matchItems, prepareRecords, recordNames,
  requestedTypes, websiteHeading,
} = await import(`./checkin_answer.js${new URL(import.meta.url).search}`);

const $ = (id) => document.getElementById(id);
const card = $("wallet");
// wallet-test-patient is on the page only when its flag is on.
const SECTIONS = ["wallet-signin", "wallet-waiting", "wallet-direct",
  "wallet-test-patient", "wallet-review", "wallet-done"].filter((s) => $(s));
const show = (id) => SECTIONS.forEach((s) => ($(s).hidden = s !== id));
const err = $("wallet-err");
// How long "Waiting for the request" waits before it offers a next step.
// A test can shorten it with ?wait=<ms>; nothing else reads the query.
const WAIT_MS = Number(new URLSearchParams(location.search).get("wait")) || 20000;
const fail = (msg) => { err.textContent = msg; err.hidden = false; };
const clear = () => { err.hidden = true; $("wallet-review-err").hidden = true; };

/* A problem with the review, said beside the Send button the person just
   tapped, scrolled into view and focused (#1030 QA: at phone width the
   top of the card is far off screen). */
function reviewFail(msg) {
  const box = $("wallet-review-err");
  box.textContent = msg;
  box.hidden = false;
  box.scrollIntoView({ block: "center" });
  box.focus({ preventScroll: true });
}

// --- sign-in, in place ----------------------------------------------------

const b64uToBuf = (s) => {
  s = s.replace(/-/g, "+").replace(/_/g, "/");
  const pad = s.length % 4 ? "=".repeat(4 - (s.length % 4)) : "";
  return Uint8Array.from(atob(s + pad), (c) => c.charCodeAt(0)).buffer;
};
const bufToB64u = (buf) => btoa(String.fromCharCode(...new Uint8Array(buf)))
  .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");

async function post(url, body) {
  const r = await fetch(url, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body === undefined ? {} : body),
  });
  const d = await r.json().catch(() => ({}));
  return { ok: r.ok, d };
}

$("signin-passkey").addEventListener("click", async () => {
  clear();
  if (!window.PublicKeyCredential) return fail("This browser doesn't support passkeys. Use email.");
  try {
    const { d: opts } = await post("/webauthn/login/options");
    opts.challenge = b64uToBuf(opts.challenge);
    if (opts.allowCredentials)
      opts.allowCredentials = opts.allowCredentials.map((c) => ({ ...c, id: b64uToBuf(c.id) }));
    const cred = await navigator.credentials.get({ publicKey: opts });
    const res = await post("/webauthn/login/verify", {
      id: cred.id, rawId: bufToB64u(cred.rawId), type: cred.type,
      response: {
        authenticatorData: bufToB64u(cred.response.authenticatorData),
        clientDataJSON: bufToB64u(cred.response.clientDataJSON),
        signature: bufToB64u(cred.response.signature),
        userHandle: cred.response.userHandle ? bufToB64u(cred.response.userHandle) : null,
      },
    });
    if (res.ok) start();
    else fail(res.d.error || "Sign-in failed.");
  } catch (e) { fail("No passkey found on this device. Try email."); }
});

let pendingEmail = "";

/* Ask for a code. The note says what happened: sent, held by the resend
   cooldown (the earlier code still works), or not confirmed (#220, #262). */
async function requestCode(email) {
  const res = await post("/api/auth/email", { email });
  if (!res.ok) { fail(res.d.error || "Couldn't send a code."); return false; }
  let note = `We emailed a code to ${email}.`;
  if (res.d.sent === false) {
    note = `We sent a code moments ago. You can ask for a new one in ${res.d.retry_after} seconds.`;
  } else if (res.d.sent !== true) {
    note = res.d.notice || "We couldn't confirm the code was sent. Check your inbox.";
  }
  $("signin-code-note").textContent = note;
  return true;
}

$("signin-email-btn").addEventListener("click", async () => {
  clear();
  const email = $("signin-email").value.trim();
  if (!email) return fail("Enter your email.");
  if (!(await requestCode(email))) return;
  pendingEmail = email;
  $("signin-start").hidden = true;
  $("signin-code").hidden = false;
  $("signin-code-input").focus();
});

$("signin-resend").addEventListener("click", async () => {
  clear();
  if (pendingEmail) await requestCode(pendingEmail);
});

$("signin-other-email").addEventListener("click", () => {
  clear();
  pendingEmail = "";
  $("signin-code-input").value = "";
  $("signin-code").hidden = true;
  $("signin-start").hidden = false;
  $("signin-email").focus();
});

$("signin-verify").addEventListener("click", async () => {
  clear();
  const res = await post("/api/auth/verify",
    { email: pendingEmail, code: $("signin-code-input").value.trim() });
  if (!res.ok) return fail(res.d.error || "Wrong code.");
  start();
});
$("signin-code-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") $("signin-verify").click();
});

// --- the review -------------------------------------------------------------

// What an item would send, in plain words: one line per record it asked
// for (checkin_answer.js recordNames), or why there is nothing to send.
function describe(m) {
  if (m.unsupported) return ["This wallet can't answer this yet. You can answer it in person instead."];
  if (m.form) return m.form.displays;
  if (!m.entries.length) return ["Nothing on file. The website will be told that."];
  return recordNames(m);
}

/* A form's questions (checkin_answer.js readForm), one radio group each.
   The text is the website's: textContent only, never markup. Nothing is
   picked to start with. */
function formQuestions(form, i) {
  const box = document.createElement("div");
  box.className = "checkin-form";
  form.questions.forEach((q, qi) => {
    const set = document.createElement("fieldset");
    set.className = "checkin-question";
    const legend = document.createElement("legend");
    legend.textContent = q.prefix ? `${q.prefix} ${q.text}` : q.text;
    set.append(legend);
    q.options.forEach((o, oi) => {
      const lab = document.createElement("label");
      const input = document.createElement("input");
      input.type = "radio";
      input.name = `form-${i}-${qi}`;
      input.value = String(oi);
      lab.append(input, ` ${o.display}`);
      set.append(lab);
    });
    box.append(set);
  });
  return box;
}

/* { linkId: option index } for the questions answered on item i. */
function formAnswers(form, i) {
  const out = {};
  form.questions.forEach((q, qi) => {
    const picked = $("wallet-items").querySelector(`input[name="form-${i}-${qi}"]:checked`);
    if (picked) out[q.linkId] = Number(picked.value);
  });
  return out;
}

/* The website's words inside a sentence of ours: at most `max` characters,
   and set apart by FIRST STRONG ISOLATE ... POP DIRECTIONAL ISOLATE, so a
   bidi control in them cannot reorder the wallet's words around them
   (#1030 security F2). */
function isolate(s, max) {
  const cut = s.length > max ? `${s.slice(0, max - 1)}\u2026` : s;
  return `\u2068${cut}\u2069`;
}

/* Resolves to {kind: "send", share, answers}, {kind: "declined-all"} or
   {kind: "closed"}. Every choice starts at "Don't share". */
function review(request, origin, matches) {
  // The origin is what the browser reported, shown as a website. Request
  // text is the website's own words: textContent only, never markup.
  $("wallet-origin").textContent = origin;
  const purpose = Object.hasOwn(request, "purpose") ? request.purpose : undefined;
  if (typeof purpose === "string" && purpose) {
    $("wallet-purpose").textContent = `The website says why: ${isolate(purpose, 200)}`;
    $("wallet-purpose").hidden = false;
  }
  const list = $("wallet-items");
  list.replaceChildren();
  matches.forEach((m, i) => {
    const li = document.createElement("li");
    li.className = "checkin-item";
    li.dataset.item = m.item.id;
    const title = document.createElement("div");
    title.className = "checkin-item-title";
    // A heading of the website's own carries its own label, inline, so its
    // words never read as ours (#1030 security F3; QA ruling: every piece
    // of website text is labelled where it stands).
    const theirs = websiteHeading(m);
    if (theirs === null) title.textContent = itemHeading(m);
    else {
      title.classList.add("checkin-website-title");
      const label = document.createElement("span");
      label.className = "checkin-website-label";
      label.textContent = "From the website: ";
      const name = document.createElement("span");
      name.className = "checkin-website-name";
      name.textContent = isolate(theirs, 80);
      title.append(label, name);
    }
    const what = document.createElement("ul");
    what.className = "checkin-item-what";
    describe(m).forEach((line) => {
      const row = document.createElement("li");
      row.textContent = line;
      what.append(row);
    });
    if (m.form) {
      // Everything under the heading is the website's words too: its intro
      // lines and its questions. One label says so, always, before them,
      // and they are set apart, as the purpose is (#1030 security F3).
      const says = document.createElement("p");
      says.className = "checkin-website-says";
      says.textContent = "The website asks:";
      li.append(title, says);
      if (m.form.displays.length) {
        what.classList.add("checkin-from-website");
        li.append(what);
      }
      li.append(formQuestions(m.form, i));
    } else li.append(title, what);
    if (!m.unsupported && (m.entries.length || m.form)) {
      // The share choice, set apart from any answers above it so it does
      // not read as one more question. `data-picked` says the person
      // tapped it: "Don't share" is where it starts, not a choice made.
      const group = document.createElement("div");
      group.className = "checkin-choice";
      group.setAttribute("role", "radiogroup");
      const ask = document.createElement("span");
      ask.className = "checkin-choice-label";
      ask.id = `choice-label-${i}`;
      ask.textContent = m.form ? "Send your answers to the website?" : "Send this to the website?";
      group.setAttribute("aria-labelledby", ask.id);
      const seg = document.createElement("div");
      seg.className = "checkin-seg";
      [["share", "Share"], ["decline", "Don't share"]].forEach(([value, label]) => {
        const lab = document.createElement("label");
        const input = document.createElement("input");
        input.type = "radio";
        input.name = `choice-${i}`;
        input.value = value;
        input.checked = value === "decline";
        // A choice by pointer, or by keyboard on the option already on:
        // Space or Enter there fires no click, and the message asks for
        // exactly that (#1030 QA). Arrow keys move the choice and click.
        const pick = () => { group.dataset.picked = "1"; };
        input.addEventListener("click", pick);
        input.addEventListener("keydown", (e) => {
          if (e.key === " " || e.key === "Spacebar" || e.key === "Enter") pick();
        });
        lab.append(input, ` ${label}`);
        seg.append(lab);
      });
      group.append(ask, seg);
      li.append(group);
    }
    list.append(li);
  });
  show("wallet-review");
  return new Promise((resolve) => {
    $("wallet-send").onclick = () => {
      clear();
      const share = {};
      const answers = {};
      let problem = null;
      matches.forEach((m, i) => {
        const picked = list.querySelector(`input[name="choice-${i}"]:checked`);
        const sharing = Boolean(picked) && picked.value === "share";
        const given = m.form ? formAnswers(m.form, i) : {};
        if (!sharing) {
          // Answered, but never chose: ask, rather than drop the answers
          // without a word (#1030 product decision).
          const chose = picked && picked.closest(".checkin-choice").dataset.picked === "1";
          if (m.form && Object.keys(given).length && !chose) {
            problem = problem || `You answered the questions in "${isolate(itemHeading(m), 80)}" but chose Don't share. Choose Share to send your answers, or Don't share to leave them out.`;
          }
          return;
        }
        share[m.item.id] = true;
        if (!m.form) return;
        answers[m.item.id] = given;
        // A form is sent whole or not at all: say so rather than decline it.
        if (Object.keys(given).length !== m.form.questions.length) {
          problem = problem || `Answer every question in "${isolate(itemHeading(m), 80)}", or choose Don't share for it.`;
        }
      });
      if (problem) return reviewFail(problem);
      resolve({ kind: "send", share, answers });
    };
    $("wallet-decline-all").onclick = () => resolve({ kind: "declined-all" });
    $("wallet-close").onclick = () => resolve({ kind: "closed" });
  });
}

function done(title, text) {
  $("wallet-done-title").textContent = title;
  $("wallet-done-text").textContent = text;
  $("wallet-ok").hidden = true;
  show("wallet-done");
}

// The website's check-in library closes this tab the moment any reply
// arrives. So a reason the person must read is held until they tap OK,
// and the reply goes only then.
function untilOk(title, text) {
  done(title, text);
  const ok = $("wallet-ok");
  ok.hidden = false;
  ok.focus();
  return new Promise((resolve) => { ok.onclick = () => resolve(); });
}

// A short pause, so a confirmation can be seen before the tab closes.
const SHOW_MS = 1500;
function briefly(title, text) {
  done(title, text);
  return new Promise((resolve) => setTimeout(resolve, SHOW_MS));
}

// What the website is told when the wallet can't answer: plain, and the
// same whatever the reason, which stays with the person.
const CANT_SHARE = "The person's wallet couldn't share records this time. They were told why.";

/* The account's records of the kinds the request names, or none when it
   names none this wallet can answer. Throws the server's fixed code. */
async function loadRecords(request, unsupportedItems) {
  const types = requestedTypes(request, unsupportedItems);
  if (!types.length) return prepareRecords({ entry: [] });
  const res = await post(card.dataset.records, { resource_types: types });
  if (!res.ok) throw new Error(typeof res.d.error === "string" ? res.d.error : "records");
  return prepareRecords(res.d);
}

// Why the records could not be read, in plain words. The website is told
// only CANT_SHARE, whatever the reason.
const NO_RECORDS = {
  no_records: "There are no records on your account yet. Upload or connect your own records first, then start check-in again.",
  sample_only: "Your account has only the sample records, which are made up and can't be shared. Upload or connect your own records first, then start check-in again.",
  several_records: "Your account holds more than one set of records, and this test version can answer from only one.",
  no_patient: "We couldn't tell whose records these are, so we didn't send any.",
  too_large: "Your records are too large to send this way.",
  paused: "Sharing records is paused on your account.",
  busy: "Records are already being added to your account. Start check-in again in a moment.",
  // A real-record connection the relay cannot answer from yet (#1027 QA F1).
  records_not_ready: "Your records aren't ready to share yet. Finish connecting them in CareAgents, then start check-in again.",
  records_disconnected: "Your records are disconnected. Connect them again in CareAgents, then start check-in again.",
  not_your_records: "Check-in shares your own records. Upload or connect your own records first.",
  has_records: "Your records aren't ready to share yet. Finish connecting them in CareAgents, then start check-in again.",
};

// --- the connectathon test patient ----------------------------------------

// The reasons that mean "nothing real to share", where the page may offer
// the test patient instead (card.dataset.testPatient, set by the server
// only when CARE_CHECKIN_TEST_PATIENT is on).
// Only where the server will make one: no real-record connection at all
// (`no_records`) or only the sample. Each says only what is missing: the
// longer NO_RECORDS text tells the person to go and connect records,
// which this screen offers instead.
const OFFER_TEST = {
  no_records: "There are no records on your account yet.",
  sample_only: "The sample records can't be used for check-in. You can use a made-up connectathon test patient instead.",
};

/* Resolves to true when the person chose the test patient. */
function offerTestPatient(why) {
  $("wallet-test-patient-why").textContent = why;
  show("wallet-test-patient");
  return new Promise((resolve) => {
    $("wallet-test-patient-use").onclick = () => resolve(true);
    $("wallet-test-patient-no").onclick = () => resolve(false);
  });
}

/* Make the account's test patient (once; the server makes nothing the
   second time), then read its records. Sends `{}`: nothing about the
   request or the website. Throws the server's fixed code. */
async function useTestPatient(request, unsupportedItems) {
  done("Getting the test patient", "This takes a few seconds.");
  const res = await post(card.dataset.testPatient, {});
  if (!res.ok) throw new Error(typeof res.d.error === "string" ? res.d.error : "records");
  return loadRecords(request, unsupportedItems);
}

// --- the hand-off -----------------------------------------------------------

let started = false;
function start() {
  if (started) return;
  started = true;
  clear();
  show("wallet-waiting");
  // The website sends its request a moment after this tab says it is ready.
  // If nothing comes, say so and what to do, rather than wait forever.
  const late = setTimeout(() => { $("wallet-waiting-late").hidden = false; },
    WAIT_MS);
  const served = serveWebWallet({
    // Stay open to say what happened; the person closes the tab.
    closeAfterReply: false,
    async onRequest({ request, origin, unsupportedItems }) {
      clearTimeout(late);
      let records;
      let code = "";
      try { records = await loadRecords(request, unsupportedItems); } catch (e) {
        code = e.message;
      }
      if (Object.hasOwn(OFFER_TEST, code) && card.dataset.testPatient) {
        if (!(await offerTestPatient(OFFER_TEST[code]))) {
          await briefly("Nothing shared", "The website will be told you chose not to share anything. This tab may close on its own.");
          return { response: declineAll(request) };
        }
        code = "";
        try { records = await useTestPatient(request, unsupportedItems); } catch (e) {
          code = e.message;
        }
      }
      if (code) {
        const why = Object.hasOwn(NO_RECORDS, code) ? NO_RECORDS[code]
          : "We couldn't read your records just now. Try check-in again in a few minutes.";
        await untilOk("Couldn't answer", `${why} Tap OK to tell the website.`);
        return { error: CANT_SHARE };
      }
      const matches = matchItems(request, unsupportedItems, records);
      const choice = await review(request, origin, matches);
      if (choice.kind === "closed") {
        await briefly("Closed", "Nothing was sent. This tab may close on its own.");
        return { declined: true };
      }
      if (choice.kind === "declined-all") {
        await briefly("Nothing shared", "The website will be told you chose not to share anything. This tab may close on its own.");
        return { response: declineAll(request) };
      }
      const response = buildResponse(request, matches, choice.share, choice.answers);
      if (checkWalletResponse(request, response).length) {
        await untilOk("Couldn't answer", "Something went wrong building the answer. Nothing was shared. Tap OK to tell the website.");
        return { error: CANT_SHARE };
      }
      await briefly("Sending", "Your choices are going to the website now. This tab may close on its own.");
      return { response };
    },
    onInvalidRequest(message, origin) {
      done("Couldn't read the request",
        `${origin} sent a request this wallet can't read. Nothing was shared.`);
    },
  });
  if (!served.opened) show("wallet-direct");
}

// Opened directly, not by a check-in page: nothing to sign in for.
if (!window.opener) show("wallet-direct");
else if (card.dataset.signedIn === "1") start();
else show("wallet-signin");
