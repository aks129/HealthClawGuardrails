/* SMART Health Check-in: what the wallet can answer, and the answer itself.
   No page, no network: checkin_wallet.js does those. Kept apart so the
   rules below can be exercised on their own (e2e checkin-wallet spec).

   The rules, per request item:
     unsupported  the library said so, or it is neither a FHIR selection
                  this wallet can send nor a small inline form it can show
                  (readForm)
     unavailable  nothing on file claims the requested profile. Nothing is
                  stamped with a claim to make it fit, and "no known
                  allergies" is never sent: an allergy goes only when it
                  codes a substance (shareableAllergy), so an absence, a
                  refuted record or one said only in words reads unavailable
     declined     there is something, and the person did not choose to share
     fulfilled    the person chose to share it: one Bundle, fhirVersion 4.0.1
                  (a form: one QuestionnaireResponse, every question answered)

   Request keys are read with Object.hasOwn (own()), never `in` or a bare
   lookup: the request is the website's, and a key named like a built-in
   property must not reach a prototype.
*/
import { selectEntries, selects } from "./vendor/smart-health-checkin-client/0.4.4/wallet.js";

const FHIR_JSON = "application/fhir+json";
const SNOMED = ["http://snomed.info/sct", "urn:oid:2.16.840.1.113883.6.96"];
const RXNORM = "http://www.nlm.nih.gov/research/umls/rxnorm";

/* obj[key] when obj has it as its own property, else undefined. */
const own = (obj, key) => (obj !== null && typeof obj === "object" &&
  Object.hasOwn(obj, key) ? obj[key] : undefined);

// SNOMED 716186003 "no known allergy" and its descendants, its inactive
// predecessor 160244002, and 260413007 "None". With NKA_LOINC, the same set
// as careagents/agent.py _NKA_CODES; tests/test_checkin_wallet.py holds them
// equal.
export const NKA_CODES = [
  "716186003", "409137002", "428197003", "428607008",
  "429625007", "716220001", "1003774007", "160244002", "260413007",
];
const LOINC = "http://loinc.org";
// LOINC answers that state an absence (r6.terminology.LOINC_ABSENCE_ANSWERS).
export const NKA_LOINC = ["LA10030-7", "LA9134-3", "LA4715-4"];
// SNOMED 1631000175102 "Patient not asked": allergies not checked, which is
// no allergy either (r6.terminology.ALLERGY_STATUS_CONCEPTS). With the two
// lists above, every engine absence concept (r6.terminology
// ABSENCE_CONCEPTS); tests/test_checkin_wallet.py holds them equal.
export const ALLERGY_STATUS_CODES = ["1631000175102"];

// Code systems that name a substance (US Core's AllergyIntolerance.code
// binding): RxNorm, SNOMED by either spelling, UNII, NDF-RT.
const SUBSTANCE_SYSTEMS = [RXNORM, ...SNOMED, "http://fdasis.nlm.nih.gov",
  "http://hl7.org/fhir/ndfrt"];
// A verificationStatus that takes the allergy back: itself a statement of
// absence, or a record that should never have been.
const WITHDRAWN = ["refuted", "entered-in-error"];

function codings(concept) {
  const c = own(concept, "coding");
  return Array.isArray(c) ? c.filter((x) => x !== null && typeof x === "object") : [];
}

/* A code as the engine compares it: a string or a number, trimmed. A
   padded " 716186003" is still that concept, not a substance. */
const codeOf = (c) => {
  const v = own(c, "code");
  return typeof v === "string" || typeof v === "number" ? String(v).trim() : "";
};

function statesAbsence(c) {
  const system = own(c, "system");
  const code = codeOf(c);
  return (SNOMED.includes(system) &&
      (NKA_CODES.includes(code) || ALLERGY_STATUS_CODES.includes(code))) ||
    (system === LOINC && NKA_LOINC.includes(code));
}

