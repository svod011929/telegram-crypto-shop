from __future__ import annotations

import json
from typing import Any

from bot.database.db import Database


class BroadcastService:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def create_draft(
        self,
        admin_telegram_id: int,
        *,
        text: str,
        photo_file_id: str | None = None,
        buttons: list[dict[str, str]] | None = None,
    ) -> int:
        if not text.strip() and not photo_file_id:
            raise ValueError("Рассылка не может быть пустой")
        limit = 1024 if photo_file_id else 4096
        if len(text) > limit:
            raise ValueError(f"Текст рассылки длиннее {limit} символов")
        normalized_buttons = buttons or []
        if len(normalized_buttons) > 8:
            raise ValueError("Можно добавить не более 8 кнопок")
        for button in normalized_buttons:
            title = str(button.get("text", ""))
            url = str(button.get("url", ""))
            if not 1 <= len(title) <= 64 or not url.startswith(
                ("https://", "http://", "tg://")
            ):
                raise ValueError("Некорректная кнопка рассылки")
        payload = {
            "text": text,
            "photo_file_id": photo_file_id,
            "buttons": normalized_buttons,
        }
        return await self.db.execute(
            "INSERT INTO broadcasts(created_by, payload_json) VALUES (?, ?)",
            (admin_telegram_id, json.dumps(payload, ensure_ascii=False)),
        )

    async def preview(self, broadcast_id: int) -> dict[str, Any] | None:
        row = await self.db.fetchone(
            "SELECT * FROM broadcasts WHERE id = ?", (broadcast_id,)
        )
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(str(row["payload_json"]))
        count = await self.db.fetchone(
            "SELECT COUNT(*) AS count FROM users WHERE banned = 0 AND bot_blocked = 0"
        )
        result["recipients"] = int(count["count"] if count else 0)
        return result

    async def launch(self, broadcast_id: int) -> int:
        async with self.db.transaction() as connection:
            cursor = await connection.execute(
                "SELECT * FROM broadcasts WHERE id = ? AND status = 'DRAFT'",
                (broadcast_id,),
            )
            broadcast = await cursor.fetchone()
            await cursor.close()
            if broadcast is None:
                raise ValueError("Рассылка не найдена или уже запущена")
            cursor = await connection.execute(
                "SELECT COUNT(*) AS count FROM users WHERE banned = 0 AND bot_blocked = 0"
            )
            count_row = await cursor.fetchone()
            await cursor.close()
            recipient_count = int(count_row["count"] if count_row else 0)
            base_payload = json.loads(str(broadcast["payload_json"]))
            base_payload["broadcast_id"] = broadcast_id
            encoded = json.dumps(base_payload, ensure_ascii=False)
            await connection.execute(
                "INSERT OR IGNORE INTO outbox(dedupe_key, kind, chat_id, payload_json) "
                "SELECT 'broadcast:' || ? || ':user:' || telegram_id, 'BROADCAST', telegram_id, ? "
                "FROM users WHERE banned = 0 AND bot_blocked = 0",
                (broadcast_id, encoded),
            )
            await connection.execute(
                "UPDATE broadcasts SET status = 'RUNNING', total_count = ?, started_at = CURRENT_TIMESTAMP WHERE id = ?",
                (recipient_count, broadcast_id),
            )
            if recipient_count == 0:
                await connection.execute(
                    "UPDATE broadcasts SET status = 'COMPLETED', completed_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (broadcast_id,),
                )
            return recipient_count

    async def cancel(self, broadcast_id: int) -> None:
        async with self.db.transaction() as connection:
            await connection.execute(
                "UPDATE broadcasts SET status = 'CANCELLED', completed_at = CURRENT_TIMESTAMP "
                "WHERE id = ? AND status IN ('DRAFT', 'RUNNING')",
                (broadcast_id,),
            )
            await connection.execute(
                "UPDATE outbox SET status = 'FAILED', last_error = 'Broadcast cancelled' "
                "WHERE kind = 'BROADCAST' AND json_extract(payload_json, '$.broadcast_id') = ? AND status = 'PENDING'",
                (broadcast_id,),
            )
