"""
Flask application routes for HealthClaw Guardrails.

Web UI routes:
- / (landing page)
- /r6-dashboard (Health Data Dashboard — FHIR interactive showcase)
- /faq (Frequently Asked Questions)
- /wiki (Project Wiki)
"""

from flask import render_template
from main import app


@app.route('/')
def index():
    """Landing page."""
    return render_template('index.html')


@app.route('/r6-dashboard')
def r6_dashboard():
    """Health Data Dashboard (FHIR) — interactive guardrail showcase."""
    return render_template('r6_dashboard.html')


@app.route('/faq')
def faq():
    """Frequently Asked Questions."""
    return render_template('faq.html')


@app.route('/wiki')
def wiki():
    """Project Wiki — architecture, concepts, and how-tos."""
    return render_template('wiki.html')
