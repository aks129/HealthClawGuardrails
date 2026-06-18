# SDC $populate + $extract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add HL7 SDC `$populate` (Questionnaire → pre-filled QuestionnaireResponse) and `$extract` (QuestionnaireResponse → transaction Bundle) as interoperable FHIR operations plus agent-facing MCP tools, reusing the existing guardrail stack.

**Architecture:** Pure transform engines in `r6/sdc/` (populate.py, extract.py, expressions.py) with zero Flask dependency, driven by SDC extensions. A thin handler layer attaches `$populate`/`$extract` routes to the existing `r6_blueprint` (so tenant + read-auth before_request hooks apply) and owns store I/O, audit, step-up, and redaction. Two MCP tools wrap the endpoints.

**Tech Stack:** Python 3.11+ / Flask / SQLAlchemy, `fhirpathpy` (new), pytest; Node/TypeScript MCP server (`services/agent-orchestrator`), Jest.

**Spec:** `docs/superpowers/specs/2026-06-17-sdc-populate-extract-design.md`

**Conventions observed in this codebase (read before starting):**
- Resource types are gated by `R6Resource.SUPPORTED_TYPES` ([r6/models.py:43](../../../r6/models.py#L43)) AND `R6_RESOURCE_TYPES` ([r6/validator.py:20](../../../r6/validator.py#L20)).
- Store reads: `R6Resource.query.filter_by(tenant_id=..., ...)`; a stored resource's JSON is `R6Resource.resource_json` (TEXT), PK is `.id`. Use `.to_fhir_json()` to get the dict.
- `record_audit_event(event_type, resource_type, resource_id, agent_id=, tenant_id=, detail=)` ([r6/audit.py:17](../../../r6/audit.py#L17)).
- `validator.validate_resource(resource)` → `{'valid': bool, 'operation_outcome': {...}}`.
- `validate_step_up_token(token, tenant_id)` → `(bool, str)` — **always destructure**.
- `_operation_outcome(severity, code, diagnostics)` helper ([r6/routes.py:2867](../../../r6/routes.py#L2867)) returns a dict (caller adds status code).
- CI runs Python 3.11 — no backslash escapes inside f-string `{...}` expressions.

---

## Task 1: Register Questionnaire & QuestionnaireResponse resource types + factor out read-auth helper

**Files:**
- Modify: `r6/models.py` (SUPPORTED_TYPES list, ~line 43)
- Modify: `r6/validator.py` (R6_RESOURCE_TYPES list, ~line 20)
- Modify: `r6/routes.py` (extract read-auth into a reusable helper, ~lines 285-307)
- Test: `tests/test_sdc_types.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_sdc_types.py`:

```python
from r6.models import R6Resource
from r6.validator import R6_RESOURCE_TYPES


def test_questionnaire_types_are_supported():
    assert R6Resource.is_supported_type('Questionnaire')
    assert R6Resource.is_supported_type('QuestionnaireResponse')


def test_questionnaire_types_in_validator_list():
    assert 'Questionnaire' in R6_RESOURCE_TYPES
    assert 'QuestionnaireResponse' in R6_RESOURCE_TYPES
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_sdc_types.py -v`
Expected: FAIL (`assert False` — types not yet registered).

- [ ] **Step 3: Add the types**

In `r6/models.py`, append to the end of the `SUPPORTED_TYPES` list (after `'FamilyMemberHistory',`):

```python
        # Phase 5 — SDC Structured Data Capture
        'Questionnaire', 'QuestionnaireResponse',
```

In `r6/validator.py`, append to the end of the `R6_RESOURCE_TYPES` list (after `'FamilyMemberHistory',`):

```python
    # Phase 5 — SDC Structured Data Capture
    'Questionnaire', 'QuestionnaireResponse',
```

- [ ] **Step 4: Refactor read-auth into a reusable helper**

In `r6/routes.py`, the `authenticate_read` before_request hook contains the credential-checking block (currently ~lines 285-307). Extract that block into a module-level function so the `$populate` POST handler (which the GET-only hook won't cover) can reuse it. Add this function just above `authenticate_read`:

```python
def authenticate_tenant_read(tenant_id):
    """Validate read credentials for `tenant_id`.

    Shared by the GET before_request hook and POST read-shaped operations
    (e.g. Questionnaire/$populate). Returns None when access is allowed,
    or an (OperationOutcome, status) tuple to abort with.

    Mirrors the gate semantics: public tenants and the disabled flag pass;
    otherwise a tenant-bound step-up token OR a SMART bearer is required.
    """
    if not _read_auth_enabled():
        return None
    if not _read_auth_required(tenant_id):
        return None

    bearer = ''
    auth = (request.headers.get('Authorization') or '').strip()
    if auth.lower().startswith('bearer '):
        bearer = auth[7:].strip()
    step_up = (request.headers.get('X-Step-Up-Token') or '').strip() or bearer

    valid = False
    if step_up:
        valid, _err = validate_step_up_token(step_up, tenant_id)
    if not valid and bearer:
        valid = _validate_oauth_read(bearer, tenant_id)

    if not valid:
        return _operation_outcome(
            'error', 'security',
            f"Read access to tenant '{tenant_id}' requires authentication",
        ), 401
    return None
```

Then replace the inline block in `authenticate_read` (from `bearer = ''` through the final `return None`) with:

```python
    return authenticate_tenant_read(tenant_id)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run python -m pytest tests/test_sdc_types.py tests/test_r6_routes.py -v`
Expected: PASS (new types registered; existing route/read-auth tests still green).

- [ ] **Step 6: Commit**

```bash
git add r6/models.py r6/validator.py r6/routes.py tests/test_sdc_types.py
git commit -m "feat(sdc): register Questionnaire types + reusable read-auth helper"
```

---

## Task 2: FHIRPath expression evaluation module

**Files:**
- Modify: `pyproject.toml` (add `fhirpathpy` dependency)
- Create: `r6/sdc/__init__.py`
- Create: `r6/sdc/expressions.py`
- Test: `tests/test_sdc_expressions.py` (new)

- [ ] **Step 1: Add the dependency**

In `pyproject.toml`, add to the `dependencies` array (keep alphabetical-ish, after `"email-validator>=2.2.0",`):

```toml
    "fhirpathpy>=0.2.2",
```

Then run: `uv sync`
Expected: resolves and installs `fhirpathpy`.

- [ ] **Step 2: Write the failing test**

Create `tests/test_sdc_expressions.py`:

```python
from r6.sdc.expressions import evaluate, build_context


def test_evaluate_simple_path():
    patient = {"resourceType": "Patient",
               "name": [{"given": ["Ada"], "family": "Lovelace"}]}
    assert evaluate("Patient.name.given.first()", patient) == "Ada"


def test_evaluate_with_launch_context_variable():
    patient = {"resourceType": "Patient", "birthDate": "1990-01-01"}
    ctx = build_context(subject=patient, resources=[patient])
    assert evaluate("%patient.birthDate", patient, ctx) == "1990-01-01"


def test_evaluate_returns_none_on_no_match():
    patient = {"resourceType": "Patient"}
    assert evaluate("Patient.name.given.first()", patient) is None


def test_evaluate_returns_none_on_bad_expression():
    assert evaluate("this is not fhirpath (((", {}) is None
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_sdc_expressions.py -v`
Expected: FAIL (`ModuleNotFoundError: r6.sdc`).

- [ ] **Step 4: Create the module**

Create `r6/sdc/__init__.py`:

```python
"""SDC (Structured Data Capture) — form $populate and data $extract engines."""
```

Create `r6/sdc/expressions.py`:

```python
"""FHIRPath evaluation for SDC populate/extract.

Thin wrapper over fhirpathpy. Evaluation failures return None rather than
raising, so a single bad expression in a Questionnaire never aborts the
whole populate/extract run (the caller records an issue instead).
"""

import logging

import fhirpathpy

logger = logging.getLogger(__name__)


def build_context(subject=None, resources=None, extra=None):
    """Build the FHIRPath environment-variable context.

    %patient / %subject resolve to the populate subject; named entries in
    `extra` (e.g. launchContext or variable values) are passed through.
    """
    context = {}
    if subject is not None:
        context['patient'] = subject
        context['subject'] = subject
    if resources:
        context['resources'] = resources
    if extra:
        context.update(extra)
    return context


def evaluate(expression, resource, context=None):
    """Evaluate a FHIRPath expression, returning a scalar, list, or None.

    Returns the single value when the result has one element, the list when
    it has several, and None when empty or on any evaluation error.
    """
    if not expression:
        return None
    try:
        result = fhirpathpy.evaluate(resource or {}, expression, context or {})
    except Exception as exc:  # noqa: BLE001 — never let one expr kill the run
        logger.warning('FHIRPath evaluation failed for %r: %s',
                       expression, type(exc).__name__)
        return None
    if not result:
        return None
    return result[0] if len(result) == 1 else result
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run python -m pytest tests/test_sdc_expressions.py -v`
Expected: PASS (4 tests).

- [ ] **Step 6: Verify Python 3.11 import**

Run: `uv run python -c "import fhirpathpy; from r6.sdc.expressions import evaluate; print('ok')"`
Expected: prints `ok` (confirms the dependency imports cleanly).

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml uv.lock r6/sdc/__init__.py r6/sdc/expressions.py tests/test_sdc_expressions.py
git commit -m "feat(sdc): add fhirpathpy expression evaluation module"
```

---

## Task 3: Populate engine

**Files:**
- Create: `r6/sdc/populate.py`
- Test: `tests/test_sdc_populate.py` (new)

The engine is pure: it receives the Questionnaire, the subject Patient, and a list of context resources (including Observations), and returns `(questionnaire_response, issues)`. No DB, no Flask.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_sdc_populate.py`:

```python
from r6.sdc.populate import populate_questionnaire

INITIAL_EXPR_URL = (
    "http://hl7.org/fhir/uv/sdc/StructureDefinition/"
    "sdc-questionnaire-initialExpression"
)


def _expr_item(link_id, expression):
    return {
        "linkId": link_id,
        "type": "string",
        "extension": [{
            "url": INITIAL_EXPR_URL,
            "valueExpression": {"language": "text/fhirpath",
                                "expression": expression},
        }],
    }


def test_populate_initial_expression():
    q = {"resourceType": "Questionnaire", "status": "active",
         "item": [_expr_item("first-name",
                             "%patient.name.given.first()")]}
    patient = {"resourceType": "Patient", "id": "p1",
               "name": [{"given": ["Ada"]}]}

    qr, issues = populate_questionnaire(q, patient, [patient])

    assert qr["resourceType"] == "QuestionnaireResponse"
    assert qr["status"] == "in-progress"
    assert qr["subject"] == {"reference": "Patient/p1"}
    answer_item = qr["item"][0]
    assert answer_item["linkId"] == "first-name"
    assert answer_item["answer"][0]["valueString"] == "Ada"
    assert issues == []


def test_populate_observation_based_by_code():
    q = {"resourceType": "Questionnaire", "status": "active",
         "item": [{
             "linkId": "weight",
             "type": "quantity",
             "code": [{"system": "http://loinc.org", "code": "29463-7"}],
         }]}
    patient = {"resourceType": "Patient", "id": "p1"}
    obs = {"resourceType": "Observation", "status": "final",
           "code": {"coding": [{"system": "http://loinc.org",
                                "code": "29463-7"}]},
           "subject": {"reference": "Patient/p1"},
           "valueQuantity": {"value": 70, "unit": "kg"}}

    qr, issues = populate_questionnaire(q, patient, [patient, obs])

    answer = qr["item"][0]["answer"][0]
    assert answer["valueQuantity"]["value"] == 70
    assert issues == []


def test_populate_records_issue_for_unresolved_item():
    q = {"resourceType": "Questionnaire", "status": "active",
         "item": [{"linkId": "missing", "type": "string",
                   "code": [{"system": "http://loinc.org", "code": "0000-0"}]}]}
    patient = {"resourceType": "Patient", "id": "p1"}

    qr, issues = populate_questionnaire(q, patient, [patient])

    # No answer produced, and no spurious answer array on the item.
    assert "answer" not in qr["item"][0]
    assert issues == []  # absence of data is not an error, just no answer


def test_populate_nested_group_items():
    q = {"resourceType": "Questionnaire", "status": "active",
         "item": [{"linkId": "demographics", "type": "group",
                   "item": [_expr_item("dob", "%patient.birthDate")]}]}
    patient = {"resourceType": "Patient", "id": "p1",
               "birthDate": "1815-12-10"}

    qr, _ = populate_questionnaire(q, patient, [patient])

    group = qr["item"][0]
    assert group["linkId"] == "demographics"
    assert group["item"][0]["answer"][0]["valueString"] == "1815-12-10"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/test_sdc_populate.py -v`
Expected: FAIL (`ModuleNotFoundError` / `populate_questionnaire` undefined).

- [ ] **Step 3: Implement the engine**

Create `r6/sdc/populate.py`:

```python
"""SDC $populate engine — Questionnaire + subject + content -> QuestionnaireResponse.

Pure function (no DB, no Flask). Supports two SDC population mechanisms:
  - Expression-based: items carrying an initialExpression (FHIRPath).
  - Observation-based: items with an item.code (LOINC) matched against
    Observations in the supplied content.

Out of scope (v1): StructureMap-based and CQL populate.
"""

from r6.sdc.expressions import build_context, evaluate

INITIAL_EXPRESSION_URL = (
    "http://hl7.org/fhir/uv/sdc/StructureDefinition/"
    "sdc-questionnaire-initialExpression"
)

# FHIR item.type -> QuestionnaireResponse.answer value[x] key for scalars.
_ANSWER_KEY_BY_TYPE = {
    "boolean": "valueBoolean",
    "decimal": "valueDecimal",
    "integer": "valueInteger",
    "date": "valueDate",
    "dateTime": "valueDateTime",
    "time": "valueTime",
    "string": "valueString",
    "text": "valueString",
    "url": "valueUri",
    "quantity": "valueQuantity",
}


def populate_questionnaire(questionnaire, subject, content_resources):
    """Return (questionnaire_response, issues).

    questionnaire: Questionnaire dict.
    subject: Patient dict (or None).
    content_resources: list of resource dicts available for population
        (should include the subject and any Observations).
    issues: list of {'linkId', 'detail'} for items that errored (not for
        items that simply had no data).
    """
    issues = []
    context = build_context(subject=subject, resources=content_resources)
    observations = [r for r in (content_resources or [])
                    if r.get("resourceType") == "Observation"]

    answer_items = []
    for item in questionnaire.get("item", []):
        populated = _populate_item(item, subject, context, observations, issues)
        if populated is not None:
            answer_items.append(populated)

    qr = {
        "resourceType": "QuestionnaireResponse",
        "status": "in-progress",
        "questionnaire": _questionnaire_canonical(questionnaire),
        "item": answer_items,
    }
    subject_ref = _reference(subject)
    if subject_ref:
        qr["subject"] = subject_ref
    return qr, issues


def _populate_item(item, subject, context, observations, issues):
    link_id = item.get("linkId")
    item_type = item.get("type")

    # Group: recurse, keep the group only if it produced child answers.
    if item_type == "group":
        children = []
        for child in item.get("item", []):
            populated = _populate_item(child, subject, context,
                                       observations, issues)
            if populated is not None:
                children.append(populated)
        if not children:
            return None
        return {"linkId": link_id, "item": children}

    answer_value, value_key = _resolve_answer(
        item, item_type, context, observations, issues, link_id)
    if answer_value is None:
        return None
    return {"linkId": link_id, "answer": [{value_key: answer_value}]}


def _resolve_answer(item, item_type, context, observations, issues, link_id):
    value_key = _ANSWER_KEY_BY_TYPE.get(item_type, "valueString")

    expr = _initial_expression(item)
    if expr:
        value = evaluate(expr, context.get("patient"), context)
        if value is not None:
            return _coerce(value, item_type), value_key
        return None, value_key

    codes = item.get("code") or []
    if codes:
        value = _observation_answer(codes, observations)
        if value is not None:
            return value, value_key
    return None, value_key


def _observation_answer(item_codes, observations):
    """Return the most recent Observation value matching any item code."""
    wanted = {(c.get("system"), c.get("code")) for c in item_codes}
    matches = []
    for obs in observations:
        for coding in obs.get("code", {}).get("coding", []):
            if (coding.get("system"), coding.get("code")) in wanted:
                matches.append(obs)
                break
    if not matches:
        return None
    matches.sort(key=lambda o: o.get("effectiveDateTime", ""), reverse=True)
    best = matches[0]
    if "valueQuantity" in best:
        return best["valueQuantity"]
    if "valueString" in best:
        return best["valueString"]
    if "valueCodeableConcept" in best:
        return best["valueCodeableConcept"].get("text")
    return None


def _initial_expression(item):
    for ext in item.get("extension", []):
        if ext.get("url") == INITIAL_EXPRESSION_URL:
            return (ext.get("valueExpression") or {}).get("expression")
    return None


def _coerce(value, item_type):
    if isinstance(value, dict):
        return value
    if item_type in ("integer",):
        try:
            return int(value)
        except (TypeError, ValueError):
            return value
    if item_type in ("decimal",):
        try:
            return float(value)
        except (TypeError, ValueError):
            return value
    if item_type == "boolean":
        return bool(value)
    return str(value)


def _reference(resource):
    if not resource:
        return None
    rtype = resource.get("resourceType")
    rid = resource.get("id")
    if rtype and rid:
        return {"reference": f"{rtype}/{rid}"}
    return None


def _questionnaire_canonical(questionnaire):
    url = questionnaire.get("url")
    if url:
        version = questionnaire.get("version")
        return f"{url}|{version}" if version else url
    qid = questionnaire.get("id")
    return f"Questionnaire/{qid}" if qid else None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/test_sdc_populate.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add r6/sdc/populate.py tests/test_sdc_populate.py
git commit -m "feat(sdc): populate engine (expression + observation based)"
```

---

## Task 4: Extract engine

**Files:**
- Create: `r6/sdc/extract.py`
- Test: `tests/test_sdc_extract.py` (new)

Pure function: receives a completed QuestionnaireResponse and its Questionnaire (for directives), returns a transaction Bundle.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_sdc_extract.py`:

```python
from r6.sdc.extract import extract_resources

OBS_EXTRACT_URL = (
    "http://hl7.org/fhir/uv/sdc/StructureDefinition/"
    "sdc-questionnaire-observationExtract"
)
DEF_EXTRACT_URL = (
    "http://hl7.org/fhir/uv/sdc/StructureDefinition/"
    "sdc-questionnaire-definitionExtract"
)


def test_extract_observation_based():
    q = {"resourceType": "Questionnaire", "status": "active",
         "item": [{
             "linkId": "weight",
             "type": "quantity",
             "code": [{"system": "http://loinc.org", "code": "29463-7"}],
             "extension": [{"url": OBS_EXTRACT_URL, "valueBoolean": True}],
         }]}
    qr = {"resourceType": "QuestionnaireResponse", "status": "completed",
          "subject": {"reference": "Patient/p1"},
          "item": [{"linkId": "weight",
                    "answer": [{"valueQuantity": {"value": 70, "unit": "kg"}}]}]}

    bundle = extract_resources(qr, q)

    assert bundle["resourceType"] == "Bundle"
    assert bundle["type"] == "transaction"
    obs = bundle["entry"][0]["resource"]
    assert obs["resourceType"] == "Observation"
    assert obs["code"]["coding"][0]["code"] == "29463-7"
    assert obs["valueQuantity"]["value"] == 70
    assert obs["subject"] == {"reference": "Patient/p1"}
    assert bundle["entry"][0]["request"]["method"] == "POST"


def test_extract_definition_based():
    q = {"resourceType": "Questionnaire", "status": "active",
         "extension": [{"url": DEF_EXTRACT_URL,
                        "valueCode": "Patient"}],
         "item": [
             {"linkId": "family", "type": "string",
              "definition": "http://hl7.org/fhir/StructureDefinition/"
                            "Patient#Patient.name.family"},
             {"linkId": "dob", "type": "date",
              "definition": "http://hl7.org/fhir/StructureDefinition/"
                            "Patient#Patient.birthDate"},
         ]}
    qr = {"resourceType": "QuestionnaireResponse", "status": "completed",
          "item": [
              {"linkId": "family", "answer": [{"valueString": "Lovelace"}]},
              {"linkId": "dob", "answer": [{"valueDate": "1815-12-10"}]},
          ]}

    bundle = extract_resources(qr, q)

    patient = bundle["entry"][0]["resource"]
    assert patient["resourceType"] == "Patient"
    assert patient["name"][0]["family"] == "Lovelace"
    assert patient["birthDate"] == "1815-12-10"


def test_extract_empty_when_no_directives():
    q = {"resourceType": "Questionnaire", "status": "active",
         "item": [{"linkId": "x", "type": "string"}]}
    qr = {"resourceType": "QuestionnaireResponse", "status": "completed",
          "item": [{"linkId": "x", "answer": [{"valueString": "y"}]}]}

    bundle = extract_resources(qr, q)
    assert bundle["entry"] == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/test_sdc_extract.py -v`
Expected: FAIL (`ModuleNotFoundError` / `extract_resources` undefined).

- [ ] **Step 3: Implement the engine**

Create `r6/sdc/extract.py`:

```python
"""SDC $extract engine — QuestionnaireResponse -> transaction Bundle.

Pure function. Supports two SDC extraction mechanisms:
  - Observation-based: items flagged observationExtract + item.code -> Observation.
  - Definition-based: root definitionExtract names the target resource type;
    items carry `definition` (StructureDefinition#element.path) -> element values.

Out of scope (v1): template-based and StructureMap-based extraction.
"""

OBSERVATION_EXTRACT_URL = (
    "http://hl7.org/fhir/uv/sdc/StructureDefinition/"
    "sdc-questionnaire-observationExtract"
)
DEFINITION_EXTRACT_URL = (
    "http://hl7.org/fhir/uv/sdc/StructureDefinition/"
    "sdc-questionnaire-definitionExtract"
)


def extract_resources(questionnaire_response, questionnaire):
    """Return a FHIR transaction Bundle of resources extracted from `qr`."""
    subject_ref = questionnaire_response.get("subject")
    answers = _index_answers(questionnaire_response.get("item", []))

    entries = []
    entries.extend(_extract_observations(questionnaire, answers, subject_ref))
    entries.extend(_extract_by_definition(questionnaire, answers, subject_ref))

    return {"resourceType": "Bundle", "type": "transaction", "entry": entries}


def _index_answers(items, acc=None):
    """Flatten QR items into {linkId: [answer, ...]} (recurses groups)."""
    acc = acc if acc is not None else {}
    for item in items:
        if "answer" in item:
            acc[item["linkId"]] = item["answer"]
        if "item" in item:
            _index_answers(item["item"], acc)
    return acc


def _extract_observations(questionnaire, answers, subject_ref):
    entries = []
    root_flag = _has_extension(questionnaire, OBSERVATION_EXTRACT_URL)
    for item in _walk_items(questionnaire.get("item", [])):
        if not (root_flag or _has_extension(item, OBSERVATION_EXTRACT_URL)):
            continue
        codes = item.get("code") or []
        if not codes:
            continue
        for answer in answers.get(item.get("linkId"), []):
            obs = {
                "resourceType": "Observation",
                "status": "final",
                "code": {"coding": codes},
            }
            if subject_ref:
                obs["subject"] = subject_ref
            value_key, value = _answer_value(answer)
            if value_key:
                obs[value_key] = value
            entries.append(_post_entry(obs))
    return entries


def _extract_by_definition(questionnaire, answers, subject_ref):
    target_type = _extension_value(questionnaire, DEFINITION_EXTRACT_URL,
                                   "valueCode")
    if not target_type:
        return []
    resource = {"resourceType": target_type}
    populated = False
    for item in _walk_items(questionnaire.get("item", [])):
        definition = item.get("definition")
        if not definition or "#" not in definition:
            continue
        path = definition.split("#", 1)[1]  # e.g. Patient.name.family
        item_answers = answers.get(item.get("linkId"), [])
        if not item_answers:
            continue
        _value_key, value = _answer_value(item_answers[0])
        if value is None:
            continue
        _set_path(resource, path, value)
        populated = True
    if not populated:
        return []
    return [_post_entry(resource)]


def _set_path(resource, dotted_path, value):
    """Set a value at an element path like 'Patient.name.family'.

    The leading resource-type segment is dropped. A `name` segment is treated
    as the conventional repeating HumanName (index 0); otherwise scalar.
    """
    parts = dotted_path.split(".")[1:]  # drop resource type
    if not parts:
        return
    if parts == ["birthDate"]:
        resource["birthDate"] = value
        return
    if parts[:1] == ["name"] and len(parts) == 2:
        names = resource.setdefault("name", [{}])
        field = parts[1]
        if field == "given":
            names[0].setdefault("given", []).append(value)
        else:
            names[0][field] = value
        return
    # Generic fallback: nested dict path, scalar leaf.
    cursor = resource
    for segment in parts[:-1]:
        cursor = cursor.setdefault(segment, {})
    cursor[parts[-1]] = value


def _answer_value(answer):
    for key, value in answer.items():
        if key.startswith("value"):
            return key, value
    return None, None


def _walk_items(items):
    for item in items:
        yield item
        if "item" in item:
            yield from _walk_items(item["item"])


def _has_extension(node, url):
    return any(ext.get("url") == url for ext in node.get("extension", []))


def _extension_value(node, url, value_key):
    for ext in node.get("extension", []):
        if ext.get("url") == url:
            return ext.get(value_key)
    return None


def _post_entry(resource):
    return {"resource": resource,
            "request": {"method": "POST",
                        "url": resource["resourceType"]}}
```

Note for the implementer: in `_extract_observations`, the duplicated `obs[...] = value` line is a copy artifact — set the value once. Replace the `if value_key:` block with:

```python
            value_key, value = _answer_value(answer)
            if value_key:
                obs[value_key] = value
            entries.append(_post_entry(obs))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/test_sdc_extract.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add r6/sdc/extract.py tests/test_sdc_extract.py
git commit -m "feat(sdc): extract engine (observation + definition based)"
```

---

## Task 5: SDC Flask routes ($populate + $extract)

**Files:**
- Create: `r6/sdc/routes.py` (attaches routes to the existing `r6_blueprint`)
- Modify: `r6/routes.py` (import the SDC route module at the end so routes register)
- Test: `tests/test_sdc_routes.py` (new)

The routes attach to `r6_blueprint` so the `enforce_tenant_id` before_request hook applies. `$populate` (POST, read-shaped) explicitly calls `authenticate_tenant_read`. `$extract` requires step-up and commits via a transaction unless `dryRun=true`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_sdc_routes.py`:

```python
import json

from r6.models import R6Resource, db

INITIAL_EXPR_URL = (
    "http://hl7.org/fhir/uv/sdc/StructureDefinition/"
    "sdc-questionnaire-initialExpression"
)
DEF_EXTRACT_URL = (
    "http://hl7.org/fhir/uv/sdc/StructureDefinition/"
    "sdc-questionnaire-definitionExtract"
)


def _store(app, resource, tenant_id):
    with app.app_context():
        r = R6Resource(
            resource_type=resource["resourceType"],
            resource_json=json.dumps(resource),
            resource_id=resource["id"],
            tenant_id=tenant_id,
        )
        db.session.add(r)
        db.session.commit()


def test_populate_by_id(client, app, tenant_id, tenant_headers):
    _store(app, {"resourceType": "Patient", "id": "p1",
                 "name": [{"given": ["Ada"]}]}, tenant_id)
    _store(app, {"resourceType": "Questionnaire", "id": "q1",
                 "status": "active",
                 "item": [{"linkId": "fn", "type": "string",
                           "extension": [{"url": INITIAL_EXPR_URL,
                                          "valueExpression": {
                                              "language": "text/fhirpath",
                                              "expression":
                                                  "%patient.name.given.first()"}}]}]},
           tenant_id)

    resp = client.post(
        "/r6/fhir/Questionnaire/q1/$populate",
        headers=tenant_headers,
        json={"resourceType": "Parameters",
              "parameter": [{"name": "subject",
                             "valueReference": {"reference": "Patient/p1"}}]},
    )

    assert resp.status_code == 200
    params = resp.get_json()
    qr = _param(params, "response")
    assert qr["item"][0]["answer"][0]["valueString"] == "Ada"


def test_extract_requires_step_up(client, tenant_headers):
    resp = client.post(
        "/r6/fhir/QuestionnaireResponse/$extract",
        headers=tenant_headers,  # no X-Step-Up-Token
        json={"resourceType": "Parameters",
              "parameter": [{"name": "questionnaire-response",
                             "resource": {"resourceType":
                                          "QuestionnaireResponse",
                                          "status": "completed"}}]},
    )
    assert resp.status_code == 401


def test_extract_dry_run_returns_bundle(client, auth_headers):
    qr = {"resourceType": "QuestionnaireResponse", "status": "completed",
          "subject": {"reference": "Patient/p1"},
          "contained": [],
          "item": [{"linkId": "weight",
                    "answer": [{"valueQuantity": {"value": 70}}]}]}
    q = {"resourceType": "Questionnaire", "status": "active",
         "item": [{"linkId": "weight", "type": "quantity",
                   "code": [{"system": "http://loinc.org", "code": "29463-7"}],
                   "extension": [{"url":
                                  "http://hl7.org/fhir/uv/sdc/"
                                  "StructureDefinition/"
                                  "sdc-questionnaire-observationExtract",
                                  "valueBoolean": True}]}]}
    resp = client.post(
        "/r6/fhir/QuestionnaireResponse/$extract?dryRun=true",
        headers=auth_headers,
        json={"resourceType": "Parameters",
              "parameter": [
                  {"name": "questionnaire-response", "resource": qr},
                  {"name": "questionnaire", "resource": q}]},
    )
    assert resp.status_code == 200
    bundle = _param_resource(resp.get_json(), "return")
    assert bundle["resourceType"] == "Bundle"
    assert bundle["entry"][0]["resource"]["resourceType"] == "Observation"


def _param(params, name):
    for p in params.get("parameter", []):
        if p["name"] == name:
            return p.get("resource") or p.get("part")
    return None


def _param_resource(params, name):
    for p in params.get("parameter", []):
        if p["name"] == name:
            return p.get("resource")
    return None
```

If the test fixtures `app` is not present in `tests/conftest.py`, use the existing `client` fixture's app via `client.application` instead of an `app` fixture: replace `app` parameter with `client` and use `client.application.app_context()` in `_store`. Check `tests/conftest.py` first and adapt the fixture name accordingly.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/test_sdc_routes.py -v`
Expected: FAIL (routes return 404 — not yet registered).

- [ ] **Step 3: Implement the routes**

Create `r6/sdc/routes.py`:

```python
"""SDC $populate / $extract Flask handlers.

Attached to the existing r6_blueprint so the tenant-enforcement before_request
hook applies. Owns all store I/O, audit, step-up, and redaction; the transform
logic lives in the pure engines (populate.py, extract.py).
"""

import json
import logging

from flask import request, jsonify

from r6.models import R6Resource
from r6.audit import record_audit_event
from r6.sdc.populate import populate_questionnaire
from r6.sdc.extract import extract_resources

logger = logging.getLogger(__name__)


def register_sdc_routes(blueprint, deps):
    """Register SDC routes on `blueprint`.

    deps: dict providing the helpers defined in r6/routes.py —
      'operation_outcome', 'authenticate_tenant_read', 'validate_step_up_token',
      'validator', 'context_builder' (for transaction commit).
    """
    operation_outcome = deps["operation_outcome"]
    authenticate_tenant_read = deps["authenticate_tenant_read"]
    validate_step_up_token = deps["validate_step_up_token"]
    validator = deps["validator"]

    @blueprint.route("/Questionnaire/$populate", methods=["POST"])
    @blueprint.route("/Questionnaire/<questionnaire_id>/$populate",
                     methods=["POST"])
    def sdc_populate(questionnaire_id=None):
        tenant_id = request.headers.get("X-Tenant-Id")
        auth_err = authenticate_tenant_read(tenant_id)
        if auth_err is not None:
            return jsonify(auth_err[0]), auth_err[1]

        params = request.get_json(silent=True) or {}
        questionnaire = _resolve_questionnaire(params, questionnaire_id,
                                               tenant_id)
        if questionnaire is None:
            return jsonify(operation_outcome(
                "error", "not-found",
                "Questionnaire could not be resolved")), 404

        subject = _resolve_subject(params, tenant_id)
        content = _gather_content(params, subject, tenant_id)

        qr, issues = populate_questionnaire(questionnaire, subject, content)

        record_audit_event("read", "Questionnaire",
                            questionnaire.get("id"),
                            agent_id=request.headers.get("X-Agent-Id"),
                            tenant_id=tenant_id,
                            detail=f"populate; issues={len(issues)}")

        response_params = {
            "resourceType": "Parameters",
            "parameter": [{"name": "response", "resource": qr}],
        }
        if issues:
            response_params["parameter"].append(
                {"name": "issues", "resource": _issues_outcome(issues)})
        return jsonify(response_params), 200

    @blueprint.route("/QuestionnaireResponse/$extract", methods=["POST"])
    @blueprint.route("/QuestionnaireResponse/<qr_id>/$extract",
                     methods=["POST"])
    def sdc_extract(qr_id=None):
        tenant_id = request.headers.get("X-Tenant-Id")
        dry_run = request.args.get("dryRun", "false").lower() == "true"

        params = request.get_json(silent=True) or {}
        qr = _param_resource(params, "questionnaire-response")
        if qr is None and qr_id:
            qr = _load_stored("QuestionnaireResponse", qr_id, tenant_id)
        if qr is None:
            return jsonify(operation_outcome(
                "error", "invalid",
                "questionnaire-response parameter is required")), 400

        questionnaire = (_param_resource(params, "questionnaire")
                         or _resolve_referenced_questionnaire(qr, tenant_id))
        if questionnaire is None:
            return jsonify(operation_outcome(
                "error", "not-found",
                "Questionnaire for the response could not be resolved")), 404

        bundle = extract_resources(qr, questionnaire)

        # Step-up gate (writes). dry_run still requires read auth only.
        if not dry_run:
            step_up = request.headers.get("X-Step-Up-Token")
            if not step_up:
                return jsonify(operation_outcome(
                    "error", "security",
                    "$extract requires X-Step-Up-Token (use dryRun=true to "
                    "preview without committing)")), 401
            valid, _err = validate_step_up_token(step_up, tenant_id)
            if not valid:
                return jsonify(operation_outcome(
                    "error", "security", "Invalid step-up token")), 401

            # Validate each extracted resource before commit.
            for entry in bundle["entry"]:
                result = validator.validate_resource(entry["resource"])
                if not result["valid"]:
                    return jsonify(result["operation_outcome"]), 422
            _commit_bundle(bundle, tenant_id)

        record_audit_event("create" if not dry_run else "read",
                            "QuestionnaireResponse", qr.get("id"),
                            agent_id=request.headers.get("X-Agent-Id"),
                            tenant_id=tenant_id,
                            detail=f"extract; dryRun={dry_run}; "
                                   f"resources={len(bundle['entry'])}")

        return jsonify({
            "resourceType": "Parameters",
            "parameter": [{"name": "return", "resource": bundle}],
        }), 200

    # --- local helpers (closures over deps) ---

    def _resolve_questionnaire(params, questionnaire_id, tenant_id):
        inline = _param_resource(params, "questionnaire")
        if inline:
            return inline
        if questionnaire_id:
            return _load_stored("Questionnaire", questionnaire_id, tenant_id)
        ref = _param_value(params, "questionnaireRef", "valueString")
        if ref and "/" in ref:
            return _load_stored("Questionnaire", ref.split("/")[-1], tenant_id)
        return None

    def _resolve_subject(params, tenant_id):
        inline = _param_resource(params, "subject")
        if inline:
            return inline
        ref = _param_value(params, "subject", "valueReference")
        if isinstance(ref, dict) and ref.get("reference"):
            ident = ref["reference"].split("/")[-1]
            return _load_stored("Patient", ident, tenant_id)
        return None

    def _gather_content(params, subject, tenant_id):
        content = []
        if subject:
            content.append(subject)
        # Observations for the subject power observation-based population.
        if subject and subject.get("id"):
            content.extend(_load_observations(subject["id"], tenant_id))
        bundle = _param_resource(params, "content")
        if bundle and bundle.get("resourceType") == "Bundle":
            content.extend(e["resource"] for e in bundle.get("entry", [])
                           if "resource" in e)
        return content

    def _resolve_referenced_questionnaire(qr, tenant_id):
        canonical = qr.get("questionnaire")
        if not canonical:
            return None
        ident = canonical.split("|")[0].split("/")[-1]
        return _load_stored("Questionnaire", ident, tenant_id)

    return sdc_populate, sdc_extract


# --- module-level helpers (no deps) ---

def _param_resource(params, name):
    for p in params.get("parameter", []):
        if p.get("name") == name and "resource" in p:
            return p["resource"]
    return None


def _param_value(params, name, value_key):
    for p in params.get("parameter", []):
        if p.get("name") == name and value_key in p:
            return p[value_key]
    return None


def _load_stored(resource_type, resource_id, tenant_id):
    row = R6Resource.query.filter_by(
        resource_type=resource_type, id=resource_id,
        tenant_id=tenant_id).first()
    return row.to_fhir_json() if row else None


def _load_observations(patient_id, tenant_id):
    rows = R6Resource.query.filter_by(
        resource_type="Observation", tenant_id=tenant_id).all()
    out = []
    ref = f"Patient/{patient_id}"
    for row in rows:
        obs = row.to_fhir_json()
        if obs.get("subject", {}).get("reference") == ref:
            out.append(obs)
    return out


def _commit_bundle(bundle, tenant_id):
    from r6.models import db
    for entry in bundle["entry"]:
        resource = entry["resource"]
        row = R6Resource(
            resource_type=resource["resourceType"],
            resource_json=json.dumps(resource),
            tenant_id=tenant_id,
        )
        resource["id"] = row.id
        row.resource_json = json.dumps(resource)
        db.session.add(row)
    db.session.commit()


def _issues_outcome(issues):
    return {
        "resourceType": "OperationOutcome",
        "issue": [{"severity": "warning", "code": "incomplete",
                   "diagnostics": f"{i['linkId']}: {i['detail']}"}
                  for i in issues],
    }
```

- [ ] **Step 4: Wire the routes into the blueprint**

At the very end of `r6/routes.py` (after all existing route definitions and helpers), add:

```python
# --- SDC ($populate / $extract) ---
from r6.sdc.routes import register_sdc_routes  # noqa: E402

register_sdc_routes(r6_blueprint, {
    "operation_outcome": _operation_outcome,
    "authenticate_tenant_read": authenticate_tenant_read,
    "validate_step_up_token": validate_step_up_token,
    "validator": validator,
    "context_builder": context_builder,
})
```

(`validator` and `context_builder` are the module-level singletons already
instantiated in `r6/routes.py` — confirm their exact names with
`grep -n "^validator\|^context_builder\|validator =\|context_builder =" r6/routes.py`
and match them.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run python -m pytest tests/test_sdc_routes.py -v`
Expected: PASS (4 tests). If `tenant_headers`/`auth_headers` behave differently than assumed, inspect `tests/conftest.py` and adjust the test headers — do not weaken the route's auth.

- [ ] **Step 6: Run the full suite for regressions**

Run: `uv run python -m pytest tests/ -q`
Expected: all green (the read-auth refactor in Task 1 is exercised here).

- [ ] **Step 7: Commit**

```bash
git add r6/sdc/routes.py r6/routes.py tests/test_sdc_routes.py
git commit -m "feat(sdc): \$populate + \$extract Flask operations with guardrails"
```

---

## Task 6: Seed a demo Questionnaire + round-trip integration test

**Files:**
- Modify: `r6/seed.py` (add a sample intake Questionnaire to the built-in resources)
- Test: `tests/test_sdc_roundtrip.py` (new)

- [ ] **Step 1: Write the failing round-trip test**

Create `tests/test_sdc_roundtrip.py`:

```python
import json

from r6.models import R6Resource, db


def _store(app_ctx_resource, tenant_id):
    r = R6Resource(
        resource_type=app_ctx_resource["resourceType"],
        resource_json=json.dumps(app_ctx_resource),
        resource_id=app_ctx_resource["id"],
        tenant_id=tenant_id,
    )
    db.session.add(r)
    db.session.commit()


def test_full_populate_then_extract_roundtrip(client, tenant_id,
                                              tenant_headers, auth_headers):
    app = client.application
    with app.app_context():
        _store({"resourceType": "Patient", "id": "p1",
                "name": [{"given": ["Ada"], "family": "Lovelace"}],
                "birthDate": "1815-12-10"}, tenant_id)
        _store({"resourceType": "Observation", "id": "o1", "status": "final",
                "code": {"coding": [{"system": "http://loinc.org",
                                     "code": "29463-7"}]},
                "subject": {"reference": "Patient/p1"},
                "effectiveDateTime": "2026-06-01",
                "valueQuantity": {"value": 70, "unit": "kg"}}, tenant_id)
        # Seeded demo Questionnaire id is stable: 'healthclaw-intake'.

    # 1. Populate from the seeded Questionnaire.
    pop = client.post(
        "/r6/fhir/Questionnaire/healthclaw-intake/$populate",
        headers=tenant_headers,
        json={"resourceType": "Parameters",
              "parameter": [{"name": "subject",
                             "valueReference": {"reference": "Patient/p1"}}]})
    assert pop.status_code == 200
    qr = _response_param(pop.get_json())
    qr["status"] = "completed"

    # 2. Extract the completed response (dry run — assert the Bundle shape).
    ext = client.post(
        "/r6/fhir/QuestionnaireResponse/$extract?dryRun=true",
        headers=auth_headers,
        json={"resourceType": "Parameters",
              "parameter": [{"name": "questionnaire-response",
                             "resource": qr}]})
    assert ext.status_code == 200
    bundle = _return_param(ext.get_json())
    types = {e["resource"]["resourceType"] for e in bundle["entry"]}
    assert "Observation" in types or "Patient" in types


def _response_param(params):
    for p in params["parameter"]:
        if p["name"] == "response":
            return p["resource"]
    raise AssertionError("no response param")


def _return_param(params):
    for p in params["parameter"]:
        if p["name"] == "return":
            return p["resource"]
    raise AssertionError("no return param")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_sdc_roundtrip.py -v`
Expected: FAIL (populate 404 — `healthclaw-intake` not seeded).

- [ ] **Step 3: Add the demo Questionnaire to the seed set**

In `r6/seed.py`, inside `_built_in_resources()`, append this resource to the returned list (match the existing return structure — it returns a `list[dict]` of FHIR resources):

```python
        {
            "resourceType": "Questionnaire",
            "id": "healthclaw-intake",
            "url": "https://healthclaw.io/Questionnaire/healthclaw-intake",
            "version": "1.0.0",
            "status": "active",
            "title": "HealthClaw Demo Intake",
            "extension": [{
                "url": "http://hl7.org/fhir/uv/sdc/StructureDefinition/"
                       "sdc-questionnaire-definitionExtract",
                "valueCode": "Patient",
            }],
            "item": [
                {
                    "linkId": "given-name",
                    "type": "string",
                    "text": "First name",
                    "definition": "http://hl7.org/fhir/StructureDefinition/"
                                  "Patient#Patient.name.given",
                    "extension": [{
                        "url": "http://hl7.org/fhir/uv/sdc/StructureDefinition/"
                               "sdc-questionnaire-initialExpression",
                        "valueExpression": {
                            "language": "text/fhirpath",
                            "expression": "%patient.name.given.first()"},
                    }],
                },
                {
                    "linkId": "family-name",
                    "type": "string",
                    "text": "Last name",
                    "definition": "http://hl7.org/fhir/StructureDefinition/"
                                  "Patient#Patient.name.family",
                    "extension": [{
                        "url": "http://hl7.org/fhir/uv/sdc/StructureDefinition/"
                               "sdc-questionnaire-initialExpression",
                        "valueExpression": {
                            "language": "text/fhirpath",
                            "expression": "%patient.name.family"},
                    }],
                },
                {
                    "linkId": "body-weight",
                    "type": "quantity",
                    "text": "Body weight",
                    "code": [{"system": "http://loinc.org", "code": "29463-7"}],
                    "extension": [{
                        "url": "http://hl7.org/fhir/uv/sdc/StructureDefinition/"
                               "sdc-questionnaire-observationExtract",
                        "valueBoolean": True}],
                },
            ],
        },
```

- [ ] **Step 4: Confirm the seed test fixture seeds built-ins**

The `client` fixture seeds the demo tenant on setup via `seed_demo_data`. Verify the Questionnaire is reachable in a quick check:

Run: `uv run python -m pytest tests/test_sdc_roundtrip.py -v`
Expected: PASS. If the test's `tenant_id` differs from the seeded `desktop-demo` tenant, store the Questionnaire under `tenant_id` in the test's `app_context` block (same `_store` helper) instead of relying on the seed.

- [ ] **Step 5: Run the full suite**

Run: `uv run python -m pytest tests/ -q`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add r6/seed.py tests/test_sdc_roundtrip.py
git commit -m "feat(sdc): seed demo intake Questionnaire + round-trip test"
```

---

## Task 7: MCP tools (questionnaire_populate / questionnaire_extract)

**Files:**
- Modify: `services/agent-orchestrator/src/tools.ts` (tool definitions, dispatch cases, helper methods)
- Modify: `services/agent-orchestrator/src/tools.test.ts` (tests)
- Modify: `CLAUDE.md` (tool count 20 → 22; add to Read/Write groups)

- [ ] **Step 1: Write the failing tests**

In `services/agent-orchestrator/src/tools.test.ts`, add (match the existing test style in that file — find how other tools assert their presence in the tool list):

```typescript
  it("lists questionnaire_populate as a read tool", () => {
    const tools = getToolDefinitions(); // use whatever the file already calls
    const t = tools.find((x) => x.name === "questionnaire_populate");
    expect(t).toBeDefined();
    expect(t!.tier).toBe("read");
  });

  it("lists questionnaire_extract as a write tool", () => {
    const tools = getToolDefinitions();
    const t = tools.find((x) => x.name === "questionnaire_extract");
    expect(t).toBeDefined();
    expect(t!.tier).toBe("write");
  });
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd services/agent-orchestrator && npm test -- tools.test.ts`
Expected: FAIL (tools not defined).

- [ ] **Step 3: Add tool definitions**

In `tools.ts`, in the tool-definitions array (alongside `fhir_validate` ~line 291), add:

```typescript
      {
        name: "questionnaire_populate",
        description:
          "SDC $populate — pre-fill a Questionnaire for a subject. Returns a QuestionnaireResponse. Read tier; mints a tenant token for non-public tenants.",
        tier: "read",
        annotations: { readOnlyHint: true, destructiveHint: false, openWorldHint: false },
        inputSchema: {
          type: "object",
          properties: {
            questionnaire_id: { type: "string", description: "Stored Questionnaire id" },
            questionnaire: { type: "object", description: "Inline Questionnaire (overrides questionnaire_id)" },
            subject_reference: { type: "string", description: "Subject reference, e.g. 'Patient/p1'" },
          },
          required: ["subject_reference"],
        },
      },
      {
        name: "questionnaire_extract",
        description:
          "SDC $extract — extract FHIR resources from a completed QuestionnaireResponse into a transaction Bundle. Write tier; requires step-up unless dry_run=true.",
        tier: "write",
        annotations: { readOnlyHint: false, destructiveHint: true, openWorldHint: false },
        inputSchema: {
          type: "object",
          properties: {
            questionnaire_response: { type: "object", description: "Completed QuestionnaireResponse" },
            questionnaire: { type: "object", description: "The referenced Questionnaire (optional if resolvable by reference)" },
            dry_run: { type: "boolean", description: "Preview the Bundle without committing", default: false },
          },
          required: ["questionnaire_response"],
        },
      },
```

- [ ] **Step 4: Add dispatch cases**

In the `switch` block (alongside `case "fhir_validate":` ~line 734), add:

```typescript
      case "questionnaire_populate":
        return this.populateQuestionnaire(
          input.questionnaire_id as string | undefined,
          input.questionnaire as Record<string, unknown> | undefined,
          input.subject_reference as string,
          fwdHeaders
        );

      case "questionnaire_extract":
        return this.extractQuestionnaire(
          input.questionnaire_response as Record<string, unknown>,
          input.questionnaire as Record<string, unknown> | undefined,
          (input.dry_run as boolean) ?? false,
          fwdHeaders
        );
```

- [ ] **Step 5: Add helper methods**

Add these methods to the same class (alongside the other private async helpers, e.g. after `evaluatePermission` ~line 1145). Match the existing `${this.baseUrl}` pattern (it is the `/r6/fhir` root):

```typescript
  private async populateQuestionnaire(
    questionnaireId: string | undefined,
    questionnaire: Record<string, unknown> | undefined,
    subjectReference: string,
    headers: Record<string, string>
  ): Promise<Record<string, unknown>> {
    const parameter: Array<Record<string, unknown>> = [
      { name: "subject", valueReference: { reference: subjectReference } },
    ];
    if (questionnaire) parameter.push({ name: "questionnaire", resource: questionnaire });

    const path = questionnaireId
      ? `/Questionnaire/${encodeURIComponent(questionnaireId)}/$populate`
      : `/Questionnaire/$populate`;
    const resp = await fetch(`${this.baseUrl}${path}`, {
      method: "POST",
      headers,
      body: JSON.stringify({ resourceType: "Parameters", parameter }),
    });
    if (!resp.ok) {
      return { error: `$populate failed with status ${resp.status}` };
    }
    return (await resp.json()) as Record<string, unknown>;
  }

  private async extractQuestionnaire(
    questionnaireResponse: Record<string, unknown>,
    questionnaire: Record<string, unknown> | undefined,
    dryRun: boolean,
    headers: Record<string, string>
  ): Promise<Record<string, unknown>> {
    const parameter: Array<Record<string, unknown>> = [
      { name: "questionnaire-response", resource: questionnaireResponse },
    ];
    if (questionnaire) parameter.push({ name: "questionnaire", resource: questionnaire });

    const url = `${this.baseUrl}/QuestionnaireResponse/$extract?dryRun=${dryRun}`;
    const resp = await fetch(url, {
      method: "POST",
      headers,
      body: JSON.stringify({ resourceType: "Parameters", parameter }),
    });
    if (!resp.ok) {
      return { error: `$extract failed with status ${resp.status}` };
    }
    return (await resp.json()) as Record<string, unknown>;
  }
```

- [ ] **Step 6: Run TS compile + tests**

Run: `cd services/agent-orchestrator && npx tsc --noEmit && npm test -- tools.test.ts`
Expected: compiles clean; the two new tests PASS.

- [ ] **Step 7: Update CLAUDE.md**

In `CLAUDE.md`, in the **MCP Server** section:
- Change "20 tools in three groups" → "22 tools in three groups".
- Add `questionnaire_populate` to the **Read** list and `questionnaire_extract` to the **Write** list.

- [ ] **Step 8: Commit**

```bash
git add services/agent-orchestrator/src/tools.ts services/agent-orchestrator/src/tools.test.ts CLAUDE.md
git commit -m "feat(sdc): questionnaire_populate + questionnaire_extract MCP tools"
```

---

## Task 8: Final verification

- [ ] **Step 1: Full Python suite**

Run: `uv run python -m pytest tests/ -q`
Expected: all green, including the new SDC tests.

- [ ] **Step 2: MCP server build + tests**

Run: `cd services/agent-orchestrator && npx tsc --noEmit && npm test`
Expected: compiles clean; all tests pass.

- [ ] **Step 3: Compliance re-check**

Read `.claude/compliance/hipaa.md` and confirm the populate output redaction and extract audit obligations are satisfied (AuditEvent emitted on both operations; `$extract` step-up gated; no PHI in audit `detail`).

- [ ] **Step 4: Manual smoke (optional, requires Flask on :5000)**

```bash
python main.py &
# Populate the demo intake for the demo patient:
curl -s -X POST "http://localhost:5000/r6/fhir/Questionnaire/healthclaw-intake/\$populate" \
  -H "X-Tenant-Id: desktop-demo" -H "Content-Type: application/json" \
  -d '{"resourceType":"Parameters","parameter":[{"name":"subject","valueReference":{"reference":"Patient/<seeded-id>"}}]}'
```
Expected: 200 with a QuestionnaireResponse in the `response` parameter.

---

## Notes for the implementer

- **Redaction on populate** (spec §3, optional `redaction` param) is intentionally NOT in the core tasks above to keep them bite-sized. Add it as a follow-up: read `?redaction=<profile>` in `sdc_populate`, and when set, run `apply_patient_controlled_redaction(qr, patient_id)` (import from `r6.redaction`) over the QuestionnaireResponse before returning. Add a test asserting free-text answers are stripped under the deidentified profile.
- **Do not** use `redact_resource` — it does not exist. The redaction imports are `apply_redaction` and `apply_patient_controlled_redaction`.
- Keep `notify_tenant` (if you wire any Telegram notification) summary-level: counts/status only, never QR answers or extracted PHI.
- The `_set_path` helper in extract.py covers the demo's element paths (`Patient.name.given`, `Patient.name.family`, `Patient.birthDate`). Extending to arbitrary US Core paths is a future phase, not v1.
```
