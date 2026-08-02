"""WSGI entry point: gunicorn 'careagents.wsgi:app'.

Conversation turns use a Redis-backed distributed lock when ``REDIS_URL`` is
configured, with a process-local fallback for single-worker development.
"""

from careagents.app import create_app

app = create_app()
