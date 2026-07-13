"""
Vercel serverless entry point.

Wraps the Flask WSGI app for Vercel's Python runtime.
All routes are handled by Flask — Vercel just proxies to this handler.

STATEFUL WRITES ARE REFUSED HERE: this copy runs with ephemeral serverless
SQLite, so any write it accepted would be silently lost (audit finding
2026-07-08). The stateful deployment is Railway at app.healthclaw.io; this
surface serves marketing pages and read-only discovery only.
"""

import sys
import os

# Ensure project root is on the Python path so imports resolve
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Import the fully-configured Flask app
from main import app  # noqa: E402

_STATEFUL_PREFIXES = ('/r6/', '/fasten/', '/shc/', '/actions/', '/wearables/',
                      '/command-center', '/email/')
_MUTATING = frozenset({'POST', 'PUT', 'PATCH', 'DELETE'})
_STATEFUL_HOST = 'https://app.healthclaw.io'


def _is_state_mutating_get(path):
    parts = path.strip('/').split('/')
    return (
        len(parts) == 4
        and parts[:2] == ['fasten', 'connections']
        and parts[3] == 'agent-access'
    ) or (
        len(parts) == 3
        and parts[:2] == ['r6', 'actions']
    )


def _refuse_serverless_writes():
    """On Vercel (VERCEL=1 is set by the platform), mutating requests to
    stateful paths get 405 + a pointer instead of a silent no-op write."""
    if not os.environ.get('VERCEL'):
        return None
    from flask import request, jsonify
    mutates = (
        request.method in _MUTATING
        and request.path.startswith(_STATEFUL_PREFIXES)
    ) or (
        request.method == 'GET' and _is_state_mutating_get(request.path)
    )
    if mutates:
        return jsonify({
            'error': 'read-only deployment',
            'detail': ('This host serves the marketing site with ephemeral '
                       'storage; writes are not persisted here.'),
            'use': _STATEFUL_HOST + request.path,
        }), 405
    return None


try:
    app.before_request(_refuse_serverless_writes)
except AssertionError:
    # Late import after the app has served a request (only happens in the
    # test suite; Vercel cold starts always import before the first request).
    pass

# Vercel expects the WSGI app as `app`
# (the variable name must match what vercel.json references)
