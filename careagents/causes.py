"""Why a chat turn failed, as one of a fixed set of codes (#926).

The owner needs to tell a model that is out of credit from an engine that is
down. The code is decided by the exception's class and HTTP status only,
never by its message: a message can carry what a patient asked, and a
classifier that reads it is one refactor away from logging it.
"""

from __future__ import annotations

from careagents import llm
from careagents.healthclaw import HealthClawError

MODEL_AUTH = "model_auth"            # the model host refused the key: 401/403
MODEL_QUOTA = "model_quota"          # out of credit, or rate limited past retry
MODEL_TIMEOUT = "model_timeout"
MODEL_OTHER = "model_other"
ENGINE_UNAVAILABLE = "engine_unavailable"   # no answer, 5xx, 408, 429
ENGINE_REFUSED = "engine_refused"           # any other 4xx
DEADLINE = "deadline"
INTERNAL = "internal"

CAUSES = (MODEL_AUTH, MODEL_QUOTA, MODEL_TIMEOUT, MODEL_OTHER,
          ENGINE_UNAVAILABLE, ENGINE_REFUSED, DEADLINE, INTERNAL)


def cause_of(exc: BaseException) -> str:
    """The cause code for `exc`. A run past its deadline is classified by
    the worker, which catches it on its own."""
    if isinstance(exc, llm.ModelHostNotApproved):
        return MODEL_AUTH       # the host is not authorised for real records
    if isinstance(exc, llm.LLMTimeout):
        return MODEL_TIMEOUT
    if isinstance(exc, (llm.LLMOutOfCredit, llm.LLMRateLimited)):
        return MODEL_QUOTA
    if isinstance(exc, llm.LLMError):
        return MODEL_AUTH if exc.status in (401, 403) else MODEL_OTHER
    if isinstance(exc, HealthClawError):
        status = exc.status or 0
        if 400 <= status < 500 and status not in (408, 429):
            return ENGINE_REFUSED
        return ENGINE_UNAVAILABLE
    return INTERNAL


def needs_owner(exc: BaseException) -> bool:
    """A failure only an operator can clear: the model key is invalid or out
    of credit. The tester is told the assistant is unavailable and the owner
    is emailed. A rate limit counts as model_quota but is not one of these:
    it clears by itself, and the tester is told to ask again in a moment."""
    return (isinstance(exc, llm.LLMOutOfCredit)
            or cause_of(exc) == MODEL_AUTH)
