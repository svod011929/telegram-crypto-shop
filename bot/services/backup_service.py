from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bot.database.db import Database
from bot.services.settings_service import SettingsService

logger = logging.getLogger(__name__)


class BackupService:
    def __init__(
        self, db: Database, settings: SettingsService, directory: Path | str = "backups"
    ) -> None:
        self.db = db
        self.settings = settings
        self.directory = Path(directory)
        self._stop = asyncio.Event()

    async def create(self, created_by: int | None = None) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(UTC).strftime("%Y-%m-%d_%H%M%S_%f")
        target = self.directory / f"bot_{timestamp}.db"
        await self.db.backup_to(target)
        check = await asyncio.to_thread(self._integrity_check, target)
        if check != "ok":
            target.unlink(missing_ok=True)
            raise RuntimeError(f"Backup integrity check failed: {check}")
        await self.db.execute(
            "INSERT INTO backups(filename, size_bytes, created_by) VALUES (?, ?, ?)",
            (target.name, target.stat().st_size, created_by),
        )
        await self.cleanup()
        logger.info("Created SQLite backup %s", target.name)
        return target

    @staticmethod
    def _integrity_check(path: Path) -> str:
        import sqlite3

        connection = sqlite3.connect(path)
        try:
            row = connection.execute("PRAGMA integrity_check").fetchone()
            return str(row[0]) if row else "unknown"
        finally:
            connection.close()

    async def cleanup(self) -> None:
        retention = await self.settings.get_int("backup_retention", 10)
        rows = await self.db.fetchall("SELECT * FROM backups ORDER BY id DESC")
        for row in rows[retention:]:
            path = self.directory / str(row["filename"])
            path.unlink(missing_ok=True)
            await self.db.execute("DELETE FROM backups WHERE id = ?", (int(row["id"]),))

    async def list(self, limit: int = 10) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT * FROM backups ORDER BY id DESC LIMIT ?", (limit,)
        )
        return [dict(row) for row in rows]

    async def run(self) -> None:
        logger.info("Backup worker started")
        while not self._stop.is_set():
            hours = await self.settings.get_int("backup_interval_hours", 24)
            row = await self.db.fetchone(
                "SELECT id FROM backups WHERE created_at >= datetime('now', ?) LIMIT 1",
                (f"-{hours} hours",),
            )
            if row is None:
                try:
                    await self.create()
                except Exception:
                    logger.exception("Automatic backup failed")
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=min(3600, max(60, hours * 900))
                )
            except TimeoutError:
                continue
        logger.info("Backup worker stopped")

    def stop(self) -> None:
        self._stop.set()
