"""Real records only go to a vetted model host.

With CARE_REAL_RECORDS open (`on` or `allowlist`), chat turns carry redacted
but real health data to whichever model provider serves chat. That provider's
host must be on a vetted list (Anthropic, OpenAI, Google Gemini and Groq, as
approved by the owner) or be named in CARE_REAL_RECORDS_MODEL_HOSTS, or the
app refuses to boot.
"""

import pytest

from careagents.config import Config, ConfigError

BASE = {"CARE_RP_ID": "localhost", "CARE_ORIGIN": "http://localhost",
        "OPENAI_API_KEY": "sk-test-secret", "HEALTHCLAW_MINT_SECRET": "m"}
FOREIGN = "https://llm.example-provider.test/v1beta/openai"


def _cfg(**env):
    return Config(env={**BASE, **env})


@pytest.mark.parametrize("url", [None, FOREIGN, "http://localhost:11434/v1"])
def test_off_accepts_any_model_host(url):
    env = {"CARE_REAL_RECORDS": "off"}
    if url:
        env["OPENAI_BASE_URL"] = url
    assert _cfg(**env).real_records == "off"
    # Unset CARE_REAL_RECORDS is off too.
    if url:
        assert _cfg(OPENAI_BASE_URL=url).real_records == "off"


@pytest.mark.parametrize("mode", ["on", "allowlist"])
@pytest.mark.parametrize("url", [None, "https://api.openai.com/v1",
                                 "https://API.OpenAI.com/v1/"])
def test_open_real_records_accept_openai(mode, url):
    env = {"CARE_REAL_RECORDS": mode}
    if url:
        env["OPENAI_BASE_URL"] = url
    assert _cfg(**env).provider == "openai"


@pytest.mark.parametrize("cred", ["ANTHROPIC_API_KEY", "ANTHROPIC_OAUTH_TOKEN"])
def test_open_real_records_accept_anthropic(cred):
    cfg = _cfg(CARE_REAL_RECORDS="on", **{cred: "a-test-secret"})
    assert cfg.provider == "anthropic"


@pytest.mark.parametrize("url", [
    "https://generativelanguage.googleapis.com/v1beta/openai",
    "https://api.groq.com/openai/v1",
])
def test_open_real_records_accept_the_owner_approved_hosts(url):
    cfg = _cfg(CARE_REAL_RECORDS="on", OPENAI_BASE_URL=url)
    assert cfg.real_records == "on"


def test_anthropic_serving_ignores_an_unused_openai_base():
    # The rule keys on the provider that will serve chat. With an Anthropic
    # credential set, the OpenAI base is never called.
    cfg = _cfg(CARE_REAL_RECORDS="on", ANTHROPIC_API_KEY="a",
               OPENAI_BASE_URL=FOREIGN)
    assert cfg.provider == "anthropic"


@pytest.mark.parametrize("mode", ["on", "allowlist"])
def test_open_real_records_refuse_a_foreign_host(mode):
    with pytest.raises(ConfigError) as exc:
        _cfg(CARE_REAL_RECORDS=mode, OPENAI_BASE_URL=FOREIGN)
    msg = str(exc.value)
    assert "llm.example-provider.test" in msg
    assert "CARE_REAL_RECORDS_MODEL_HOSTS" in msg
    # Host only: not the URL, never the key.
    assert "/v1beta" not in msg
    assert "sk-test-secret" not in msg


def test_anthropic_base_url_redirect_is_refused():
    # The Anthropic SDK reads ANTHROPIC_BASE_URL from the environment, so
    # "Anthropic" is only vetted while that is not pointed elsewhere.
    with pytest.raises(ConfigError, match="proxy.example.test"):
        _cfg(CARE_REAL_RECORDS="on", ANTHROPIC_API_KEY="a",
             ANTHROPIC_BASE_URL="https://proxy.example.test")
    assert _cfg(CARE_REAL_RECORDS="on", ANTHROPIC_API_KEY="a",
                ANTHROPIC_BASE_URL="https://api.anthropic.com"
                ).provider == "anthropic"


def test_a_host_named_in_the_env_var_is_accepted():
    cfg = _cfg(CARE_REAL_RECORDS="on", OPENAI_BASE_URL=FOREIGN,
               CARE_REAL_RECORDS_MODEL_HOSTS=(
                   " other.example.test, Llm.Example-Provider.TEST ,"))
    assert cfg.real_records == "on"


@pytest.mark.parametrize("url", [
    "https://api.openai.com.evil.test/v1",
    "https://evil-api.openai.com/v1",
    "https://api.openai.com@evil.test/v1",
    "https://evil.test/api.openai.com/v1",
    "https://openai.com/v1",
    "api.openai.com/v1",
])
def test_host_match_is_exact_on_hostname(url):
    with pytest.raises(ConfigError):
        _cfg(CARE_REAL_RECORDS="on", OPENAI_BASE_URL=url)


def test_listed_host_is_matched_exactly_too():
    with pytest.raises(ConfigError):
        _cfg(CARE_REAL_RECORDS="on",
             OPENAI_BASE_URL="https://googleapis.com.evil.test/v1",
             CARE_REAL_RECORDS_MODEL_HOSTS="googleapis.com")
