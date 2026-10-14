"""QA of PR #955: the local search `self` link keeps its exact shape.

#955 moved the local builder into `search_self_link`. A live byte diff of
local search responses against main matched, but no test pinned the shape:
building the pairs in request order instead of registry order passed the
whole suite. These pin the order, the unknown-key exclusion and the encoding.
"""

from unittest.mock import patch

import pytest


@pytest.mark.parametrize('query,expected', [
    ('context-id=ctx-abc&_count=2', '?_count=2&context-id=ctx-abc'),
    ('_count=3&code=85354-9&patient=Patient/p1',
     '?patient=Patient%2Fp1&code=85354-9&_count=3'),
    ('bogus=1&_count=2', '?_count=2'),
    ('code=a%20b%26c%3Dd', '?code=a+b%26c%3Dd'),
    ('patient=&code=', ''),
])
def test_the_local_self_link_follows_registry_order(
        client, tenant_headers, query, expected):
    """MUTATION: build the pairs in request order -> red."""
    with patch('r6.routes.get_proxy_for_request', return_value=None):
        resp = client.get(f'/r6/fhir/Observation?{query}',
                          headers=tenant_headers)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.get_json()['link'] == [{
        'relation': 'self',
        'url': f'http://localhost/r6/fhir/Observation{expected}'}]


def test_the_helper_reads_the_first_of_a_repeated_key():
    from werkzeug.datastructures import MultiDict

    from r6.search_fidelity import search_self_link
    values = MultiDict([('_count', '5'), ('_count', '9'), ('code', 'x')])
    assert search_self_link('http://h/', 'Condition', values) == (
        'http://h/r6/fhir/Condition?code=x&_count=5')
