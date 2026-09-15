"""/health says which commit it runs (#703 deploy packet).

Flask auto-deploys from main while the other services deploy by hand, so
"healthy" at app.healthclaw.io said nothing about WHICH main. CareAgents
already answers this (careagents/_build.py, #258); Railway injects
RAILWAY_GIT_COMMIT_SHA on the Flask service, so the same question has an
answer there too — and "unknown" where it does not, never a guess.
"""


def test_health_reports_the_injected_commit(client, monkeypatch):
    monkeypatch.setenv('RAILWAY_GIT_COMMIT_SHA',
                       '136cd0faba6eb2e533c65b9990c65a8cc7e7587d')
    body = client.get('/r6/fhir/health').get_json()
    assert body['build'] == '136cd0faba6e'


def test_health_says_unknown_where_nothing_is_injected(client, monkeypatch):
    monkeypatch.delenv('RAILWAY_GIT_COMMIT_SHA', raising=False)
    body = client.get('/r6/fhir/health').get_json()
    assert body['build'] == 'unknown'
    assert body['status'] == 'healthy'
