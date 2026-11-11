"""A client for RxNav, the National Library of Medicine's RxNorm service.

One question, one request: what is this RxNorm code, and may it be named?
``fetch`` asks ``/REST/rxcui/<code>/historystatus.json`` and answers with a
``Concept`` whose label, if it has one, follows the static table's rule
(``r6/terminology.py``): a brand or a pack is named by what is in it, never
by its brand, and a strength or release form is never dropped.

What this must never lose
-------------------------
1. **Only the code leaves.** A code is sent only if it is one to eight ASCII
   digits. Anything else (free text in a code slot, a code with a name after
   it) is never formatted into a URL. Nothing here logs.
2. **A retired code has no name.** Only an Active concept is named. A code
   remapped to exactly one concept is followed one hop; remapped to several,
   it is ambiguous and gets none.
3. **Three answers, kept apart.** A ``Concept`` with a label; a ``Concept``
   without one (RxNav answered, and the answer is no); ``Unavailable`` (it
   could not be asked). A caller may cache the first two, never the third.
4. **One attempt.** A short timeout, no retry, no redirect followed.

The label is RxNorm's own name for the code, checked by ``valid_label``. It
is a property of the code, looked up by code; it is never a ``display`` or
``text`` carried on a patient's record.

This product uses publicly available data from the U.S. National Library of
Medicine (NLM), National Institutes of Health, Department of Health and Human
Services; NLM is not responsible for the product and does not endorse or
recommend this or any other product.
"""

from __future__ import annotations

import re
import threading
import unicodedata
from dataclasses import dataclass

import requests

HISTORY_URL = "https://rxnav.nlm.nih.gov/REST/rxcui/%s/historystatus.json"
TIMEOUT = 3  # seconds, per request

_RXCUI = re.compile(r"[0-9]{1,8}")
_TTY = re.compile(r"[A-Z]{2,5}")
#: ASCII only, so no control, format, bidi or zero-width character, and no
#: markup: <>[]{}, backticks, *_#| and double quotes are all outside it.
_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9 .,/()%+:'&-]*")
LABEL_MAX = 120

#: The statuses RxNav documents, lower-cased. The live service has answered
#: "UNKNOWN" where its documentation writes "Unknown", so every comparison
#: is made on the lower-cased form; anything else reads as "unknown".
STATUSES = frozenset({"active", "remapped", "obsolete", "quantified",
                      "notcurrent", "unknown"})

#: Term types named by RxNorm's own name. "product" is a specific drug with
#: its strength and form; "named" is anything less specific.
_PRODUCT, _NAMED = "product", "named"
_INGREDIENT_LEVEL = frozenset({"IN", "PIN", "MIN"})
_COMPONENT_LEVEL = frozenset({"SCDC", "SCDF", "SCDG"})
_PACKS = frozenset({"GPCK", "BPCK"})
_INERT = "inert ingredients"


class Unavailable(Exception):
    """RxNav could not be asked, or did not answer in the expected shape."""


@dataclass(frozen=True)
class Concept:
    """What RxNav says about one code. ``label`` is None when it has no name
    we will use; ``kind`` is "product" or "named" exactly when it has one.
    ``tty`` is the term type of the concept the label came from, which for a
    remapped code is the concept it was remapped to."""

    rxcui: str
    status: str
    tty: str
    label: str | None
    kind: str | None
    remapped_to: str | None


def is_rxcui(code) -> bool:
    """Whether this is shaped like an RxNorm concept id: 1-8 ASCII digits."""
    return isinstance(code, str) and _RXCUI.fullmatch(code) is not None


def valid_label(text) -> bool:
    """Whether this exact string may be shown as a label.

    A string that fails is dropped whole. It is never trimmed to fit: the
    end of a drug name is where its strength and form are.
    """
    if not isinstance(text, str):
        return False
    if text != unicodedata.normalize("NFC", text).strip():
        return False
    if not 2 <= len(text) <= LABEL_MAX or not _LABEL.fullmatch(text):
        return False
    if "://" in text or not any(ch.isalpha() for ch in text):
        return False
    # Imported here: r6.terminology imports the resolver, which imports this.
    from r6.terminology import _ABSENCE_WORDING
    return _ABSENCE_WORDING.search(text) is None


def _text(value) -> str:
    return value if isinstance(value, str) else ""


