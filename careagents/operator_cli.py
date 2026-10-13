"""Operator commands for the invited-tester stage (beta spec 4.5, 4.6).

Run as `flask --app careagents.wsgi <command>` where CARE_DATABASE_URL
points at the database to change. Output goes to the operator's terminal
only. Nothing here writes an email address to the application log. The
`invites` group (spec 4.2) lives in app.py, next to `page-views`.
"""

from __future__ import annotations

import click

from careagents import beta


def register(app, svc) -> None:
    @app.cli.group("records")
    def records():
        """Pause or resume one account's records."""

    @records.command("pause")
    @click.argument("email")
    def records_pause(email):
        """Stop the account's chat turns that CareAgents runs (web chat and
        the iMessage relay), new real connections, new MCP grants, refresh,
        upload, and approvals. Does NOT stop engine-side ingest, an existing
        MCP grant, or a chat answered by an external gateway such as the
        Telegram bot."""
        if not svc.set_paused(email, True):
            raise click.ClickException("no account with that email")
        click.echo("paused")

    @records.command("resume")
    @click.argument("email")
    def records_resume(email):
        """Undo `records pause`."""
        if not svc.set_paused(email, False):
            raise click.ClickException("no account with that email")
        click.echo("resumed")

    @app.cli.group("assistants")
    def assistants():
        """How many assistants accounts hold."""

    @assistants.command("count")
    def assistants_count():
        """Accounts with none, one, or more than one assistant: the ones
        that see the legacy rendering (one assistant per person spec,
        section 4). Counts only, no ids."""
        n = svc.assistant_counts()
        click.echo(f"accounts: {n['accounts']}")
        click.echo(f"accounts with no assistant: {n['none']}")
        click.echo(f"accounts with one assistant: {n['one']}")
        click.echo(f"accounts with more than one assistant: {n['several']}")

    @app.cli.command("weekly-counts")
    @click.option("--weeks", default=4, show_default=True)
    def weekly(weeks):
        """The weekly number: counts only, one row per ISO week (UTC)."""
        click.echo("week      signed_up  real_connected  asked  approved")
        for r in beta.weekly_counts(svc.session, weeks=weeks):
            click.echo(f"{r['week']}  {r['signed_up']:>9}  "
                       f"{r['real_connected']:>14}  {r['asked']:>5}  "
                       f"{r['approved']:>8}")