/* An allow-list, not a list of absence phrases (#960 security review):
   an AllergyIntolerance is shared only when a coding names a substance in
   a substance code system, no coding states an absence, and its
   verificationStatus is not refuted or entered-in-error. Free text is
   never read, either to send a record or to hold one back; a record said
   only in words reads unavailable. Errs one way: toward unavailable,
   never toward an inferred "none". */
export function shareableAllergy(resource) {
  const code = codings(own(resource, "code"));
  if (code.some(statesAbsence)) return false;
  if (codings(own(resource, "verificationStatus")).some(
    (c) => WITHDRAWN.includes(own(c, "code")))) return false;
  return code.some((c) => SUBSTANCE_SYSTEMS.includes(own(c, "system")) &&
    typeof own(c, "code") === "string" && codeOf(c) !== "");
}

const shareable = (resource) => resource.resourceType !== "AllergyIntolerance" ||
  shareableAllergy(resource);

/* What the engine's checkin export can send (r6/checkin.py CHECKIN_TYPES),
   and the profile claims that make a row of each type exportable
   (CLAIMS_BY_TYPE). tests/test_checkin_relay.py holds both equal. */
export const CHECKIN_TYPES = [
  "AllergyIntolerance", "Condition", "Coverage", "Immunization",
  "MedicationRequest", "Patient",
];
export const PROFILE_TYPES = {
  "http://hl7.org/fhir/us/core/StructureDefinition/us-core-patient": "Patient",
  "http://hl7.org/fhir/us/core/StructureDefinition/us-core-condition": "Condition",
  "http://hl7.org/fhir/us/core/StructureDefinition/us-core-condition-problems-health-concerns": "Condition",
  "http://hl7.org/fhir/us/core/StructureDefinition/us-core-condition-encounter-diagnosis": "Condition",
  "http://hl7.org/fhir/us/core/StructureDefinition/us-core-allergyintolerance": "AllergyIntolerance",
  "http://hl7.org/fhir/us/core/StructureDefinition/us-core-medicationrequest": "MedicationRequest",
  "http://hl7.org/fhir/us/core/StructureDefinition/us-core-immunization": "Immunization",
  "http://hl7.org/fhir/us/core/StructureDefinition/us-core-coverage": "Coverage",
  "http://hl7.org/fhir/us/insurance-card/StructureDefinition/C4DIC-Coverage": "Coverage",
};

/* Whether this wallet can answer the item at all. */
function answerable(item, unsupported) {
  const content = own(item, "content");
  const accept = own(item, "accept");
  return !unsupported.has(own(item, "id")) &&
    own(content, "kind") === "selection.fhir" &&
    Array.isArray(accept) && accept.includes(FHIR_JSON);
}

/* --- Forms (FHI-55) -------------------------------------------------------

   A `form.fhir` item is answered only from the Questionnaire the website
   sent inline, and only when it is small, flat and single-choice, as the
   PHQ-2 is: display lines and choice questions, each with its own coded
   answer options. Everything shown is the website's own words, put on the
   page as text, never as markup; the Questionnaire's generated narrative
   is never read. Anything else is unsupported rather than half shown. The
   answers stay in this tab: they go only into the sealed response. */

const FORM_MAX_CHARS = 32768;      // the Questionnaire, serialised
const FORM_MAX_QUESTIONS = 10;
const FORM_MAX_OPTIONS = 10;
const FORM_TEXT_MAX = 500;
const FORM_CANONICAL_MAX = 1024;
// What would make a question more than one pick from a fixed list.
const FORM_REFUSED_KEYS = ["item", "enableWhen", "answerValueSet", "initial"];

const text = (v, max = FORM_TEXT_MAX) =>
  typeof v === "string" && v.trim() && v.length <= max ? v : null;

// Bidi embedding, override and isolate controls (U+202A-202E, U+2066-2069).
const BIDI = /[‪-‮⁦-⁩]/u;

/* Words the person will read: `text`, with at least one letter or digit
   (a string of format characters alone, U+200B and the like, is blank on
   screen however it trims), and no bidi control that could reorder what
   is around it. Otherwise null, and the form is unsupported (#1030
   security F1). */
