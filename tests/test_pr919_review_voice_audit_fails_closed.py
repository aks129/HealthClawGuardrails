"""#919 security review: the sample-voiced intake review fails closed when
its audit write fails, exactly as the plain one does (V2). The voice is
asked for with the internal secret, so this is the path CareAgents takes on
made-up records. Synthetic data only.
"""

from __future__ import annotations

import pytest

from r6.audit import AuditWriteError
from tests.test_review_sample_voice import (SECRET, form_action,  # noqa: F401
                                            intake_ready)


@pytest.mark.parametrize("query,secret", [("", None),
                                          ("?voice=sample", SECRET)],
                         ids=["plain", "sample-voice"])
def test_an_audit_failure_blocks_the_review(client, auth_headers, form_action,  # noqa: F811
                                            monkeypatch, query, secret):
    import r6.actions.review as review

    def boom(*_a, **_k):
        raise AuditWriteError("forced")
    monkeypatch.setattr(review, "record_audit_event", boom)
    headers = dict(auth_headers)
    if secret:
        headers["X-Internal-Secret"] = secret
    # The test app propagates exceptions: the raise reaching the client is
    # the proof no page was rendered or returned.
    with pytest.raises(AuditWriteError):
        client.get(f"/r6/actions/{form_action}/review{query}",
                   headers=headers)
