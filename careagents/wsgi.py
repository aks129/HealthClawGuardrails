"""WSGI entry point: gunicorn 'careagents.wsgi:app'.

The WSGI process only authenticates, enqueues, and replays durable run events.
Inference and tools execute in ``python -m careagents.worker``.
"""

from careagents.app import create_app

app = create_app()
