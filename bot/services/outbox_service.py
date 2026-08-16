from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from bot.database.db import Database
from bot.services.settings_service import SettingsService
from bot.utils.money import format_money

logger = logging.getLogger(__name__)


class OutboxWorker:
    def __init__(self, db: Database, bot: Bot, settings: SettingsService) -> None:
        self.db = db
        self.bot = bot
        self.settings = settings
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        logger.info("Outbox worker started")
        while not self._stop.is_set():
            worked: str | None = None
            try:
                worked = await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Outbox worker tick failed")
            if worked == "BROADCAST":
                rate = await self.settings.get_int("broadcast_rate_per_second", 20)
                timeout = 1 / max(1, min(25, rate))
            else:
                timeout = 0.05 if worked else 2.0
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=timeout)
            except TimeoutError:
                continue
        logger.info("Outbox worker stopped")

    async def tick(self) -> str | None:
        async with self.db.transaction() as connection:
            cursor = await connection.execute(
                "SELECT * FROM outbox WHERE status IN ('PENDING', 'SENDING') "
                "AND available_at <= CURRENT_TIMESTAMP "
                "ORDER BY CASE kind WHEN 'ORDER_DELIVERY' THEN 0 WHEN 'BROADCAST' THEN 2 ELSE 1 END, id LIMIT 1"
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row is None:
                return None
            cursor = await connection.execute(
                "UPDATE outbox SET status = 'SENDING', attempts = attempts + 1, "
                "available_at = datetime('now', '+5 minutes') WHERE id = ? "
                "AND status IN ('PENDING', 'SENDING')",
                (int(row["id"]),),
            )
            claimed = cursor.rowcount == 1
            await cursor.close()
            if not claimed:
                return "CLAIMED"
            payload = json.loads(str(row["payload_json"]))
            if str(row["kind"]) == "ORDER_DELIVERY":
                cursor = await connection.execute(
                    "UPDATE orders SET delivery_status = 'SENDING', updated_at = CURRENT_TIMESTAMP "
                    "WHERE id = ? AND status != 'REFUNDED' "
                    "AND delivery_status IN ('PENDING', 'FAILED', 'SENDING', 'UNKNOWN')",
                    (int(payload["order_id"]),),
                )
                delivery_claimed = cursor.rowcount == 1
                await cursor.close()
                if not delivery_claimed:
                    await connection.execute(
                        "UPDATE outbox SET status = 'FAILED', last_error = 'Order is not deliverable' WHERE id = ?",
                        (int(row["id"]),),
                    )
                    return "CLAIMED"
        try:
            await self._send(str(row["kind"]), int(row["chat_id"]), payload)
        except TelegramRetryAfter as exc:
            await self._retry(
                int(row["id"]),
                int(row["attempts"]) + 1,
                f"Retry after {exc.retry_after}",
                exc.retry_after,
                payload,
                uncertain=False,
            )
        except TelegramNetworkError as exc:
            await self._retry(
                int(row["id"]),
                int(row["attempts"]) + 1,
                str(exc),
                None,
                payload,
                uncertain=True,
            )
        except TelegramBadRequest as exc:
            await self._retry(
                int(row["id"]),
                int(row["attempts"]) + 1,
                str(exc),
                None,
                payload,
                uncertain=False,
            )
        except TelegramForbiddenError as exc:
            await self.db.execute(
                "UPDATE users SET bot_blocked = 1 WHERE telegram_id = ?",
                (int(row["chat_id"]),),
            )
            await self._fail(int(row["id"]), payload, str(exc))
        except Exception as exc:  # noqa: BLE001 - one bad outbox item must not stop the worker
            await self._retry(
                int(row["id"]),
                int(row["attempts"]) + 1,
                str(exc),
                None,
                payload,
                uncertain=True,
            )
        else:
            await self._sent(int(row["id"]), str(row["kind"]), payload)
        return str(row["kind"])

    async def _send(self, kind: str, chat_id: int, payload: dict[str, Any]) -> None:
        if kind == "ORDER_DELIVERY":
            order_id = int(payload["order_id"])
            header = (
                f"✅ Оплата получена\n\nЗаказ: #{order_id}\nТовар: {payload['product_name']}\n"
                f"Сумма: {format_money(int(payload['amount_cents']), 'USDT')}"
            )
            cashback = int(payload.get("cashback_cents", 0))
            if cashback:
                header += f"\n🎁 Cashback: {format_money(cashback, 'USDT')}"
            if payload.get("file_id"):
                await self.bot.send_document(
                    chat_id, str(payload["file_id"]), caption=header, parse_mode=None
                )
            else:
                body = str(
                    payload.get("text") or "Товар готов. Свяжитесь с поддержкой."
                )
                prefix = f"{header}\n\n📦 Ваш товар:\n"
                if len(prefix) + len(body) <= 4096:
                    await self.bot.send_message(chat_id, prefix + body, parse_mode=None)
                else:
                    document = BufferedInputFile(
                        body.encode("utf-8"), filename=f"order_{order_id}.txt"
                    )
                    await self.bot.send_document(
                        chat_id,
                        document,
                        caption=f"{header}\n\n📦 Товар во вложении.",
                        parse_mode=None,
                    )
            return
        if kind == "BROADCAST":
            keyboard = self._keyboard(payload.get("buttons", []))
            if payload.get("photo_file_id"):
                await self.bot.send_photo(
                    chat_id,
                    str(payload["photo_file_id"]),
                    caption=str(payload.get("text", "")),
                    reply_markup=keyboard,
                    parse_mode=None,
                )
            else:
                await self.bot.send_message(
                    chat_id,
                    str(payload.get("text", "")),
                    reply_markup=keyboard,
                    parse_mode=None,
                )
            return
        await self.bot.send_message(
            chat_id, str(payload.get("text", "")), parse_mode=None
        )

    @staticmethod
    def _keyboard(buttons: list[dict[str, str]]) -> InlineKeyboardMarkup | None:
        rows = []
        for button in buttons[:8]:
            text = str(button.get("text", ""))[:64]
            url = str(button.get("url", ""))
            if text and url.startswith(("https://", "http://", "tg://")):
                rows.append([InlineKeyboardButton(text=text, url=url)])
        return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None

    async def _sent(self, outbox_id: int, kind: str, payload: dict[str, Any]) -> None:
        async with self.db.transaction() as connection:
            await connection.execute(
                "UPDATE outbox SET status = 'SENT', sent_at = CURRENT_TIMESTAMP, last_error = NULL WHERE id = ?",
                (outbox_id,),
            )
            if kind == "ORDER_DELIVERY":
                await connection.execute(
                    "UPDATE orders SET delivery_status = 'SENT', delivered_at = CURRENT_TIMESTAMP, "
                    "updated_at = CURRENT_TIMESTAMP WHERE id = ? AND status != 'REFUNDED'",
                    (int(payload["order_id"]),),
                )
                logger.info("Delivered order %s", payload["order_id"])
            if kind == "BROADCAST" and payload.get("broadcast_id"):
                await connection.execute(
                    "UPDATE broadcasts SET sent_count = sent_count + 1 WHERE id = ?",
                    (int(payload["broadcast_id"]),),
                )
                await self._finish_broadcast_if_done(
                    connection, int(payload["broadcast_id"])
                )

    async def _retry(
        self,
        outbox_id: int,
        attempts: int,
        error: str,
        delay: int | None,
        payload: dict[str, Any],
        *,
        uncertain: bool,
    ) -> None:
        if attempts >= 10:
            row = await self.db.fetchone(
                "SELECT payload_json FROM outbox WHERE id = ?", (outbox_id,)
            )
            payload = json.loads(str(row["payload_json"])) if row else {}
            await self._fail(outbox_id, payload, error)
            return
        seconds = delay if delay is not None else min(3600, 2 ** min(attempts, 10))
        await self.db.execute(
            "UPDATE outbox SET status = 'PENDING', available_at = datetime('now', ?), last_error = ? WHERE id = ?",
            (f"+{max(1, seconds)} seconds", error[:500], outbox_id),
        )
        if payload.get("order_id"):
            await self.db.execute(
                "UPDATE orders SET delivery_status = ?, last_error = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE id = ? AND status != 'REFUNDED'",
                (
                    "UNKNOWN" if uncertain else "PENDING",
                    error[:500],
                    int(payload["order_id"]),
                ),
            )

    async def _fail(self, outbox_id: int, payload: dict[str, Any], error: str) -> None:
        async with self.db.transaction() as connection:
            cursor = await connection.execute(
                "SELECT kind FROM outbox WHERE id = ?", (outbox_id,)
            )
            row = await cursor.fetchone()
            await cursor.close()
            await connection.execute(
                "UPDATE outbox SET status = 'FAILED', last_error = ? WHERE id = ?",
                (error[:500], outbox_id),
            )
            if row and str(row["kind"]) == "ORDER_DELIVERY" and payload.get("order_id"):
                await connection.execute(
                    "UPDATE orders SET delivery_status = 'FAILED', last_error = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (error[:500], int(payload["order_id"])),
                )
            if row and str(row["kind"]) == "BROADCAST" and payload.get("broadcast_id"):
                broadcast_id = int(payload["broadcast_id"])
                await connection.execute(
                    "UPDATE broadcasts SET failed_count = failed_count + 1 WHERE id = ?",
                    (broadcast_id,),
                )
                await self._finish_broadcast_if_done(connection, broadcast_id)

    @staticmethod
    async def _finish_broadcast_if_done(connection: Any, broadcast_id: int) -> None:
        cursor = await connection.execute(
            "SELECT total_count, sent_count, failed_count FROM broadcasts WHERE id = ?",
            (broadcast_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row and int(row["sent_count"]) + int(row["failed_count"]) >= int(
            row["total_count"]
        ):
            await connection.execute(
                "UPDATE broadcasts SET status = 'COMPLETED', completed_at = CURRENT_TIMESTAMP WHERE id = ?",
                (broadcast_id,),
            )