const words = (v, max = FORM_TEXT_MAX) =>
  text(v, max) && /[\p{L}\p{N}]/u.test(v) && !BIDI.test(v) ? v : null;

function readOption(option) {
  const c = own(option, "valueCoding");
  const code = text(own(c, "code"), 256);
  const display = words(own(c, "display"));
  const system = own(c, "system");
  if (!code || !display) return null;
  if (system !== undefined && !text(system, FORM_CANONICAL_MAX)) return null;
  const coding = system === undefined ? { code, display } : { system, code, display };
  return { coding, display };
}

/* The form an item carries, or null when this wallet will not answer it:
   { canonical, title, displays: [text], questions: [{ linkId, prefix,
   text, options: [{ coding, display }] }] }. */
export function readForm(item) {
  const content = own(item, "content");
  const accept = own(item, "accept");
  if (own(content, "kind") !== "form.fhir" || !Array.isArray(accept) ||
      !accept.includes(FHIR_JSON)) return null;
  const q = own(content, "questionnaire");
  if (own(q, "resourceType") !== "Questionnaire") return null;
  let size;
  try { size = JSON.stringify(q).length; } catch (e) { return null; }
  if (size > FORM_MAX_CHARS) return null;

  // What the answer names: the item's canonical exactly as sent (a `|1`
  // version included), else the inline url, with its version if it has one.
  let canonical = own(content, "questionnaireCanonical");
  if (canonical === undefined) {
    const url = text(own(q, "url"), FORM_CANONICAL_MAX);
    const version = own(q, "version");
    canonical = url && (version === undefined ? url
      : text(version, 64) && `${url}|${version}`);
  }
  if (!text(canonical, FORM_CANONICAL_MAX)) return null;

  const items = own(q, "item");
  if (!Array.isArray(items)) return null;
  const displays = [];
  const questions = [];
  const linkIds = new Set();
  for (const it of items) {
    if (it === null || typeof it !== "object") return null;
    if (FORM_REFUSED_KEYS.some((k) => Object.hasOwn(it, k))) return null;
    if (own(it, "repeats") === true) return null;
    const linkId = text(own(it, "linkId"), 255);
    const said = words(own(it, "text"));
    if (!linkId || !said || linkIds.has(linkId)) return null;
    linkIds.add(linkId);
    const type = own(it, "type");
    if (type === "display") { displays.push(said); continue; }
    if (type !== "choice") return null;
    const prefix = own(it, "prefix");
    if (prefix !== undefined && !words(prefix, 16)) return null;
    const options = own(it, "answerOption");
    if (!Array.isArray(options) || options.length < 2 ||
        options.length > FORM_MAX_OPTIONS) return null;
    const read = options.map(readOption);
    if (read.includes(null)) return null;
    questions.push({ linkId, prefix: prefix || "", text: said, options: read });
  }
  if (!questions.length || questions.length > FORM_MAX_QUESTIONS) return null;
  return { canonical, title: words(own(q, "title")) || "", displays, questions };
}

/* The QuestionnaireResponse for `picked` ({ linkId: option index }), or
   null unless every question has exactly one of its own options. */
function questionnaireResponse(form, picked) {
  const item = [];
  for (const question of form.questions) {
    const i = own(picked, question.linkId);
    if (!Number.isInteger(i) || i < 0 || i >= question.options.length) return null;
    item.push({ linkId: question.linkId, text: question.text,
      answer: [{ valueCoding: { ...question.options[i].coding } }] });
  }
  return {
    resourceType: "QuestionnaireResponse",
    status: "completed",
    questionnaire: form.canonical,
    authored: new Date().toISOString(),
    item,
  };
}

const strings = (obj, key) => {
  const v = own(obj, key);
  return Array.isArray(v) ? v.filter((s) => typeof s === "string") : [];
};

