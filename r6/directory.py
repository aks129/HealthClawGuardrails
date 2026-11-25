"""A read-only client for two public provider directories.

``find_clinicians`` asks AINPI (ainpi.dev, a published audit of the CMS
National Provider Directory) for a clinician or practice by name and state.
``find_pharmacies`` asks the NPPES NPI Registry (CMS) for pharmacies in a ZIP
code. ``contact`` asks NPPES for one NPI's practice-location phone and fax.

What this must never lose
-------------------------
1. **Only search terms leave.** A name (letters and ``' . -``, 2-40), a state
   (two letters), a ZIP (five digits) or an NPI (ten digits, check digit
   valid). Anything else raises ValueError before a URL is formed. No tenant,
   no header of the caller's and nothing read from a record is ever sent.
2. **A listing has no number.** ``Listing`` carries ``has_phone`` and no
   phone field. Numbers exist only on ``Contact``, from ``contact(npi)``: a
   second call, for the one entry a person chose and will check themselves.
3. **A filter the directory ignored is not an answer.** AINPI echoes the
   query it ran, and it silently drops parameters it does not know. If the
   echo does not match what was sent, the rows are unfiltered and refused.
4. **Three answers, kept apart.** Listings; no listings (asked, and the
   answer is none); ``Unavailable`` (it could not be asked). The first two
   are cached for a few minutes, the third never.
5. **One attempt.** A short timeout, no retry, no redirect followed.

Every string taken from a directory is shape-checked and a row that fails is
dropped whole. A pharmacy's type is named from ``PHARMACY_TYPES`` by code,
never from the registry's description.
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
CACHE_MAX = 256

#: NUCC taxonomy codes for pharmacies a person can walk into or call, with
#: our own wording. Mail-order, institutional, long-term-care, nuclear and
#: home-infusion pharmacies are left out on purpose.
PHARMACY_TYPES = {
    "333600000X": "Pharmacy",
    "3336C0003X": "Community pharmacy",
    "3336C0002X": "Clinic pharmacy",
    "3336C0004X": "Compounding pharmacy",
    "3336S0011X": "Specialty pharmacy",
}

_NAME = re.compile(r"[A-Za-z][A-Za-z '.-]{1,39}")
_STATE = re.compile(r"[A-Z]{2}")
_ZIP = re.compile(r"[0-9]{5}")
_NPI = re.compile(r"[0-9]{10}")
#: What a directory's own name or city may look like before we repeat it.
_SHOWN = re.compile(r"[A-Za-z0-9][A-Za-z0-9 .,'&#/()-]{0,79}")
#: Seven digits in a row could be a phone number hiding in a name.
_LONG_DIGITS = re.compile(r"[0-9]{7}")
_PHONE = re.compile(r"[0-9]{3}-?[0-9]{3}-?[0-9]{4}")


class Unavailable(Exception):
    """The directory could not be asked, or did not answer as expected."""


@dataclass(frozen=True)
class Listing:
    """One directory entry, safe to show a model. There is no phone field.

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
class Contact:
    """The practice-location numbers NPPES lists for one NPI. For a person
    to check and confirm; never part of a search result."""

    npi: str
    name: str | None
    phone: str | None
    fax: str | None


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
    if not _SHOWN.fullmatch(text) or _LONG_DIGITS.search(text):
        return None
    return text


_SESSION = None
_LOCK = threading.Lock()
_CACHE: dict[tuple, tuple[float, object]] = {}


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
    """``ask()``, remembered by its search terms. Unavailable is raised
    through and never stored."""
    now = time.monotonic()
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and hit[0] > now:
            return hit[1]
    value = ask()
    with _LOCK:
        if key not in _CACHE and len(_CACHE) >= CACHE_MAX:
            _CACHE.pop(next(iter(_CACHE)))
        _CACHE[key] = (now + CACHE_SECONDS, value)
    return value


def _rows(value) -> list[dict]:
    return [r for r in value if isinstance(r, dict)] \
        if isinstance(value, list) else []


