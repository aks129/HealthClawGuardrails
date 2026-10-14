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

// SNOMED 716186003 "no known allergy" and its descendants. The same set as
// careagents/agent.py _NKA_CODES; tests/test_checkin_wallet.py holds them equal.
export const NKA_CODES = [
  "716186003", "409137002", "428197003", "428607008",
  "429625007", "716220001", "1003774007",
];

function isNoKnownAllergy(resource) {
  if (!resource || resource.resourceType !== "AllergyIntolerance") return false;
  const codings = (resource.code && resource.code.coding) || [];
  return codings.some((c) => c && SNOMED.includes(c.system) &&
    NKA_CODES.includes(String(c.code)));
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

/* For each item: { item, unsupported: bool, entries: [...] }. */
export function matchItems(request, unsupportedItems, records) {
  const unsupported = new Set((unsupportedItems || []).map((u) => u.id));
  return request.items.map((item) => {
    const content = item.content || {};
    if (unsupported.has(item.id) || content.kind !== "selection.fhir" ||
        !(item.accept || []).includes(FHIR_JSON)) {
      return { item, unsupported: true, entries: [] };
    }
    const shareable = records.entries.filter((e) => !isNoKnownAllergy(e.resource));
    // Only what the item asks for makes it answerable: a payer that would
    // ride along by reference is not, on its own, something to share.
    const primary = shareable.filter((e) => selects(content, e.resource));
    if (!primary.length) return { item, unsupported: false, entries: [] };
    // The Patient has an item of its own; elsewhere it is not pulled along.
    const asksForPatient = primary.some((e) => e.fullUrl === records.patientFullUrl);
    const exclude = asksForPatient || !records.patientFullUrl ? [] : [records.patientFullUrl];
    return { item, unsupported: false, entries: selectEntries(content, shareable, { exclude }) };
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
