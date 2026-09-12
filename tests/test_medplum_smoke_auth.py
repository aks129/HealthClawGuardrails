"""Exercise CLI credential wiring, including the deliberately anonymous probe."""

import json
import sys
from types import SimpleNamespace

import pytest
import requests

from scripts import smoke_medplum


def test_cli_authenticates_reads_but_not_the_refusal_probe(monkeypatch):
    token = 'synthetic-test-credential'
    calls = []

    def response(status, body):
        return SimpleNamespace(status_code=status, json=lambda: body)

    def post(url, headers, data):
        calls.append(('POST', url, dict(headers)))
        assert json.loads(data)['resourceType'] == 'Patient'
        if len(calls) == 1:
            assert 'X-Step-Up-Token' not in headers
            assert 'Authorization' not in headers
            return response(401, {})
        assert headers['X-Step-Up-Token'] == token
        return response(201, {'resourceType': 'Patient', 'id': 'synthetic-patient'})

    def get(url, headers):
        calls.append(('GET', url, dict(headers)))
        if headers.get('X-Step-Up-Token') != token:
            return response(401, {})
        if '/AuditEvent?' in url:
            return response(200, {'resourceType': 'Bundle', 'total': 1})
        return response(200, {'resourceType': 'Patient', 'id': 'synthetic-patient',
                              '_source': 'upstream', 'name': [{'family': 'T.'}]})

    monkeypatch.setattr(requests, 'post', post)
    monkeypatch.setattr(requests, 'get', get)
    monkeypatch.setattr(sys, 'argv', ['smoke_medplum', '--base-url',
                                     'https://synthetic.invalid', '--tenant-id',
                                     'synthetic-tenant', '--step-up-token', token])
    with pytest.raises(SystemExit) as stopped:
        smoke_medplum.main()
    assert stopped.value.code == 0
    assert len(calls) == 4
    assert all(h['X-Tenant-Id'] == 'synthetic-tenant' for _, _, h in calls)
    assert all(h.get('X-Step-Up-Token') == token for method, _, h in calls
               if method == 'GET')
