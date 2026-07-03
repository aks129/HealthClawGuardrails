"""SSRF guard for the SHARP per-request upstream (X-FHIR-Server-URL)."""

from r6.fhir_proxy import validate_upstream_url, _is_blocked_ip


def test_blocks_cloud_metadata_ip():
    assert not validate_upstream_url("https://169.254.169.254/latest/meta-data/")


def test_blocks_loopback():
    assert not validate_upstream_url("https://127.0.0.1/fhir")
    assert not validate_upstream_url("https://[::1]/fhir")


def test_blocks_private_ranges():
    for ip in ("10.0.0.5", "172.16.0.1", "192.168.1.1"):
        assert not validate_upstream_url(f"https://{ip}/fhir"), ip


def test_requires_https():
    assert not validate_upstream_url("http://8.8.8.8/fhir")


def test_allows_public_ip():
    assert validate_upstream_url("https://8.8.8.8/fhir")


def test_rejects_garbage_and_missing_host():
    assert not validate_upstream_url("not-a-url")
    assert not validate_upstream_url("https:///fhir")
    assert not validate_upstream_url("")


def test_allowlist_rejects_unlisted(monkeypatch):
    monkeypatch.setenv("FHIR_UPSTREAM_ALLOWED_HOSTS", "fhir.medent.com,hapi.fhir.org")
    # public but not on the allowlist -> rejected
    assert not validate_upstream_url("https://8.8.8.8/fhir")


def test_allowlist_allows_listed_ip(monkeypatch):
    monkeypatch.setenv("FHIR_UPSTREAM_ALLOWED_HOSTS", "8.8.8.8")
    assert validate_upstream_url("https://8.8.8.8/fhir")


def test_is_blocked_ip_ranges():
    for ip in ("127.0.0.1", "10.1.2.3", "172.20.0.1", "192.168.0.1",
               "169.254.169.254", "::1", "fd00::1", "0.0.0.0"):
        assert _is_blocked_ip(ip), ip
    for ip in ("8.8.8.8", "1.1.1.1", "93.184.216.34"):
        assert not _is_blocked_ip(ip), ip
