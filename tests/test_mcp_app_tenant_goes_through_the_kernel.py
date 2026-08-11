"""The four MCP App pages read their tenant through the access kernel.

Access kernel, slice 11a (docs/2026-08-03-access-kernel-spec.md §2.6). These
are the multi-source sites: `(HEADER, QUERY)`, no default. Slice 11 is one
site per PR because each one has a different source order and each is a
behaviour risk, and this one carries the risk the other ten do not.

WHY THIS SLICE IS NOT INERT LIKE 10a/10b

Every site in slices 9 and 10 sits behind `enforce_tenant_id`, which has
already required the header and format-checked it with the same pattern the
kernel uses. Migrating one of those re-validates a value that cannot fail.

`/mcp-apps/` is an EXEMPT prefix. The hook returns early, so these four
handlers are the only tenant readers in r6/routes.py reachable with no
tenant at all — and that is deliberate: three of the four templates render
an input for the reader to type one into. A page whose job is to be opened
cold must not 400 when it is opened cold. So `absent` is caught and becomes
`''`, exactly as before.

THE ONE DELIBERATE BEHAVIOUR CHANGE

A malformed tenant used to reach the template and render escaped, at 200. It
is now refused at 400. That is a change, so it is pinned here rather than
discovered later, and the pin it invalidates
(test_the_tenant_arrives_from_the_query_and_is_escaped) is rewritten in this
same PR with its reason, per the working protocol §6.

The reasoning: `[A-Za-z0-9_-]{1,64}` is the pattern `enforce_tenant_id`
applies to every other route in the app. A tenant that fails it cannot be
used anywhere, so a page rendered around one is a page whose every fetch is
already doomed — it just fails later, in a browser console, instead of here
where the reason is legible. The templates keep their escaping; it is now
defence in depth rather than the only thing between a query string and the
markup.
"""

import re
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
ROUTES = ROOT / 'r6' / 'routes.py'

#: Every MCP App page, with the marker that proves the template rendered.
PAGES = (
    ('/r6/fhir/mcp-apps/care-gaps', 'care-gaps'),
    ('/r6/fhir/mcp-apps/lab-trends', 'lab-trends'),
    ('/r6/fhir/mcp-apps/wearables', 'wearables'),
    ('/r6/fhir/mcp-apps/compiled-truth/Observation/obs-1', 'compiled-truth'),
)


@pytest.mark.parametrize('path,app_name', PAGES)
def test_the_page_still_opens_cold(client, path, app_name):
    """MUTATION: let `absent` propagate instead of returning '' -> red.

    This is the property the slice exists to protect. An MCP client sends the
    header; a person opening the same resource URI in a browser cannot.
    """
    r = client.get(path)
    assert r.status_code == 200
    assert r.headers['X-MCP-App'] == app_name


@pytest.mark.parametrize('path,_app', PAGES)
def test_the_query_string_still_supplies_the_tenant(client, path, _app):
    r = client.get(f'{path}?tenant_id=desktop-demo')
    assert r.status_code == 200
    assert 'desktop-demo' in r.get_data(as_text=True)


@pytest.mark.parametrize('path,_app', PAGES)
def test_the_header_still_wins_over_the_query(client, path, _app):
    """The declared order is (HEADER, QUERY), which is the order these four
    handlers already used. `sources` is an ordered tuple precisely so a
    migration cannot quietly reverse a precedence.

    MUTATION: swap the tuple to (QUERY, HEADER) -> red.
    """
    r = client.get(f'{path}?tenant_id=from-query',
                   headers={'X-Tenant-Id': 'from-header'})
    body = r.get_data(as_text=True)
    assert r.status_code == 200
    assert 'from-header' in body
    assert 'from-query' not in body


@pytest.mark.parametrize('path,_app', PAGES)
def test_a_malformed_tenant_is_refused_rather_than_rendered(client, path,
                                                            _app):
    """The deliberate delta. Documented in this module's docstring.

    MUTATION: catch MALFORMED as well as ABSENT and return '' -> red.
    """
    r = client.get(f'{path}?tenant_id=%22%3E%3Cscript%3Ex')
    assert r.status_code == 400
    assert '<script>x' not in r.get_data(as_text=True)


@pytest.mark.parametrize('path,_app', PAGES)
def test_a_refusal_names_the_rule_it_applied(client, path, _app):
    """A 400 that does not say what would have been acceptable sends the
    reader back to the source. The kernel's own renderer prints the pattern;
    this pins that these pages reach it rather than some local 400."""
    r = client.get(f'{path}?tenant_id=not a tenant')
    assert r.status_code == 400
    assert 'a-zA-Z0-9_-' in r.get_data(as_text=True)


def _mcp_apps_section() -> str:
    """r6/routes.py from the MCP Apps banner to the end of the file."""
    src = ROUTES.read_text(encoding='utf-8')
    marker = '# --- MCP Apps (embedded HTML surfaces for MCP clients)'
    assert marker in src, 'the MCP Apps section banner moved; fix this guard'
    return src.split(marker, 1)[1]


def test_no_mcp_app_handler_reads_the_header_itself():
    """The kernel is meant to be the one tenant reader (spec §1.1).

    Asserted on the section rather than the whole file because the rest of
    r6/routes.py still has its own unmigrated sites; this pins that the four
    migrated here do not come back. tests/test_ratchets.py holds the
    whole-file count.

    MUTATION: restore the header-or-query expression in any handler -> red.
    """
    section = _mcp_apps_section()
    code = '\n'.join(line for line in section.split('\n')
                     if not line.lstrip().startswith('#'))
    # The helper's own docstring is prose, not a read; strip docstrings so
    # explaining the rule cannot violate it. This file has tripped that trap
    # three times in one week on other guards.
    code = re.sub(r'"""(?:.|\n)*?"""', '', code)
    assert "request.headers.get('X-Tenant-Id')" not in code
    assert 'request.args.get(' not in code
