/* SMART Health Check-in web wallet page (careagents/checkin_wallet.py).

   Order is the point. serveWebWallet posts `ready` to the opener the moment
   it starts, and the check-in page sends its request on `ready`. So sign-in
   finishes here first, in this tab, without navigating (a redirect would
   lose the request); only then does the library start listening.

   Nothing about the request or the answer goes to the CareAgents server.
   The request arrives by postMessage, the answer is built and sealed here
   and leaves by postMessage. The one fetch is a GET of the synthetic test
   patient (FHI-51); the others are the existing sign-in endpoints. */
import {
  checkWalletResponse, declineAll, serveWebWallet,
} from "./vendor/smart-health-checkin-client/0.4.4/wallet.js";
// Loaded with this file's own ?v= build stamp, so a cached copy from an
// earlier deploy never runs against this page (#909). A static import
// cannot carry it.
const {
  buildResponse, itemHeading, matchItems, prepareRecords, recordNames,
} = await import(`./checkin_answer.js${new URL(import.meta.url).search}`);

const $ = (id) => document.getElementById(id);
const card = $("wallet");
const SECTIONS = ["wallet-signin", "wallet-waiting", "wallet-direct",
  "wallet-review", "wallet-done"];
const show = (id) => SECTIONS.forEach((s) => ($(s).hidden = s !== id));
const err = $("wallet-err");
// How long "Waiting for the request" waits before it offers a next step.
// A test can shorten it with ?wait=<ms>; nothing else reads the query.
const WAIT_MS = Number(new URLSearchParams(location.search).get("wait")) || 20000;
const fail = (msg) => { err.textContent = msg; err.hidden = false; };
const clear = () => { err.hidden = true; };

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
  if (m.unsupported) return ["This wallet can't answer this yet."];
  if (!m.entries.length) return ["Nothing on file. The website will be told that."];
  return recordNames(m);
}

/* Resolves to {kind: "send", share}, {kind: "declined-all"} or
   {kind: "closed"}. Every choice starts at "Don't share". */
function review(request, origin, matches) {
  // The origin is what the browser reported, shown as a website. Request
  // text is the website's own words: textContent only, never markup.
  $("wallet-origin").textContent = origin;
  if (typeof request.purpose === "string" && request.purpose) {
    $("wallet-purpose").textContent = `The website says why: ${request.purpose}`;
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
    title.textContent = itemHeading(m);
    const what = document.createElement("ul");
    what.className = "checkin-item-what";
    describe(m).forEach((line) => {
      const row = document.createElement("li");
      row.textContent = line;
      what.append(row);
    });
    li.append(title, what);
    if (!m.unsupported && m.entries.length) {
      const group = document.createElement("div");
      group.className = "checkin-choice";
      group.setAttribute("role", "radiogroup");
      [["share", "Share"], ["decline", "Don't share"]].forEach(([value, label]) => {
        const lab = document.createElement("label");
        const input = document.createElement("input");
        input.type = "radio";
        input.name = `choice-${i}`;
        input.value = value;
        input.checked = value === "decline";
        lab.append(input, ` ${label}`);
        group.append(lab);
      });
      li.append(group);
    }
    list.append(li);
  });
  show("wallet-review");
  return new Promise((resolve) => {
    $("wallet-send").onclick = () => {
      const share = {};
      matches.forEach((m, i) => {
        const picked = list.querySelector(`input[name="choice-${i}"]:checked`);
        if (picked && picked.value === "share") share[m.item.id] = true;
      });
      resolve({ kind: "send", share });
    };
    $("wallet-decline-all").onclick = () => resolve({ kind: "declined-all" });
    $("wallet-close").onclick = () => resolve({ kind: "closed" });
  });
}

function done(title, text) {
  $("wallet-done-title").textContent = title;
  $("wallet-done-text").textContent = text;
  show("wallet-done");
}

async function loadRecords() {
  const r = await fetch(card.dataset.records, { credentials: "same-origin" });
  if (!r.ok) throw new Error("records");
  return prepareRecords(await r.json());
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
      try { records = await loadRecords(); } catch (e) {
        done("Couldn't answer", "We couldn't read the test records. The website was told something went wrong.");
        return { error: "The wallet couldn't read its records" };
      }
      const matches = matchItems(request, unsupportedItems, records);
      const choice = await review(request, origin, matches);
      if (choice.kind === "closed") {
        done("Closed", "Nothing was sent. You can close this tab.");
        return { declined: true };
      }
      if (choice.kind === "declined-all") {
        done("Nothing shared", "The website was told you chose not to share anything. You can close this tab.");
        return { response: declineAll(request) };
      }
      const response = buildResponse(request, matches, choice.share);
      if (checkWalletResponse(request, response).length) {
        done("Couldn't answer", "Something went wrong building the answer. Nothing was shared.");
        return { error: "The wallet couldn't build a valid answer" };
      }
      done("Sent", "Your choices went to the website. You can close this tab.");
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
