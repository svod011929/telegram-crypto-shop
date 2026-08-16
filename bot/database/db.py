from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator, Iterable, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite

from bot.database.migrations import apply_migrations


class Database:
    """A single WAL-enabled SQLite connection with serialized transactions."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._connection: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    @property
    def connection(self) -> aiosqlite.Connection:
        if self._connection is None:
            raise RuntimeError("Database is not connected")
        return self._connection

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = await aiosqlite.connect(self.path, timeout=30)
        connection.row_factory = aiosqlite.Row
        try:
            for pragma in (
                "PRAGMA journal_mode=WAL",
                "PRAGMA foreign_keys=ON",
                "PRAGMA busy_timeout=30000",
                "PRAGMA synchronous=NORMAL",
            ):
                cursor = await connection.execute(pragma)
                await cursor.close()
            await apply_migrations(connection)
        except BaseException:
            await connection.close()
            raise
        self._connection = connection

    async def close(self) -> None:
        if self._connection is not None:
            async with self._lock:
                await self._connection.close()
                self._connection = None

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        async with self._lock:
            connection = self.connection
            await connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise

    async def fetchone(
        self, query: str, params: Sequence[Any] = ()
    ) -> aiosqlite.Row | None:
        async with self._lock:
            cursor = await self.connection.execute(query, params)
            row = await cursor.fetchone()
            await cursor.close()
            return row

    async def fetchall(
        self, query: str, params: Sequence[Any] = ()
    ) -> list[aiosqlite.Row]:
        async with self._lock:
            cursor = await self.connection.execute(query, params)
            rows = await cursor.fetchall()
            await cursor.close()
            return list(rows)

    async def execute(self, query: str, params: Sequence[Any] = ()) -> int:
        async with self._lock:
            cursor: aiosqlite.Cursor | None = None
            try:
                cursor = await self.connection.execute(query, params)
                row_id = int(cursor.lastrowid or 0)
                await self.connection.commit()
                return row_id
            except BaseException:
                await self.connection.rollback()
                raise
            finally:
                if cursor is not None:
                    await cursor.close()

    async def executemany(self, query: str, params: Iterable[Sequence[Any]]) -> None:
        async with self._lock:
            cursor: aiosqlite.Cursor | None = None
            try:
                cursor = await self.connection.executemany(query, params)
                await self.connection.commit()
            except BaseException:
                await self.connection.rollback()
                raise
            finally:
                if cursor is not None:
                    await cursor.close()

    async def backup_to(self, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        async with self._lock:
            cursor = await self.connection.execute("PRAGMA wal_checkpoint(FULL)")
            await cursor.close()
            destination = sqlite3.connect(target)
            try:
                await self.connection.backup(destination)
            finally:
                destination.close()

    async def integrity_check(self) -> str:
        row = await self.fetchone("PRAGMA integrity_check")
        return str(row[0]) if row else "unknown"
