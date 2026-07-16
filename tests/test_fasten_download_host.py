"""The Fasten private key is attached as Basic auth only for the real Fasten
download host — never a look-alike. Guards CodeQL py/incomplete-url-substring-
sanitization at r6/fasten/ingester.py.
"""

from __future__ import annotations

import pytest

from r6.fasten.ingester import _is_fasten_download_host


@pytest.mark.parametrize("url", [
    "https://fastenhealth.com/export/abc.ndjson",
    "https://api.fastenhealth.com/v1/download/xyz",
    "https://connect.fastenhealth.com/files/1",
])
def test_real_fasten_hosts_get_credentials(url):
    assert _is_fasten_download_host(url) is True


@pytest.mark.parametrize("url", [
    # substring / look-alike hosts that the old `'fastenhealth.com' in url`
    # check would have wrongly trusted with the private key
    "https://evil.com/fastenhealth.com",
    "https://fastenhealth.com.evil.com/export",
    "https://evil.com/?redir=fastenhealth.com",
    "https://notfastenhealth.com/export",
    "https://fastenhealth.com.attacker.io/x",
    # cleartext must never carry Basic-auth credentials
    "http://fastenhealth.com/export",
    # malformed / empty
    "not a url",
    "",
    "ftp://fastenhealth.com/x",
])
def test_lookalike_and_cleartext_hosts_are_rejected(url):
    assert _is_fasten_download_host(url) is False