/* The kinds of record the request's answerable items ask for, sorted: what
   the wallet asks the CareAgents relay for, and so the exact type set the
   engine's single-use token is minted for. A profile is read as the
   library reads it (base before any `|version`; profilesFrom as a stem);
   one this table does not know asks for nothing. An item with no selector
   asks for every type. Nothing outside CHECKIN_TYPES is ever asked for. */
export function requestedTypes(request, unsupportedItems) {
  const unsupported = new Set((unsupportedItems || []).map((u) => own(u, "id")));
  const items = own(request, "items");
  const wanted = new Set();
  for (const item of Array.isArray(items) ? items : []) {
    if (!answerable(item, unsupported)) continue;
    const content = own(item, "content");
    const types = strings(content, "resourceTypes");
    const profiles = strings(content, "profiles");
    const stems = strings(content, "profilesFrom");
    let found;
    if (profiles.length || stems.length) {
      found = [];
      for (const p of profiles) {
        const base = p.split("|")[0];
        if (Object.hasOwn(PROFILE_TYPES, base)) found.push(PROFILE_TYPES[base]);
      }
      for (const s of stems) {
        const stem = s.split("|")[0].replace(/\/+$/, "") + "/";
        for (const [url, t] of Object.entries(PROFILE_TYPES)) {
          if (url.startsWith(stem)) found.push(t);
        }
      }
      if (types.length) found = found.filter((t) => types.includes(t));
    } else {
      found = types.length ? types : CHECKIN_TYPES;
    }
    found.filter((t) => CHECKIN_TYPES.includes(t)).forEach((t) => wanted.add(t));
  }
  return [...wanted].sort();
}

/* The record set as Bundle entries whose references can be followed.
   selectEntries pulls along what a match references (a payer, a
   prescriber) only when the reference IS that entry's fullUrl, and records
   say "Patient/<id>". So, on a copy: each "Type/id" that names an entry here
   becomes that entry's fullUrl. */
export function prepareRecords(bundle) {
  const entries = ((bundle && bundle.entry) || [])
    .filter((e) => e && e.fullUrl && e.resource && e.resource.resourceType)
    .map((e) => ({ fullUrl: e.fullUrl, resource: structuredClone(e.resource) }));
  const urlOf = new Map(entries.map((e) =>
    [`${e.resource.resourceType}/${e.resource.id}`, e.fullUrl]));
  const rewrite = (node) => {
    if (Array.isArray(node)) { node.forEach(rewrite); return; }
    if (!node || typeof node !== "object") return;
    for (const [k, v] of Object.entries(node)) {
      if (k === "reference" && typeof v === "string" && urlOf.has(v)) node[k] = urlOf.get(v);
      else rewrite(v);
    }
  };
  entries.forEach((e) => rewrite(e.resource));
  const patient = entries.find((e) => e.resource.resourceType === "Patient");
  return { entries, patientFullUrl: patient ? patient.fullUrl : null };
}

/* For each item: { item, unsupported, entries, primary }, and `form` for a
   form this wallet can show (readForm). `entries` is what would be sent;
   `primary` is the part of it the item asked for, which is what the person
   is shown (a payer or prescriber rides along unnamed). */
export function matchItems(request, unsupportedItems, records) {
  const unsupported = new Set((unsupportedItems || []).map((u) => own(u, "id")));
  return own(request, "items").map((item) => {
    const form = unsupported.has(own(item, "id")) ? null : readForm(item);
    if (form) return { item, unsupported: false, form, entries: [], primary: [] };
    if (!answerable(item, unsupported)) {
      return { item, unsupported: true, entries: [], primary: [] };
    }
    const content = own(item, "content");
    const sendable = records.entries.filter((e) => shareable(e.resource));
    // Only what the item asks for makes it answerable: a payer that would
    // ride along by reference is not, on its own, something to share.
    const primary = sendable.filter((e) => selects(content, e.resource));
    if (!primary.length) return { item, unsupported: false, entries: [], primary: [] };
    // The Patient has an item of its own; elsewhere it is not pulled along.
    const asksForPatient = primary.some((e) => e.fullUrl === records.patientFullUrl);
    const exclude = asksForPatient || !records.patientFullUrl ? [] : [records.patientFullUrl];
    return { item, unsupported: false, primary,
      entries: selectEntries(content, sendable, { exclude }) };
  });
}

