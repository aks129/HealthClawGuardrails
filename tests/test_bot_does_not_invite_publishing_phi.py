"""The demo bot does not tell users a shared tenant is their own (#459).

Raised by Dr. Magan on 2026-08-09 as a wording problem: the /start banner said
"You're accessing your own records here; by continuing you accept that for
your own data." It reads oddly on the synthetic tenant, she said. It read
oddly because every clause was false.

    what it said              what is true
    "your own records"        one tenant shared by every user of the bot
    implied private           world-readable, no credentials needed
    "for your own data"       consent framed around data the user does not
                              separately own here

And the consequence is larger than the wording, because /connect pulls REAL
records via Fasten into that tenant. A user who believed the banner and ran
/connect would have published their medical history to a tenant anyone can
read anonymously — having agreed on the strength of a sentence that was not
true.

cmd_start already carried the right rule three lines above the defect:
"offering a privacy control that does nothing leaves the user worse off than
saying nothing, because they choose to continue on the strength of it." That
rule governs statements of fact, not just feature lists.

Two halves, and the second is the one that matters:
  - the banner says what is true
  - /connect REFUSES when the tenant is world-readable, because a banner is a
    sentence someone can skip
"""

import importlib
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BOT = ROOT / 'openclaw' / 'bot.py'


def _source():
    return BOT.read_text(encoding='utf-8')


class TestTheBannerNoLongerLies:

    def test_the_ownership_claim_is_gone(self):
        """MUTATION: restore the 'your own records' sentence -> red."""
        src = _source()
        for claim in ('accessing your own records',
                      'accept that for your own data'):
            assert claim not in src, (
                f'the /start banner claims {claim!r} on a tenant shared by '
                'every user of the bot (#459)')

    def test_the_banner_says_the_tenant_is_shared(self):
        """Replacing a false claim with silence is not the fix. The user is
        making a decision and needs the fact the decision turns on."""
        src = _source()
        assert 'shared by everyone using it' in src, (
            'the banner no longer tells the user the tenant is shared')


class TestConnectRefusesToPublishPHI:

    def test_connect_checks_before_it_offers(self):
        """MUTATION: delete the readable check from cmd_connect -> red.

        Asserted on cmd_connect's own body rather than the whole file, so a
        probe that exists but is never called cannot satisfy this.
        """
        src = _source()
        body = src.split('async def cmd_connect(', 1)[1].split('\nasync def ', 1)[0]
        assert 'tenant_is_world_readable()' in body, (
            'cmd_connect offers connection options without checking whether '
            'the tenant is world-readable')
        assert 'return' in body.split('tenant_is_world_readable()', 1)[1][:400], (
            'cmd_connect checks readability but does not refuse on it')

    def test_an_unknown_answer_refuses_too(self):
        """The fail-closed half.

        `tenant_is_world_readable` returns None when it cannot reach the
        server. Treating None as "private" would mean an outage becomes
        permission to publish someone's medical history — a check that
        examined nothing answering as though it had (defect catalogue §0).

        MUTATION: change `if readable is not False:` to `if readable:` -> red.
        """
        src = _source()
        body = src.split('async def cmd_connect(', 1)[1].split('\nasync def ', 1)[0]
        assert 'readable is not False' in body, (
            'cmd_connect treats an undetermined answer as safe; only an '
            'explicit False means credentials are required')


class TestTheProbeObservesRatherThanAssumes:

    @pytest.fixture
    def bot(self, monkeypatch):
        """Import bot.py with its telegram dependencies stubbed.

        The module is deployed alone (openclaw/Dockerfile copies bot.py), so
        it is imported here the same way — for its own logic, not the bot
        framework's.
        """
        # A PACKAGE, not a bare module: bot.py does `from telegram.error
        # import Conflict`, and Python refuses a submodule import on a
        # module with no __path__.
        for name in ('telegram', 'telegram.ext', 'telegram.constants',
                     'telegram.error'):
            mod = types.ModuleType(name)
            mod.__path__ = []
            monkeypatch.setitem(sys.modules, name, mod)
        tg = sys.modules['telegram']
        tg.ext = sys.modules['telegram.ext']
        tg.error = sys.modules['telegram.error']
        tg.constants = sys.modules['telegram.constants']
        sys.modules['telegram.error'].Conflict = type('Conflict', (Exception,), {})
        for attr in ('Update', 'InlineKeyboardButton', 'InlineKeyboardMarkup',
                     'BotCommand'):
            setattr(tg, attr, type(attr, (), {}))
        ext = sys.modules['telegram.ext']
        for attr in ('Application', 'CommandHandler', 'MessageHandler',
                     'filters', 'CallbackQueryHandler', 'CallbackContext',
                     'JobQueue'):
            setattr(ext, attr, type(attr, (), {}))
        # Used as an annotation (ContextTypes.DEFAULT_TYPE) throughout, so it
        # is dereferenced at def time, not call time.
        ext.ContextTypes = type('ContextTypes', (), {'DEFAULT_TYPE': object})
        # bot.py reads os.environ['TELEGRAM_BOT_TOKEN'] at import — it is a
        # deployed script, not a library, and failing loudly without a token
        # is correct for it. Supply one; the tests below never send anything.
        monkeypatch.setenv('TELEGRAM_BOT_TOKEN', 'test-token-not-real')
        # 'bot' itself goes through monkeypatch too. Without this the
        # stub-built module survives in sys.modules and the NEXT file to
        # import bot.py gets one wired to fake telegram classes —
        # tests/test_bot_singleton_hardening.py went red four ways before
        # this line existed. A fixture that leaks its doubles into the rest
        # of the suite is worse than no fixture: the failure lands somewhere
        # else, on someone else.
        previous = sys.modules.pop('bot', None)
        sys.path.insert(0, str(ROOT / 'openclaw'))
        try:
            yield importlib.import_module('bot')
        finally:
            sys.path.remove(str(ROOT / 'openclaw'))
            sys.modules.pop('bot', None)
            if previous is not None:
                sys.modules['bot'] = previous

    @pytest.mark.parametrize('status,expected', [
        (200, True),    # a stranger read it
        (401, False),   # credentials required
        (403, False),
        (500, None),    # cannot tell
    ])
    def test_it_reads_the_answer_the_server_gives(self, bot, monkeypatch,
                                                  status, expected):
        class Resp:
            status_code = status
        monkeypatch.setattr(bot.requests, 'get', lambda *a, **k: Resp())
        assert bot.tenant_is_world_readable() is expected

    def test_a_network_failure_is_undetermined_not_private(self, bot,
                                                           monkeypatch):
        """The direction of this default is the whole point."""
        def boom(*a, **k):
            raise OSError('no route to host')
        monkeypatch.setattr(bot.requests, 'get', boom)
        assert bot.tenant_is_world_readable() is None

    def test_the_probe_sends_no_credentials(self, bot, monkeypatch):
        """It must make the request a stranger would make. Sending a token
        would answer a question nobody asked — whether WE can read it."""
        seen = {}

        class Resp:
            status_code = 401

        def capture(*args, **kwargs):
            seen.update(kwargs)
            return Resp()
        monkeypatch.setattr(bot.requests, 'get', capture)
        bot.tenant_is_world_readable()
        headers = {k.lower() for k in (seen.get('headers') or {})}
        assert 'authorization' not in headers
        assert 'x-step-up-token' not in headers
