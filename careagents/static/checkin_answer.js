/* SMART Health Check-in: what the wallet can answer, and the answer itself.
   No page, no network: checkin_wallet.js does those. Kept apart so the
   rules below can be exercised on their own (e2e checkin-wallet spec).

   The rules, per request item:
     unsupported  the library said so, or it is not a FHIR selection this
                  wallet can send (a form, say: PHQ-2 is later work)
     unavailable  nothing on file claims the requested profile. Nothing is
                  stamped with a claim to make it fit, and "no known
                  allergies" is never sent: a record coded that way is not
                  an allergy to share, so the item reads unavailable
     declined     there is something, and the person did not choose to share
     fulfilled    the person chose to share it: one Bundle, fhirVersion 4.0.1
*/
import { selectEntries, selects } from "./vendor/smart-health-checkin-client/0.4.4/wallet.js";

const FHIR_JSON = "application/fhir+json";
const SNOMED = ["http://snomed.info/sct", "urn:oid:2.16.840.1.113883.6.96"];

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

// Words that state an absence rather than name an allergen: "No known
// allergies", "NKDA", "None", "Nil known". Feeds write these in code.text or
// as a local code's display. Read only to hold a record back, never to show
// it, so a false match costs an "unavailable", never a disclosure.
const ABSENCE_WORDING = new RegExp(
  "^(?:none|nil)(?: known)?$" +
  "|^no (?:known )?(?:\\w+ )?allerg" +
  "|\\bno known\\b" +
  "|\\bnk[a-z]?a\\b");

function readsAsAbsence(text) {
  if (typeof text !== "string") return false;
  const words = text.toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
  return words !== "" && ABSENCE_WORDING.test(words);
}

function isNoKnownAllergy(resource) {
  if (!resource || resource.resourceType !== "AllergyIntolerance") return false;
  const code = resource.code || {};
  const codings = Array.isArray(code.coding) ? code.coding : [];
  return readsAsAbsence(code.text) || codings.some((c) => c && (
    (SNOMED.includes(c.system) && NKA_CODES.includes(String(c.code))) ||
    (c.system === LOINC && NKA_LOINC.includes(String(c.code))) ||
    readsAsAbsence(c.display)));
}

// HL7 v3 ActReason HTEST: every Bundle this wallet sends says it is test
// data, whatever its records carry. True while it answers only from the
// synthetic test patient; the real-record export must drop it.
const HTEST = { system: "http://terminology.hl7.org/CodeSystem/v3-ActReason",
  code: "HTEST", display: "test health data" };

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

/* For each item: { item, unsupported, entries, primary }. `entries` is what
   would be sent; `primary` is the part of it the item asked for, which is
   what the person is shown (a payer or prescriber rides along unnamed). */
export function matchItems(request, unsupportedItems, records) {
  const unsupported = new Set((unsupportedItems || []).map((u) => u.id));
  return request.items.map((item) => {
    const content = item.content || {};
    if (unsupported.has(item.id) || content.kind !== "selection.fhir" ||
        !(item.accept || []).includes(FHIR_JSON)) {
      return { item, unsupported: true, entries: [], primary: [] };
    }
    const shareable = records.entries.filter((e) => !isNoKnownAllergy(e.resource));
    // Only what the item asks for makes it answerable: a payer that would
    // ride along by reference is not, on its own, something to share.
    const primary = shareable.filter((e) => selects(content, e.resource));
    if (!primary.length) return { item, unsupported: false, entries: [], primary: [] };
    // The Patient has an item of its own; elsewhere it is not pulled along.
    const asksForPatient = primary.some((e) => e.fullUrl === records.patientFullUrl);
    const exclude = asksForPatient || !records.patientFullUrl ? [] : [records.patientFullUrl];
    return { item, unsupported: false, primary,
      entries: selectEntries(content, shareable, { exclude }) };
  });
}

/* The SMART response. `share` maps item id -> true for what the person
   chose to share; anything else is not shared. */
export function buildResponse(request, matches, share) {
  const artifacts = [];
  const requestStatus = [];
  for (const m of matches) {
    const id = m.item.id;
    if (m.unsupported) { requestStatus.push({ item: id, status: "unsupported" }); continue; }
    if (!m.entries.length) { requestStatus.push({ item: id, status: "unavailable" }); continue; }
    if (!share || share[id] !== true) { requestStatus.push({ item: id, status: "declined" }); continue; }
    artifacts.push({
      id: `fhir-${id}`,
      mediaType: FHIR_JSON,
      fhirVersion: "4.0.1",
      fulfills: [id],
      value: {
        resourceType: "Bundle",
        meta: { tag: [{ ...HTEST }] },
        type: "collection",
        entry: m.entries.map((e) => ({ fullUrl: e.fullUrl, resource: e.resource })),
      },
    });
    requestStatus.push({ item: id, status: "fulfilled" });
  }
  return {
    type: "smart-health-checkin-response",
    version: "1",
    requestId: request.id,
    artifacts,
    requestStatus,
  };
}

/* --- What the person is shown, in plain words ---------------------------

   Never a FHIR type name, never `text` or a coding's `display`: those are
   the fields a real feed puts other people's names in. A record is named by
   its code, from LABELS, or by structured fields of the person's own
   (their name, the payer Organization's name, a masked member id). The
   real-record path will name records from the engine's output, whose
   labels come from r6/terminology.py; LABELS mirrors that table for the
   test patient's codes (tests/test_checkin_wallet.py holds them equal). */

const RXNORM = "http://www.nlm.nih.gov/research/umls/rxnorm";
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

/* Our heading for an item, from what it matched; the website's own title
   only when there is nothing to name it by. */
export function itemHeading(match) {
  const types = new Set((match.primary || []).map((e) => e.resource.resourceType));
  if (types.size === 1) {
    const h = HEADINGS[[...types][0]];
    if (h) return h;
  }
  return match.item.title || match.item.id;
}

function labelOf(concept) {
  const codings = (concept && Array.isArray(concept.coding)) ? concept.coding : [];
  for (const c of codings) {
    if (!c) continue;
    const system = SNOMED.includes(c.system) ? SNOMED[0] : c.system;
    const label = LABELS[`${system}|${String(c.code)}`];
    if (label) return label;
  }
  return null;
}

const day = (s) => (typeof s === "string" ? s.slice(0, 10) : "");

function masked(value) {
  const s = String(value || "");
  return s.length > 4 ? `ending ${s.slice(-4)}` : "on file";
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