def parse(body: dict) -> dict:
    """A historystatus body as the plain dict the recorded fixture holds
    (tests/fixtures/rxnav_concepts.json), plus ``scd_name`` and ``remapped``.
    """
    history = body["rxcuiStatusHistory"]
    attrs = history.get("attributes") or {}
    features = history.get("definitionalFeatures") or {}
    derived = history.get("derivedConcepts") or {}
    parts = [
        {
            "ingredient": _text(p.get("baseName")).lower(),
            "numerator": _text(p.get("numeratorValue")),
            "unit": _text(p.get("numeratorUnit")),
            "denominator": _text(p.get("denominatorValue")),
            "denominator_unit": _text(p.get("denominatorUnit")),
            "active_ingredient": _text(p.get("activeIngredientName")).lower(),
        }
        for p in features.get("ingredientAndStrength") or []
    ]
    pack = [
        {"rxcui": _text(m.get("packRxcui")), "name": _text(m.get("packName")),
         "count": _text(m.get("packNumber"))}
        for m in (history.get("pack") or {}).get("packConcept") or []
    ]
    return {
        "name": _text(attrs.get("name")),
        "tty": _text(attrs.get("tty")),
        "status": _text((history.get("metaData") or {}).get("status")),
        "ingredients": parts,
        "dose_forms": [_text(f.get("doseFormName"))
                       for f in features.get("doseFormConcept") or []],
        "pack": pack,
        "scd_name": _text((derived.get("scdConcept") or {})
                          .get("scdConceptName")),
        "remapped": [_text(r.get("remappedRxCui"))
                     for r in derived.get("remappedConcept") or []],
    }


def name_for(concept: dict) -> tuple[str | None, str | None]:
    """(label, kind) for a parsed concept, or (None, None).

    Only an Active concept is named. A clinical drug (SCD) keeps RxNorm's
    name; a branded drug (SBD) takes the name of its generic from the same
    answer; a pack is its ingredients; an ingredient or component keeps its
    name. A brand name, a branded component and every other term type get
    nothing.
    """
    if _text(concept.get("status")).lower() != "active":
        return None, None
    tty = concept.get("tty")
    name, kind = "", _NAMED
    if tty == "SCD":
        name, kind = _text(concept.get("name")), _PRODUCT
    elif tty == "SBD":
        name, kind = _text(concept.get("scd_name")), _PRODUCT
    elif tty in _PACKS:
        names = []
        for part in concept.get("ingredients") or []:
            ingredient = part.get("ingredient")
            if ingredient and ingredient != _INERT and ingredient not in names:
                names.append(ingredient)
        name = " / ".join(names) + " pack" if names else ""
    elif tty in _INGREDIENT_LEVEL:
        name = _text(concept.get("name"))
        name = name[:1].upper() + name[1:]
    elif tty in _COMPONENT_LEVEL:
        name = _text(concept.get("name"))
    label = unicodedata.normalize("NFC", name).strip()
    if not valid_label(label):
        return None, None
    return label, kind


_SESSION = None
_SESSION_LOCK = threading.Lock()


def _session():
    """One pooled session per process. requests does not retry by default,
    and nothing here asks it to."""
    global _SESSION
    if _SESSION is None:
        with _SESSION_LOCK:
            if _SESSION is None:
                _SESSION = requests.Session()
    return _SESSION


def history(code: str, session=None) -> dict:
    """The parsed historystatus answer for one code, in one request.

    Raises ValueError, before anything is sent, for a code that is not an
    RxCUI; raises Unavailable for a failed or malformed answer.
    """
    if not is_rxcui(code):
        raise ValueError("not an RxCUI")
    try:
        resp = (session or _session()).get(
            HISTORY_URL % code,
            # RxNav answers a FHIR Accept with 406; ask for plain JSON.
            headers={"Accept": "application/json"},
            timeout=TIMEOUT,
            allow_redirects=False,
        )
        answered = resp.status_code == 200
        concept = parse(resp.json()) if answered else None
    except Exception:  # noqa: BLE001 — every failure is the same answer
        # `from None`: a transport error's text carries the URL.
        raise Unavailable("request failed") from None
    if concept is None or not concept["status"]:
        raise Unavailable("no answer")
    return concept


def fetch(code: str, session=None) -> Concept | None:
    """What RxNav says about this code, or None if it is not an RxCUI (in
    which case nothing is sent). Raises Unavailable if it could not be asked.
    """
    if not is_rxcui(code):
        return None
    concept = history(code, session)
    status = concept["status"].lower()
    if status not in STATUSES:
        status = "unknown"
    remapped_to = None
    if status == "remapped":
        targets = concept["remapped"]
        # One hop, and only to a single concept. A target that was itself
        # remapped is not Active, so name_for gives it nothing and the
        # chain ends there. With several targets the code is ambiguous:
        # name_for sees the Remapped concept itself and names nothing.
        if len(targets) == 1 and is_rxcui(targets[0]):
            remapped_to = targets[0]
            concept = history(remapped_to, session)
    label, kind = name_for(concept)
    tty = concept["tty"] if _TTY.fullmatch(concept["tty"]) else ""
    return Concept(code, status, tty, label, kind, remapped_to)
