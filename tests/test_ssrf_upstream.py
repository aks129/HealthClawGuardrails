"""SSRF guard for the SHARP per-request upstream (X-FHIR-Server-URL).

The predicate tests below check that `validate_upstream_url` decides
correctly. Nothing in them checks that anything ever *calls* it, and for a
guard that is the load-bearing half: a correct rule nobody applies refuses
nothing. `get_proxy_for_request` has exactly one enforcement point, and it
could be deleted with the whole suite green (#634 F3).

So the class at the bottom drives the resolver instead of the predicate. It
is a behavioural check rather than an assertion about source: a test that
greps for the call site pins a spelling, which is the defect this file was
found to have. This pins the outcome.

Two mutations, both executed 2026-09-05, each reddening only its own half:

  `if server_url and not valid:` -> `if False and server_url and not valid:`
  in get_proxy_for_request  ->  the four `builds_no_proxy` rows fail.

  `return valid` -> `return bool(_raw)` in is_sharp_context_active
  ->  the four `does_not_activate_sharp_context` rows fail.

Both leave the other four green, which is the point: they pin two separate
properties and neither stands in for the other.
"""

import pytest

from r6.fhir_proxy import (
    validate_upstream_url,
    _is_blocked_ip,
    get_proxy_for_request,
    is_sharp_context_active,
    close_request_proxy,
    reset_proxy,
    SHARP_SERVER_URL_HEADER,
    SHARP_ACCESS_TOKEN_HEADER,
)


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


class TestHostnamesAreResolvedAndEveryAddressChecked:
    """The hostname branch, which no test reached (#634 F4).

    Every one of the nine tests above passes a literal IP or a malformed
    string, so the resolve-then-check branch was unexecuted. Its docstring
    claims "Hostnames are resolved and EVERY resolved IP is checked" — the
    word doing the work is EVERY, and that is what the second test pins.

    Neither test needs the network. `localhost` resolves from the hosts
    file, and the multi-address case substitutes the resolver outright, so
    this does not repeat the #635 mistake of reaching the public internet
    from the default suite.

    MUTATION: replace the `for info in infos:` loop in validate_upstream_url
    with `return True` -> both tests below fail (executed 2026-09-05).
    """

    def test_a_hostname_resolving_to_loopback_is_refused(self):
        """The branch is reached at all. `localhost` is a hostname, not a
        literal, so it takes the resolve path the nine tests above skip."""
        assert not validate_upstream_url("https://localhost/fhir")

    def test_one_blocked_address_among_several_refuses_the_whole_host(
            self, monkeypatch):
        """EVERY, not ANY.

        A host answering with a public address first and an internal one
        second is the realistic shape — a split-horizon or attacker-run
        domain. Checking only the first resolved address would accept it.
        """
        def fake_getaddrinfo(host, port, *args, **kwargs):
            return [
                (2, 1, 6, "", ("8.8.8.8", port)),
                (2, 1, 6, "", ("169.254.169.254", port)),
            ]
        monkeypatch.setattr(
            "r6.fhir_proxy.socket.getaddrinfo", fake_getaddrinfo)

        assert not validate_upstream_url("https://split-horizon.example/fhir")

    def test_the_refusal_is_not_vacuous_an_all_public_host_is_allowed(
            self, monkeypatch):
        """Without this, the test above passes if the branch refuses
        everything, which would make it prove nothing."""
        def fake_getaddrinfo(host, port, *args, **kwargs):
            return [(2, 1, 6, "", ("8.8.8.8", port))]
        monkeypatch.setattr(
            "r6.fhir_proxy.socket.getaddrinfo", fake_getaddrinfo)

        assert validate_upstream_url("https://all-public.example/fhir")


#: Each is refused by `validate_upstream_url` above. The point here is not
#: that the rule says no, it is that no proxy is built when it does.
BLOCKED_UPSTREAMS = [
    ("cloud metadata", "http://169.254.169.254/latest/meta-data/"),
    ("loopback", "https://127.0.0.1/fhir"),
    ("private range", "https://10.0.0.5/fhir"),
    ("plain http", "http://8.8.8.8/fhir"),
]


class TestTheGuardIsActuallyReached:
    """The enforcement point, not the rule."""

    def teardown_method(self):
        reset_proxy()

    @pytest.mark.parametrize(
        "label,url", BLOCKED_UPSTREAMS, ids=[c[0] for c in BLOCKED_UPSTREAMS])
    def test_a_refused_upstream_builds_no_proxy(self, app, label, url):
        """A caller-supplied upstream that fails validation must not be built.

        With the enforcement removed, the cloud-metadata row returns a live
        FHIRUpstreamProxy aimed at 169.254.169.254 over plain http, carrying
        the caller's SMART token on its client.
        """
        with app.test_request_context(
            "/r6/fhir/Patient",
            headers={
                SHARP_SERVER_URL_HEADER: url,
                SHARP_ACCESS_TOKEN_HEADER: "smart-token-abc123",
            },
        ):
            assert get_proxy_for_request() is None, (
                f"{label}: a proxy was built for an upstream the SSRF guard "
                f"refuses ({url})"
            )

    @pytest.mark.parametrize(
        "label,url", BLOCKED_UPSTREAMS, ids=[c[0] for c in BLOCKED_UPSTREAMS])
    def test_a_refused_upstream_does_not_activate_sharp_context(
            self, app, label, url):
        """Active SHARP context skips read-auth and synthesizes a tenant, so a
        URL that failed validation must not switch it on (#634 F5)."""
        with app.test_request_context(
            "/r6/fhir/Patient",
            headers={SHARP_SERVER_URL_HEADER: url},
        ):
            assert is_sharp_context_active() is False, (
                f"{label}: SHARP context activated on an upstream the guard "
                f"refuses ({url})"
            )

    def test_the_refusal_is_not_vacuous_a_permitted_upstream_still_builds(
            self, app):
        """Without this, both tests above pass if proxying is simply broken.

        This is the failure mode that made #634 F2 possible: three leak
        assertions passing because nothing was captured at all.
        """
        with app.test_request_context(
            "/r6/fhir/Patient",
            headers={
                SHARP_SERVER_URL_HEADER: "https://8.8.8.8/fhir",
                SHARP_ACCESS_TOKEN_HEADER: "smart-token-abc123",
            },
        ):
            proxy = get_proxy_for_request()
            assert proxy is not None, (
                "a permitted upstream built no proxy, so the refusals above "
                "prove nothing"
            )
            assert proxy.upstream_url == "https://8.8.8.8/fhir"
            close_request_proxy()
