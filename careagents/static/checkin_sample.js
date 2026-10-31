/* The sample new-patient form (careagents/templates/checkin_sample.html,
   #1073): a made-up clinic's check-in page on this site.

   It asks the way any check-in website does, with the organizers' Verifier
   library (vendored, the 0.4.4 release's checkin.js): it opens
   /checkin/wallet in a new tab, sends a SMART Health Check-in request there
   by postMessage, and gets back a sealed answer, which the library opens
   in this tab. So the wallet runs exactly as it does for an outside
   website. What comes back is shown here and nowhere else: this page calls
   no server and keeps nothing.

   The form is three kinds of record the wallet answers from records
   (medicines, allergies, health problems) and a short questionnaire: one
   pick-one question the person answers in the other tab, and one free-text
   question the wallet does not answer, so it is shown as not answered. */
import { runCheckin, webWallet } from "./vendor/smart-health-checkin-client/0.4.4/checkin.js";
// With this file's ?v= build stamp, as the wallet page loads it (#909).
const { recordNames } = await import(
  `./checkin_answer.js${new URL(import.meta.url).search}`);

const $ = (id) => document.getElementById(id);

const US_CORE = "http://hl7.org/fhir/us/core/StructureDefinition/";
const FORM_URL = `${location.origin}/checkin/sample/Questionnaire/new-patient`;
const FHIR_JSON = ["application/fhir+json"];

/* Each item: what it asks for, its heading here, and the kinds of record
   that answer it (so the names shown are the item's own records, not the
   ones sent along with them). */
const RECORD_ITEMS = [
  { id: "medications", heading: "Your medicines", profile: "us-core-medicationrequest",
    types: ["MedicationRequest", "MedicationStatement"] },
  { id: "allergies", heading: "Your allergies", profile: "us-core-allergyintolerance",
    types: ["AllergyIntolerance"] },
  { id: "problems", heading: "Your health problems",
    profile: "us-core-condition-problems-health-concerns", types: ["Condition"] },
];
const QUESTIONS = {
  "first-visit": "Is this your first visit to our clinic?",
  reason: "What brings you in today?",
};

function newRequest() {
  return {
    type: "smart-health-checkin-request", version: "1",
    id: crypto.randomUUID(), purpose: "New-patient check-in",
    fhirVersions: ["4.0.1"],
    items: [
      ...RECORD_ITEMS.map((it) => ({
        id: it.id, title: it.heading, accept: FHIR_JSON,
        content: { kind: "selection.fhir", profiles: [US_CORE + it.profile] },
      })),
      { id: "intake", title: "A few questions from the clinic", accept: FHIR_JSON,
        content: {
          kind: "form.fhir", questionnaireCanonical: `${FORM_URL}|1`,
          questionnaire: {
            resourceType: "Questionnaire", url: FORM_URL, version: "1",
            status: "active", title: "A few questions from the clinic",
            item: [
              { linkId: "first-visit", type: "choice", text: QUESTIONS["first-visit"],
                answerOption: [
                  { valueCoding: { system: "http://loinc.org", code: "LA33-6", display: "Yes" } },
                  { valueCoding: { system: "http://loinc.org", code: "LA32-8", display: "No" } },
                ] },
              // Free text: the wallet leaves it for the person to tell the
              // clinic themselves.
              { linkId: "reason", type: "string", text: QUESTIONS.reason },
            ],
          },
        } },
    ],
  };
}

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}

/* One answer card: a heading, then either a list or one plain line. */
function card(heading, lines, none) {
  const li = el("li");
  li.append(el("div", "sample-q", heading));
  if (lines.length) {
    const ul = el("ul", "sample-a");
    lines.forEach((l) => ul.append(el("li", "", l)));
    li.append(ul);
  } else {
    li.append(el("p", "sample-none", none));
  }
  return li;
}

const NOT_SHARED = {
  declined: "Not shared. You chose not to share this.",
  unavailable: "Nothing on file to share.",
  unsupported: "Couldn't be answered from your records.",
};

function showResult(title, text, cards = []) {
  $("sample-form").hidden = true;
  $("sample-result-title").textContent = title;
  $("sample-result-text").textContent = text;
  $("sample-answers").replaceChildren(...cards);
  $("sample-result").hidden = false;
  $("sample-result-title").focus();
}

/* The answer, as the clinic would see it. Each record named by the
   wallet's own words for it (checkin_answer.js recordNames). */
function showAnswer(json) {
  const status = new Map((json.requestStatus || []).map((s) => [s.item, s.status]));
  const value = new Map((json.artifacts || []).map((a) => [a.fulfills[0], a.value]));
  if (!value.size) {
    showResult("Nothing was shared", "The form is still blank. The clinic would get nothing from your records.");
    return;
  }
  const cards = RECORD_ITEMS.map((it) => {
    const bundle = value.get(it.id);
    const entries = (bundle && Array.isArray(bundle.entry)) ? bundle.entry : [];
    const primary = entries.filter((e) => e && e.resource &&
      it.types.includes(e.resource.resourceType));
    return card(it.heading, recordNames({ primary, entries }),
      NOT_SHARED[status.get(it.id)] || NOT_SHARED.unavailable);
  });
  const form = value.get("intake");
  const answered = (form && Array.isArray(form.item)) ? form.item : [];
  const first = answered.find((q) => q.linkId === "first-visit");
  const pick = first && first.answer && first.answer[0] && first.answer[0].valueCoding;
  cards.push(card(QUESTIONS["first-visit"], pick ? [pick.display] : [],
    NOT_SHARED[status.get("intake")] || NOT_SHARED.declined));
  cards.push(card(QUESTIONS.reason, [],
    "Not answered from your records. You'd tell the clinic yourself."));
  showResult("Your form, filled in",
    "This is what the clinic would get. It stays on this page and is gone when you close it.",
    cards);
}

$("sample-start").onclick = () => {
  // No await before this: the new tab must open on the person's own tap.
  const wallet = webWallet({ id: "careagents", name: "CareAgents",
    walletUrl: `${location.origin}/checkin/wallet` });
  const running = runCheckin(newRequest(), { wallet });
  $("sample-start").disabled = true;
  $("sample-waiting").hidden = false;
  running.then((r) => {
    if (r.status === "completed" && r.response) showAnswer(r.response.json);
    else if (r.status === "declined") showResult("Nothing was shared", "The other tab closed before you shared anything. The form is still blank.");
    else showResult("The form wasn't filled in", "Nothing was shared. You can start the form again.");
  }, () => showResult("The form wasn't filled in", "Nothing was shared. You can start the form again."));
};

$("sample-again").onclick = () => {
  $("sample-result").hidden = true;
  $("sample-answers").replaceChildren();
  $("sample-waiting").hidden = true;
  $("sample-start").disabled = false;
  $("sample-form").hidden = false;
};

// Every handler is attached: the form can start.
$("sample-start").disabled = false;
