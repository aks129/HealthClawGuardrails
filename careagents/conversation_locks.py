"""Per-conversation turn serialization.

Redis is the cross-worker lock in production. A process-local lock remains the
development/test fallback and also prevents duplicate work inside one worker.
Lock keys contain only a SHA-256 digest, never tenant or conversation text.
"""

from __future__ import annotations

import hashlib
import logging
from contextlib import contextmanager
from threading import Lock

logger = logging.getLogger(__name__)


class ConversationLockError(RuntimeError):
    pass


class ConversationTurnLocks:
    def __init__(self, redis_url: str = "", production: bool = False):
        self._guard = Lock()
        self._local: dict[str, tuple[Lock, int]] = {}
        self._production = production
        self._redis = None
        if redis_url:
            import redis
            self._redis = redis.Redis.from_url(redis_url, decode_responses=True)

    def _local_lock(self, key: str) -> Lock:
        with self._guard:
            lock, users = self._local.get(key, (Lock(), 0))
            self._local[key] = (lock, users + 1)
            return lock

    def _release_local(self, key: str, lock: Lock) -> None:
        with self._guard:
            current, users = self._local[key]
            if current is lock and users == 1:
                self._local.pop(key)
            else:
                self._local[key] = (current, users - 1)

    @staticmethod
    def _redis_key(key: str) -> str:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return f"healthclaw:careagents:conversation-lock:{digest}"

    @contextmanager
    def hold(self, key: str):
        local = self._local_lock(key)
        try:
            with local:
                distributed = None
                if self._redis is not None:
                    try:
                        distributed = self._redis.lock(
                            self._redis_key(key), timeout=600,
                            blocking_timeout=30)
                        if not distributed.acquire(blocking=True):
                            raise ConversationLockError(
                                "conversation is busy; retry shortly")
                    except ConversationLockError:
                        raise
                    except Exception as exc:  # noqa: BLE001 - Redis errors vary
                        if self._production:
                            raise ConversationLockError(
                                "shared conversation lock is unavailable") from exc
                        logger.warning(
                            "Redis conversation lock unavailable; using local lock")
                        distributed = None
                try:
                    yield
                finally:
                    if distributed is not None:
                        try:
                            distributed.release()
                        except Exception as exc:  # noqa: BLE001 - Redis errors vary
                            logger.error(
                                "could not release conversation lock: %s",
                                type(exc).__name__)
        finally:
            self._release_local(key, local)
