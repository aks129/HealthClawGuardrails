"""WSGI entry point: gunicorn 'careagents.wsgi:app'.

Run with ONE worker (threads for concurrency) — chat history is
process-local. See deploy/careagents/careagents.service.
"""

from careagents.app import create_app

app = create_app()