def find_clinicians(name: str, state: str, session=None) -> tuple[Listing, ...]:
    """Clinicians and practices AINPI lists under this name in this state.

    Raises ValueError, before anything is sent, for a malformed term; raises
    Unavailable for a failed answer or one whose echoed query differs.
    """
    if not (isinstance(name, str) and isinstance(state, str)):
        raise ValueError("name and state are required")
    name, state = " ".join(name.split()), state.strip().upper()
    if not _NAME.fullmatch(name) or not _STATE.fullmatch(state):
        raise ValueError("malformed name or state")

    def ask():
        body = _get(AINPI_SEARCH_URL, {"name": name, "state": state}, session)
        echo = body.get("query")
        if not isinstance(echo, dict) or any(
                not isinstance(echo.get(k), str)
                or echo[k].casefold() != sent.casefold()
                for k, sent in (("name", name), ("state", state))):
            raise Unavailable("the directory did not apply the search")
        data = body.get("data") if isinstance(body.get("data"), dict) else {}
        found = []
        for row in _rows(data.get("practitioners")):
            shown = _shown(f"{row.get('given_name') or ''} "
                           f"{row.get('family_name') or ''}")
            found.append((row, shown, "clinician"))
        for row in _rows(data.get("organizations")):
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
        return tuple(out[:MAX_RESULTS])

    return _cached(("clinician", name.casefold(), state), ask)


def _location(row: dict) -> dict:
    """The practice-location address. A mailing address is never used: it
    is often a head office in another state."""
    for address in _rows(row.get("addresses")):
        if address.get("address_purpose") == "LOCATION":
            return address
    return {}


def find_pharmacies(zip_code: str, session=None) -> tuple[Listing, ...]:
    """Pharmacies NPPES lists with a practice location in this ZIP code.

    Raises ValueError, before anything is sent, for anything but five
    digits; raises Unavailable for a failed answer.
    """
    if not (isinstance(zip_code, str) and _ZIP.fullmatch(zip_code)):
        raise ValueError("malformed ZIP code")

    def ask():
        body = _get(NPPES_URL, {
            "version": "2.1", "enumeration_type": "NPI-2",
            "taxonomy_description": "Pharmacy", "postal_code": zip_code,
            "limit": "50"}, session)
        out = []
        for row in _rows(body.get("results")):
            where = _location(row)
            basic = row.get("basic") if isinstance(row.get("basic"), dict) \
                else {}
            label = next((PHARMACY_TYPES[t["code"]]
                          for t in _rows(row.get("taxonomies"))
                          if isinstance(t.get("code"), str)
                          and t["code"] in PHARMACY_TYPES), None)
            shown = _shown(basic.get("organization_name"))
            # NPPES matches a ZIP on the mailing address too.
            if (label and shown and is_npi(row.get("number"))
                    and basic.get("status") == "A"
                    and str(where.get("postal_code") or "")[:5] == zip_code):
                out.append(Listing(
                    row["number"], shown, "pharmacy", label,
                    _shown(where.get("city")),
                    where["state"] if _STATE.fullmatch(
                        str(where.get("state"))) else None,
                    zip_code, _phone(where.get("telephone_number")) is not None))
        return tuple(out[:MAX_RESULTS])

    return _cached(("pharmacy", zip_code), ask)


def _phone(value) -> str | None:
    """Ten digits, or None for anything that is not a US number."""
    if not (isinstance(value, str) and _PHONE.fullmatch(value)):
        return None
    return value.replace("-", "")


def contact(npi: str, session=None) -> Contact | None:
    """The practice-location phone and fax NPPES lists for one NPI, or None
    if NPPES has no such NPI. Not for a model: the person who chose this
    entry checks the number before anything uses it.

    Raises ValueError, before anything is sent, for a malformed NPI; raises
    Unavailable for a failed answer.
    """
    if not is_npi(npi):
        raise ValueError("malformed NPI")

    def ask():
        body = _get(NPPES_URL, {"version": "2.1", "number": npi}, session)
        row = next((r for r in _rows(body.get("results"))
                    if r.get("number") == npi), None)
        if row is None:
            return None
        where = _location(row)
        basic = row.get("basic") if isinstance(row.get("basic"), dict) else {}
        name = basic.get("organization_name") or (
            f"{basic.get('first_name') or ''} {basic.get('last_name') or ''}")
        return Contact(npi, _shown(name), _phone(where.get("telephone_number")),
                       _phone(where.get("fax_number")))

    return _cached(("contact", npi), ask)
