"""POST /internal/seed twice must leave the tenant unchanged, and say so.

#457 made `seed_demo_data` idempotent. This file covers the HTTP path, which
is a separate caller from the pre-deploy CLI and was left asserting the old
behaviour in its own docstring:

    Idempotent — re-seeding the same tenant appends
    new resources (IDs are generated fresh each call).

Read that twice. It claims the guarantee and describes its violation in one
sentence. "Idempotent" and "appends new resources on every call" cannot both
be true, and the two halves had been sitting next to each other long enough
that nobody read past the first word.

That is the same failure the conformance report had (#443/#456) and the same
one `railway.toml` had, where a comment called the pre-deploy seed
"idempotent" while the code inserted unconditionally. Three surfaces, one
shape: a reassuring word doing the work that a check should do.

So this file does not test the docstring's wording. It tests the behaviour
through the endpoint and pins the claim to it, because a comment cannot be
trusted to describe code that nothing forces it to match.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from models import db
from r6.models import R6Resource

_TENANT = "seed-endpoint-idempotency"
_ENDPOINT = "/r6/fhir/internal/seed"


def _counts(tenant: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for row in R6Resource.query.filter_by(tenant_id=tenant).all():
        out[row.resource_type] = out.get(row.resource_type, 0) + 1
    return out


@pytest.fixture
def clean(app):
    with app.app_context():
        R6Resource.query.filter_by(tenant_id=_TENANT).delete()
        db.session.commit()
        yield
        R6Resource.query.filter_by(tenant_id=_TENANT).delete()
        db.session.commit()


def test_the_endpoint_seeds_something_on_a_fresh_tenant(app, client, clean):
    """A pass over an endpoint that seeds nothing is not a pass."""
    r = client.post(_ENDPOINT, json={"tenant_id": _TENANT})
    assert r.status_code in (200, 201), r.get_data(as_text=True)
    with app.app_context():
        counts = _counts(_TENANT)
    assert counts.get("Patient") == 1, f"first seed produced {counts}"
    assert sum(counts.values()) >= 6, f"first seed produced {counts}"


def test_posting_the_seed_twice_changes_nothing(app, client, clean):
    """The claim in the docstring, as a behaviour.

    MUTATION: remove the skip-if-present branch from seed_demo_data -> red.
    """
    client.post(_ENDPOINT, json={"tenant_id": _TENANT})
    with app.app_context():
        first = _counts(_TENANT)

    client.post(_ENDPOINT, json={"tenant_id": _TENANT})
    with app.app_context():
        second = _counts(_TENANT)

    assert first == second, (
        f"re-seeding through the endpoint changed the tenant.\n"
        f"  after one:  {first}\n  after two: {second}")


def test_posting_the_seed_five_times_still_leaves_one_patient(app, client, clean):
    """Production reached twelve copies, not two.

    MUTATION: make the existence check ignore resource_type -> red.
    """
    for _ in range(5):
        client.post(_ENDPOINT, json={"tenant_id": _TENANT})
    with app.app_context():
        counts = _counts(_TENANT)

    assert counts.get("Patient") == 1, f"5 seeds produced {counts}"
    assert counts.get("Condition") == 1, f"5 seeds produced {counts}"


def test_a_custom_bundle_without_ids_is_still_allowed_to_append(app, client, clean):
    """The guarantee is about the BUILT-IN set, not about caller-supplied data.

    A caller posting their own bundle with no ids is asking for new rows, and
    refusing that would break the endpoint's other use. The distinction is the
    reason the docstring has to be precise rather than just shorter.

    MUTATION: make the endpoint reject id-less custom resources -> red.
    """
    bundle = {"resourceType": "Bundle", "entry": [
        {"resource": {"resourceType": "Observation", "status": "final",
                      "code": {"text": "custom"}}}]}
    client.post(_ENDPOINT, json={"tenant_id": _TENANT, "bundle": bundle})
    client.post(_ENDPOINT, json={"tenant_id": _TENANT, "bundle": bundle})

    with app.app_context():
        assert _counts(_TENANT).get("Observation") == 2, (
            "a custom id-less bundle should still append; only the built-in "
            "set carries the idempotency guarantee")


def test_no_surface_still_claims_ids_are_generated_fresh_each_call():
    """The stale half of the old sentence, pinned out of the tree.

    MUTATION: restore "IDs are generated fresh each call" anywhere -> red.
    """
    # Every phrasing that promises appending, not just the one the docstring
    # used. The first version of this guard checked only "generated fresh
    # each call" and so missed the endpoint's own RESPONSE note, which went
    # on telling live callers "Re-seed anytime to add more resources" after
    # the docstring four lines above it had been corrected.
    #
    # A guard aimed at one sentence protects that sentence, not the claim.
    STALE = (
        "generated fresh each call",
        "add more resources",
        "appends new resources",
    )
    root = Path(__file__).resolve().parents[1]
    offenders = []
    for path in list(root.glob("r6/**/*.py")) + [root / "railway.toml"]:
        text = path.read_text(encoding="utf-8", errors="replace")
        # Rejoin adjacent string literals so a reflowed line does not hide it.
        joined = re.sub(r'"\s*\n\s*"', "", text)
        joined = re.sub(r"'\s*\n\s*'", "", joined)
        for phrase in STALE:
            if phrase in joined:
                offenders.append(f"{path.relative_to(root)}: {phrase!r}")

    assert not offenders, (
        f"these files still describe the pre-#457 behaviour: {offenders}. "
        f"The built-in seed set now carries stable ids and re-seeding is a "
        f"no-op.")