/* The SMART response. `share` maps item id -> true for what the person
   chose to share; anything else is not shared. `answers` maps a form's
   item id -> { linkId: option index }; a form chosen with any question
   unanswered is declined, never sent in part. No HTEST tag: the records
   are the account's own now, not the synthetic test patient, and a test
   marker on them would mislabel them (the #960 note said the real-record
   export drops it). */
export function buildResponse(request, matches, share, answers = {}) {
  const artifacts = [];
  const requestStatus = [];
  for (const m of matches) {
    const id = own(m.item, "id");
    if (m.unsupported) { requestStatus.push({ item: id, status: "unsupported" }); continue; }
    if (m.form) {
      const qr = own(share, id) === true ? questionnaireResponse(m.form, own(answers, id)) : null;
      if (!qr) { requestStatus.push({ item: id, status: "declined" }); continue; }
      artifacts.push({ id: `fhir-${id}`, mediaType: FHIR_JSON, fhirVersion: "4.0.1",
        fulfills: [id], value: qr });
      requestStatus.push({ item: id, status: "fulfilled" });
      continue;
    }
    if (!m.entries.length) { requestStatus.push({ item: id, status: "unavailable" }); continue; }
    if (own(share, id) !== true) { requestStatus.push({ item: id, status: "declined" }); continue; }
    artifacts.push({
      id: `fhir-${id}`,
      mediaType: FHIR_JSON,
      fhirVersion: "4.0.1",
      fulfills: [id],
      value: {
        resourceType: "Bundle",
        type: "collection",
        entry: m.entries.map((e) => ({ fullUrl: e.fullUrl, resource: e.resource })),
      },
    });
    requestStatus.push({ item: id, status: "fulfilled" });
  }
  return {
    type: "smart-health-checkin-response",
    version: "1",
    requestId: own(request, "id"),
    artifacts,
    requestStatus,
  };
}

/* --- What the person is shown, in plain words ---------------------------

   Never a FHIR type name, never a coding's `display`, never an upstream
   feed's words: those are where a real feed puts other people's names. The
   records come only from the engine's checkin export, which drops every
   upstream `display` and `text` and writes each CodeableConcept's `text`
   as OUR label for its code, from r6/terminology.py (r6/access.py
   _codes_with_our_text), or leaves none. So a record is named by that
   engine-written `text`, the same words the website receives; then by
   LABELS, for a code the engine does not label yet; then by a plain
   fallback ("A medicine"). Otherwise by structured fields of the person's
   own (their name, the payer Organization's name, a masked member id).
   tests/test_checkin_relay.py runs a canary through the real engine. */

const CVX = "http://hl7.org/fhir/sid/cvx";

export const LABELS = {
  [`${SNOMED[0]}|38341003`]: "Hypertensive disorder",
  [`${RXNORM}|7980`]: "Penicillin",
  [`${RXNORM}|314076`]: "Lisinopril 10 mg",
  [`${CVX}|150`]: "Influenza vaccine",
};

const HEADINGS = {
  Patient: "Your name, birth date and contact details",
  Condition: "Your health problems",
  AllergyIntolerance: "Your allergies",
  MedicationRequest: "Your medicines",
  MedicationStatement: "Your medicines",
  Immunization: "Your vaccines",
  Coverage: "Your insurance card",
};

/* Our heading for an item, from what it matched, or, with nothing on
   file, from the one kind of record it asks for: the same heading either
   way. The website's own title only when there is no one kind to name it
   by (a form, say), which the page shows as the website's (websiteHeading). */
export function itemHeading(match) {
  return ourHeading(match) || websiteHeading(match);
}

