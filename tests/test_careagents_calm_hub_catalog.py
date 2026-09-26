"""The connector menu's words (calm hub spec section 4 and 7)."""

from __future__ import annotations

import json

import pytest

from careagents import connectors
from careagents.config import Config
from tests.test_careagents import FakeClient

OPERATOR_PHRASES = ("not configured", "deployment", "sidecar", "wired")

CONFIGS = [
    {},
    {"FASTEN_PUBLIC_KEY": "pub"},
    {"CARE_WEARABLES_ENABLED": "1"},
    {"FASTEN_PUBLIC_KEY": "pub", "CARE_WEARABLES_ENABLED": "1"},
]


def _cfg(**env):
    base = {"CARE_DATABASE_URL": "sqlite:///:memory:",
            "OPENAI_API_KEY": "k", "HEALTHCLAW_MINT_SECRET": "m"}
    return Config(env={**base, **env})


def _by_id(cfg, real_records):
    return {m["id"]: m for m in
            connectors.catalog(cfg, real_records=real_records)}


@pytest.mark.parametrize("env", CONFIGS)
@pytest.mark.parametrize("real_records", [False, True])
def test_catalog_output_contains_no_operator_phrases(env, real_records):
    text = json.dumps(connectors.catalog(
        _cfg(**env), real_records=real_records)).lower()
    for phrase in OPERATOR_PHRASES:
        assert phrase not in text, phrase


def test_a_refused_start_or_refresh_names_no_operator_detail():
    cfg, fake = _cfg(), FakeClient()      # no Fasten key, wearables off
    said = [
        connectors.start("fasten", None, cfg, fake, real_records=True)["error"],
        connectors.refresh("fasten", "ca-1", None, cfg, fake)["error"],
        connectors.refresh("wearable", "ca-1", "apple", cfg, fake)["reason"],
    ]
    for line in said:
        for phrase in OPERATOR_PHRASES:
            assert phrase not in line.lower(), line


def test_the_closed_menu_offers_only_the_sample():
    items = _by_id(_cfg(FASTEN_PUBLIC_KEY="pub"), real_records=False)
    assert items["sample"]["chip"] == "Available"
    for sid in connectors.REAL_RECORD_SOURCES:
        assert items[sid]["chip"] == "Coming soon", sid


def test_the_open_menu_marks_phase_one_sources_available():
    items = _by_id(_cfg(FASTEN_PUBLIC_KEY="pub"), real_records=True)
    assert items["fasten"]["chip"] == "Available"
    assert items["direct"]["chip"] == "Available"
    for sid in ("hbo", "healthex", "shl", "wearable"):
        assert items[sid]["chip"] == "Coming soon", sid


def test_every_source_sits_in_its_named_group():
    groups = {m["id"]: m["group"] for m in
              connectors.catalog(_cfg(), real_records=True)}
    assert groups == {"sample": "sample", "fasten": "find",
                      "hbo": "services", "healthex": "services",
                      "direct": "file", "shl": "file",
                      "wearable": "devices"}
    names = dict(connectors.GROUPS)
    assert list(names) == ["find", "services", "file", "devices"]
    assert names["find"] == "Find my records"


def test_patient_copy_uses_the_spec_words_and_no_em_dash():
    open_items = _by_id(_cfg(FASTEN_PUBLIC_KEY="pub"), real_records=True)
    assert open_items["sample"]["label"] == "Explore with made-up records"
    assert open_items["sample"]["blurb"] == "Made-up records to explore safely."
    assert open_items["fasten"]["label"] == (
        "Find my records at my doctor or hospital")
    assert open_items["direct"]["label"] == (
        "Upload a file from your patient portal")
    everything = list(open_items.values()) + connectors.catalog(
        _cfg(), real_records=False)
    for m in everything:
        assert "—" not in m["label"] + m["blurb"], m["id"]
        assert "no signup" not in m["blurb"].lower(), m["id"]
