from __future__ import annotations

import asyncio
import time


class Cooldowns:
    """Small in-memory anti-spam guard; financial idempotency remains in SQLite."""

    def __init__(self) -> None:
        self._values: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def allow(self, key: str, seconds: float) -> bool:
        now = time.monotonic()
        async with self._lock:
            deadline = self._values.get(key, 0.0)
            if deadline > now:
                return False
            self._values[key] = now + seconds
            if len(self._values) > 10_000:
                self._values = {
                    stored_key: value
                    for stored_key, value in self._values.items()
                    if value > now
                }
            return True