function ourHeading(match) {
  let types = new Set((match.primary || []).map((e) => e.resource.resourceType));
  if (!types.size) types = new Set(requestedTypes({ items: [match.item] }, []));
  if (types.size === 1) {
    const h = own(HEADINGS, [...types][0]);
    if (typeof h === "string") return h;
  }
  return null;
}

/* The website's own words for an item we have no heading of ours for: its
   title, else its id, and only ever a string. Null when the heading is
   ours. */
export function websiteHeading(match) {
  if (ourHeading(match)) return null;
  for (const key of ["title", "id"]) {
    const v = own(match.item, key);
    if (typeof v === "string" && v) return v;
  }
  return "";
}

function labelOf(concept) {
  // The engine's label for this code (see above), never upstream words.
  const ours = own(concept, "text");
  if (typeof ours === "string" && ours.trim()) return ours.trim();
  for (const c of codings(concept)) {
    const system = SNOMED.includes(own(c, "system")) ? SNOMED[0] : own(c, "system");
    const label = own(LABELS, `${system}|${String(own(c, "code"))}`);
    if (typeof label === "string") return label;
  }
  return null;
}

const MONTHS = ["January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December"];

/* A FHIR date or dateTime as "4 July 1985" ("July 1985", "1985" when that
   is all it says). Read from its digits, never through a time zone, so
   the day cannot shift. */
export function day(s) {
  const m = typeof s === "string" ? /^(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?/.exec(s) : null;
  if (!m) return "";
  const month = m[2] ? MONTHS[Number(m[2]) - 1] : undefined;
  if (!month) return m[1];
  return m[3] ? `${Number(m[3])} ${month} ${m[1]}` : `${month} ${m[1]}`;
}

/* The last 4 characters, and only of an id at least twice that long, so at
   most half of it shows; a shorter one is "on file" (#960 security note: a
   5-character id showed 4 of its 5). */
export function masked(value) {
  const s = typeof value === "string" ? value : "";
  return s.length >= 8 ? `ending ${s.slice(-4)}` : "on file";
}

/* One short line per record the item asked for. `entries` is everything
   that would be sent with it, so a Coverage can name its payer. */
export function recordNames(match) {
  const byUrl = new Map((match.entries || []).map((e) => [e.fullUrl, e.resource]));
  return (match.primary || []).flatMap(({ resource: r }) => {
    switch (r.resourceType) {
      case "Patient": {
        const n = (Array.isArray(r.name) && r.name[0]) || {};
        const given = Array.isArray(n.given) ? n.given[0] : "";
        const who = [given, n.family].filter(Boolean).join(" ") || "Your name";
        const lines = [r.birthDate ? `${who}, born ${day(r.birthDate)}` : who];
        // What else goes with it, named but not repeated here.
        const also = [
          Array.isArray(r.address) && r.address.length && "address",
          Array.isArray(r.telecom) && r.telecom.some((t) => t && t.system === "phone") && "phone",
          Array.isArray(r.telecom) && r.telecom.some((t) => t && t.system === "email") && "email",
        ].filter(Boolean);
        if (also.length) lines.push(`Your ${also.join(", ")}`);
        return lines;
      }
      case "Coverage": {
        const payerRef = Array.isArray(r.payor) && r.payor[0] && r.payor[0].reference;
        const payer = byUrl.get(payerRef);
        const insurer = (payer && payer.resourceType === "Organization" &&
          typeof payer.name === "string" && payer.name) || "Your insurer";
        return `${insurer}, member ID ${masked(r.subscriberId)}`;
      }
      case "Immunization": {
        const what = labelOf(r.vaccineCode) || "A vaccine";
        return r.occurrenceDateTime ? `${what}, ${day(r.occurrenceDateTime)}` : what;
      }
      case "MedicationRequest":
      case "MedicationStatement":
        return labelOf(r.medicationCodeableConcept) || "A medicine";
      case "AllergyIntolerance":
        return labelOf(r.code) || "An allergy";
      case "Condition":
        return labelOf(r.code) || "A health problem";
      default:
        return "A record";
    }
  });
}
