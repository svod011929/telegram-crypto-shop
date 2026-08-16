from __future__ import annotations

import json
from typing import Any

from bot.database.db import Database


class UserService:
    def __init__(self, db: Database, superadmin_id: int) -> None:
        self.db = db
        self.superadmin_id = superadmin_id

    async def ensure_superadmin(self) -> None:
        await self.db.execute(
            "INSERT INTO admins(telegram_id, role, permissions_json, active) VALUES (?, 'SUPERADMIN', '{}', 1) "
            "ON CONFLICT(telegram_id) DO UPDATE SET role = 'SUPERADMIN', active = 1, updated_at = CURRENT_TIMESTAMP",
            (self.superadmin_id,),
        )

    async def register_or_update(
        self,
        telegram_id: int,
        username: str | None,
        first_name: str,
        language: str | None,
        referrer_telegram_id: int | None = None,
    ) -> tuple[dict[str, Any], bool]:
        async with self.db.transaction() as connection:
            cursor = await connection.execute(
                "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
            )
            existing = await cursor.fetchone()
            await cursor.close()
            if existing is not None:
                await connection.execute(
                    "UPDATE users SET username = ?, first_name = ?, language = ?, last_activity = CURRENT_TIMESTAMP, "
                    "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (username, first_name, language or "ru", int(existing["id"])),
                )
                cursor = await connection.execute(
                    "SELECT * FROM users WHERE id = ?", (int(existing["id"]),)
                )
                row = await cursor.fetchone()
                await cursor.close()
                return dict(row), False

            referrer_id: int | None = None
            if referrer_telegram_id and referrer_telegram_id != telegram_id:
                cursor = await connection.execute(
                    "SELECT id FROM users WHERE telegram_id = ? AND banned = 0",
                    (referrer_telegram_id,),
                )
                referrer = await cursor.fetchone()
                await cursor.close()
                if referrer is not None:
                    referrer_id = int(referrer["id"])
            cursor = await connection.execute(
                "INSERT INTO users(telegram_id, username, first_name, language, referrer_id) VALUES (?, ?, ?, ?, ?)",
                (telegram_id, username, first_name, language or "ru", referrer_id),
            )
            user_id = int(cursor.lastrowid)
            await cursor.close()
            if referrer_id is not None:
                await connection.execute(
                    "UPDATE users SET number_of_referrals = number_of_referrals + 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (referrer_id,),
                )
            cursor = await connection.execute(
                "SELECT * FROM users WHERE id = ?", (user_id,)
            )
            row = await cursor.fetchone()
            await cursor.close()
            return dict(row), True

    async def touch(self, telegram_id: int) -> None:
        await self.db.execute(
            "UPDATE users SET last_activity = CURRENT_TIMESTAMP, bot_blocked = 0 WHERE telegram_id = ?",
            (telegram_id,),
        )

    async def by_telegram_id(self, telegram_id: int) -> dict[str, Any] | None:
        row = await self.db.fetchone(
            "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
        )
        return dict(row) if row else None

    async def by_id(self, user_id: int) -> dict[str, Any] | None:
        row = await self.db.fetchone("SELECT * FROM users WHERE id = ?", (user_id,))
        return dict(row) if row else None

    async def find(self, query: str) -> list[dict[str, Any]]:
        clean = query.strip().lstrip("@")
        if clean.isdigit():
            rows = await self.db.fetchall(
                "SELECT * FROM users WHERE id = ? OR telegram_id = ? ORDER BY id LIMIT 20",
                (int(clean), int(clean)),
            )
        else:
            rows = await self.db.fetchall(
                "SELECT * FROM users WHERE username = ? COLLATE NOCASE ORDER BY id LIMIT 20",
                (clean,),
            )
        return [dict(row) for row in rows]

    async def is_admin(self, telegram_id: int, roles: set[str] | None = None) -> bool:
        row = await self.db.fetchone(
            "SELECT role FROM admins WHERE telegram_id = ? AND active = 1",
            (telegram_id,),
        )
        return bool(row and (roles is None or str(row["role"]) in roles))

    async def admin_role(self, telegram_id: int) -> str | None:
        row = await self.db.fetchone(
            "SELECT role FROM admins WHERE telegram_id = ? AND active = 1",
            (telegram_id,),
        )
        return str(row["role"]) if row else None

    async def add_admin(
        self, actor_telegram_id: int, telegram_id: int, role: str
    ) -> None:
        if actor_telegram_id != self.superadmin_id:
            raise PermissionError("Только SUPERADMIN может управлять администраторами")
        normalized = role.upper()
        if normalized not in {"ADMIN", "SUPPORT"}:
            raise ValueError("Доступные роли: ADMIN, SUPPORT")
        await self.db.execute(
            "INSERT INTO admins(telegram_id, role, permissions_json, active, created_by) VALUES (?, ?, ?, 1, ?) "
            "ON CONFLICT(telegram_id) DO UPDATE SET role = excluded.role, active = 1, updated_at = CURRENT_TIMESTAMP",
            (
                telegram_id,
                normalized,
                json.dumps({}, ensure_ascii=False),
                actor_telegram_id,
            ),
        )

    async def deactivate_admin(self, actor_telegram_id: int, telegram_id: int) -> None:
        if actor_telegram_id != self.superadmin_id:
            raise PermissionError("Только SUPERADMIN может управлять администраторами")
        if telegram_id == self.superadmin_id:
            raise ValueError("Нельзя отключить SUPERADMIN")
        await self.db.execute(
            "UPDATE admins SET active = 0, updated_at = CURRENT_TIMESTAMP WHERE telegram_id = ?",
            (telegram_id,),
        )

    async def set_banned(self, user_id: int, banned: bool) -> None:
        await self.db.execute(
            "UPDATE users SET banned = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (int(banned), user_id),
        )
