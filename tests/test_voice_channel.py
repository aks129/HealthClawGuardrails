"""The engine accepts `voice` as a conversation channel (voice PR 1).

CareAgents will send surface="voice" for a turn that started as speech, so
an audit reader can tell it from a typed web turn. The value is a closed
literal, so the audit detail stays PHI-free. Synthetic data only.
"""

from __future__ import annotations

import pytest


def _post(app, client, **body):
    from r6.stepup import generate_step_up_token
    with app.app_context():
        token = generate_step_up_token("tenant-a")
    return client.post("/command-center/api/conversations",
                       headers={"X-Step-Up-Token": token},
                       json={"tenant_id": "tenant-a", "role": "user",
                             "text": "hi", **body})


def test_voice_is_a_known_channel():
    from r6.command_center.models import KNOWN_CHANNELS, audit_channel
    assert "voice" in KNOWN_CHANNELS
    assert audit_channel("voice") == "voice"


def test_a_voice_turn_is_accepted_and_keeps_its_channel(app, client):
    r = _post(app, client, surface="voice")
    assert r.status_code == 201, r.get_data(as_text=True)
    assert r.get_json()["channel"] == "voice"


@pytest.mark.parametrize("value", ["Voice", "voice ", "VOICE", "voice\n"])
def test_near_misses_of_voice_are_still_refused(app, client, value):
    assert _post(app, client, surface=value).status_code == 400
