"""Bounded admission gate with one in-flight request per user."""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass


@dataclass(frozen=True)
class Admission:
    accepted: bool
    message: str
    position: int = 0


class JobGate:
    def __init__(self, max_pending: int, cooldown_seconds: int) -> None:
        self.max_pending = max_pending
        self.cooldown_seconds = cooldown_seconds
        self._active: set[int] = set()
        self._last_accepted: dict[int, float] = {}
        self._lock = asyncio.Lock()
        self._processing = asyncio.Semaphore(1)

    async def admit(self, user_id: int) -> Admission:
        now = time.monotonic()
        async with self._lock:
            if user_id in self._active:
                return Admission(False, "У вас уже есть запись в обработке или очереди.")
            last = self._last_accepted.get(user_id)
            if last is not None and now - last < self.cooldown_seconds:
                return Admission(False, "Подождите несколько секунд перед следующей записью.")
            if len(self._active) >= self.max_pending:
                return Admission(False, "Сейчас слишком много запросов. Попробуйте немного позже.")
            position = len(self._active)
            self._active.add(user_id)
            self._last_accepted[user_id] = now
            # Keep recent cooldown entries without accumulating user IDs indefinitely.
            if len(self._last_accepted) > 10000:
                self._last_accepted = {
                    uid: ts for uid, ts in self._last_accepted.items()
                    if now - ts < max(60, self.cooldown_seconds)
                }
            return Admission(True, "", position)

    async def release(self, user_id: int) -> None:
        async with self._lock:
            self._active.discard(user_id)

    @asynccontextmanager
    async def processing_slot(self):
        async with self._processing:
            yield
