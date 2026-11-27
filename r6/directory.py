"""A read-only client for two public provider directories.

``find_clinicians`` asks AINPI (ainpi.dev, a published audit of the CMS
National Provider Directory) for a clinician or practice by name and state.
``find_pharmacies`` asks the NPPES NPI Registry (CMS) for pharmacies in a ZIP
code. Neither returns a phone or fax number, and nothing else here does.

What this must never lose
-------------------------
1. **Only search terms leave.** A name (letters and ``' . -``, 2-40), a state
   (two letters) or a ZIP (five digits). Anything else raises ValueError
   before a URL is formed. No tenant, no header of the caller's and nothing
   read from a record is ever sent.
2. **A listing has no number.** ``Listing`` carries ``has_phone`` and no
   phone field. A name that holds a phone-shaped number drops its row, and
   a city that does is left out. Looking up one entry's number is a
   separate step that does not exist yet; it arrives with its caller.
3. **A filter the directory ignored is not an answer.** AINPI echoes the
   query it ran, and it silently drops parameters it does not know. If the
   echo does not match what was sent, the rows are unfiltered and refused.
4. **Three answers, kept apart.** Listings; no listings (asked, and the
   answer is an empty list); ``Unavailable`` (it could not be asked, or the
   answer had no list where one belongs). The first two are cached for ten
   minutes. A failure is remembered for under a minute, so an outage is not
   asked again at once, and is raised again: it is never an answer.
   ``Found.dropped`` tells "none" from "rows came back and none was kept".
5. **One attempt.** A short timeout, no retry, no redirect followed.

Every string taken from a directory is shape-checked and a row that fails is
dropped whole. The check is on shape and nothing more: up to 80 characters
of a directory's own wording still reach whoever reads a listing, a model
included. A pharmacy's type is named from ``PHARMACY_TYPES`` by code, never
from the registry's description.

The cache is per process: each worker asks, and remembers, on its own.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass

import requests

AINPI_SEARCH_URL = "https://ainpi.dev/api/npd/search"
NPPES_URL = "https://npiregistry.cms.hhs.gov/api/"
TIMEOUT = 5  # seconds, per request
MAX_RESULTS = 10
CACHE_SECONDS = 600
UNAVAILABLE_SECONDS = 45  # how long a failed ask is remembered as failed
NPPES_PAGE = 50  # rows asked of NPPES; a full page may not be all of them
CACHE_MAX = 256

#: NUCC taxonomy codes for pharmacies a person can walk into or call, with
#: our own wording. Each code was in the NPPES answer captured on 2026-11-27
#: with a description that starts "Pharmacy" (the fixture's header lists
#: them). Mail-order, managed-care and home-infusion pharmacies are left out
#: on purpose; a code not listed here is not shown.
PHARMACY_TYPES = {
    "333600000X": "Pharmacy",
    "3336C0003X": "Community pharmacy",
    "3336C0002X": "Clinic pharmacy",
    "3336S0011X": "Specialty pharmacy",
}

_NAME = re.compile(r"[A-Za-z][A-Za-z '.-]{1,39}")
_STATE = re.compile(r"[A-Z]{2}")
_ZIP = re.compile(r"[0-9]{5}")
_NPI = re.compile(r"[0-9]{10}")
#: What a directory's own name or city may look like before we repeat it.
_SHOWN = re.compile(r"[A-Za-z0-9][A-Za-z0-9 .,'&#/()-]{0,79}")
#: A phone number hiding in a name, however it is broken up: seven digits
#: in a row once everything between them is gone (555-0100, 215 555 0100).
_BETWEEN = re.compile(r"[^A-Za-z0-9]")
_LONG_DIGITS = re.compile(r"[0-9]{7}")


class Unavailable(Exception):
    """The directory could not be asked, or did not answer as expected."""


@dataclass(frozen=True)
class Listing:
    """One directory entry as a search returns it. There is no phone field.
    ``name`` and ``city`` are the directory's wording, checked for shape only.

    ``has_phone`` is True or False when the source lists a practice-location
    number, and None when the source does not say (AINPI). ``taxonomy`` is
    our own label for a pharmacy type, and None for a clinician: AINPI's
    search rows carry no specialty.
    """

    npi: str
    name: str
    kind: str  # "clinician", "practice" or "pharmacy"
    taxonomy: str | None
    city: str | None
    state: str | None
    postal_code: str | None
    has_phone: bool | None


@dataclass(frozen=True)
class Found:
    """A search's answer. ``dropped`` counts rows the directory returned
    that are not listed (one failed a check, or was not what was asked
    for). ``truncated`` is True when the directory may hold more matches
    than are listed."""

    listings: tuple[Listing, ...]
    dropped: int
    truncated: bool


def is_npi(value) -> bool:
    """Ten ASCII digits whose last is the Luhn check digit over 80840 + NPI."""
    if not (isinstance(value, str) and _NPI.fullmatch(value)):
        return False
    total = 0
    for i, ch in enumerate(reversed("80840" + value)):
        n = int(ch) * (2 if i % 2 else 1)
        total += n - 9 if n > 9 else n
    return total % 10 == 0


def _shown(value) -> str | None:
    """A directory string we will repeat, or None."""
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    if not _SHOWN.fullmatch(text):
        return None
    # Ten digits in all is a number too, whatever sits between them.
    if _LONG_DIGITS.search(_BETWEEN.sub("", text)) \
            or sum(c.isdigit() for c in text) >= 10:
        return None
    return text


_SESSION = None
_LOCK = threading.Lock()
_CACHE: dict[tuple, tuple[float, object]] = {}
_FAILED = object()  # a remembered failure; never handed back as an answer


def _session():
    """One pooled session per process. requests does not retry by default,
    and nothing here asks it to."""
    global _SESSION
    if _SESSION is None:
        with _LOCK:
            if _SESSION is None:
                _SESSION = requests.Session()
    return _SESSION


def _get(url: str, params: dict, session) -> dict:
    """One GET; the JSON object it answered with, or Unavailable."""
    try:
        resp = (session or _session()).get(
            url, params=params, headers={"Accept": "application/json"},
            timeout=TIMEOUT, allow_redirects=False)
        body = resp.json() if resp.status_code == 200 else None
    except Exception:  # noqa: BLE001 — every failure is the same answer
        # `from None`: a transport error's text carries the URL.
        raise Unavailable("request failed") from None
    # NPPES reports a refused query as HTTP 200 with an "Errors" list.
    if not isinstance(body, dict) or "Errors" in body:
        raise Unavailable("no answer")
    return body


def _cached(key: tuple, ask):
    """``ask()``, remembered by its search terms. A failure is remembered
    too, for UNAVAILABLE_SECONDS, and raised again as Unavailable."""
    now = time.monotonic()
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and hit[0] > now:
            if hit[1] is _FAILED:
                raise Unavailable("asked a moment ago, and it failed")
            return hit[1]
    try:
        value, keep = ask(), CACHE_SECONDS
    except Unavailable:
        value, keep = _FAILED, UNAVAILABLE_SECONDS
    with _LOCK:
        if key not in _CACHE and len(_CACHE) >= CACHE_MAX:
            _CACHE.pop(next(iter(_CACHE)))
        _CACHE[key] = (now + keep, value)
    if value is _FAILED:
        raise Unavailable("no answer")
    return value


def _rows(value) -> list[dict]:
    """A list inside one row. A row without it is dropped, not refused."""
    return [r for r in value if isinstance(r, dict)] \
        if isinstance(value, list) else []


def _listed(value) -> list[dict]:
    """The list of rows an answer is made of. Anything but a list is an
    answer that could not be read: only an empty list means none. A row
    that is not an object becomes an empty one, which fails every check."""
    if not isinstance(value, list):
        raise Unavailable("no list of rows in the answer")
    return [r if isinstance(r, dict) else {} for r in value]


def clinician_terms(name, state) -> tuple[str, str]:
    """The name and state as they will be sent, or ValueError for a
    malformed one. Sends nothing."""
    if not (isinstance(name, str) and isinstance(state, str)):
        raise ValueError("name and state are required")
    name, state = " ".join(name.split()), state.strip().upper()
    if not _NAME.fullmatch(name) or not _STATE.fullmatch(state):
        raise ValueError("malformed name or state")
    return name, state


def pharmacy_term(zip_code) -> str:
    """The ZIP code as it will be sent, or ValueError for anything but five
    digits. Sends nothing."""
    if not (isinstance(zip_code, str) and _ZIP.fullmatch(zip_code)):
        raise ValueError("malformed ZIP code")
    return zip_code


def find_clinicians(name: str, state: str, session=None) -> Found:
    """Clinicians and practices AINPI lists under this name in this state.

    Raises ValueError, before anything is sent, for a malformed term; raises
    Unavailable for a failed answer, one whose echoed query differs, or one
    without both lists of rows.
    """
    name, state = clinician_terms(name, state)

    def ask():
        body = _get(AINPI_SEARCH_URL, {"name": name, "state": state}, session)
        echo = body.get("query")
        if not isinstance(echo, dict) or any(
                not isinstance(echo.get(k), str)
                or echo[k].casefold() != sent.casefold()
                for k, sent in (("name", name), ("state", state))):
            raise Unavailable("the directory did not apply the search")
        data = body.get("data")
        if not isinstance(data, dict):
            raise Unavailable("no data in the answer")
        found = []
        for row in _listed(data.get("practitioners")):
            shown = _shown(f"{row.get('given_name') or ''} "
                           f"{row.get('family_name') or ''}")
            found.append((row, shown, "clinician"))
        for row in _listed(data.get("organizations")):
            found.append((row, _shown(row.get("name")), "practice"))
        out = []
        for row, shown, kind in found:
            zip5 = str(row.get("postal_code") or "")[:5]
            if shown and is_npi(row.get("npi")) and row.get("active") is True:
                out.append(Listing(
                    row["npi"], shown, kind, None, _shown(row.get("city")),
                    row["state"] if _STATE.fullmatch(str(row.get("state")))
                    else None,
                    zip5 if _ZIP.fullmatch(zip5) else None, None))
        return Found(tuple(out[:MAX_RESULTS]), len(found) - len(out),
                     len(out) > MAX_RESULTS)

    return _cached(("clinician", name.casefold(), state), ask)


def _location(row: dict, zip_code: str) -> dict:
    """The row's practice location in this ZIP code, or {}. NPPES keeps the
    first practice location in ``addresses`` and any others in
    ``practiceLocations``, and matches a ZIP on all of them. A mailing
    address is never used: it is often a head office in another state."""
    for address in (_rows(row.get("addresses"))
                    + _rows(row.get("practiceLocations"))):
        if (address.get("address_purpose") == "LOCATION"
                and str(address.get("postal_code") or "")[:5] == zip_code):
            return address
    return {}


def find_pharmacies(zip_code: str, session=None) -> Found:
    """Pharmacies NPPES lists with a practice location in this ZIP code.

    Raises ValueError, before anything is sent, for anything but five
    digits; raises Unavailable for a failed answer or one without a list of
    results.
    """
    zip_code = pharmacy_term(zip_code)

    def ask():
        body = _get(NPPES_URL, {
            "version": "2.1", "enumeration_type": "NPI-2",
            "taxonomy_description": "Pharmacy", "postal_code": zip_code,
            "limit": str(NPPES_PAGE)}, session)
        rows = _listed(body.get("results"))
        out = []
        for row in rows:
            where = _location(row, zip_code)
            phone = where.get("telephone_number")
            basic = row.get("basic") if isinstance(row.get("basic"), dict) \
                else {}
            label = next((PHARMACY_TYPES[t["code"]]
                          for t in _rows(row.get("taxonomies"))
                          if isinstance(t.get("code"), str)
                          and t["code"] in PHARMACY_TYPES), None)
            shown = _shown(basic.get("organization_name"))
            # NPPES matches a ZIP on the mailing address too: no location
            # in the ZIP, no listing.
            if (label and shown and where and is_npi(row.get("number"))
                    and basic.get("status") == "A"):
                out.append(Listing(
                    row["number"], shown, "pharmacy", label,
                    _shown(where.get("city")),
                    where["state"] if _STATE.fullmatch(
                        str(where.get("state"))) else None,
                    zip_code, isinstance(phone, str)
                    and sum(c.isdigit() for c in phone) == 10))
        return Found(tuple(out[:MAX_RESULTS]), len(rows) - len(out),
                     len(out) > MAX_RESULTS or len(rows) >= NPPES_PAGE)

    return _cached(("pharmacy", zip_code), ask)
