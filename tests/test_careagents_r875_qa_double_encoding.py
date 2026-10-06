"""QA #875: the signed-URL strip decodes twice, as its comment says.

`_is_signed_url` tests each token on `unquote(unquote(token))` "for a double
encoding". Only a single encoding was pinned, so dropping the second decode
left the suite green. Synthetic URLs only.
"""

from __future__ import annotations

import pytest

from careagents import imessage


@pytest.mark.parametrize("url", [
    "https://hc.example/r6/sdc/%2564ocuments/d?t=x",     # documents, twice
    "https://hc.example/x.pdf?t=x&%2573ig=abc",           # sig, twice
])
def test_a_double_encoded_signed_url_is_stripped(url):
    assert imessage.strip_signed_urls(f"Here: {url} done.") == "Here: done."


def test_double_decoding_does_not_eat_ordinary_percent_text():
    text = "A1c fell 10%25 in a year, 100% sure."
    assert imessage.strip_signed_urls(text) == text
