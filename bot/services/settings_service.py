from __future__ import annotations

import re
from typing import Any, ClassVar

from bot.database.db import Database


class SettingError(ValueError):
    """Raised for unsupported or invalid business settings."""


class SettingsService:
    EDITABLE_KEYS: ClassVar[set[str]] = {
        "shop_name",
        "support_username",
        "global_referral_bp",
        "global_cashback_bp",
        "min_withdrawal_cents",
        "withdrawal_fee_bp",
        "withdrawal_mode",
        "payout_asset",
        "invoice_asset",
        "invoice_expiry_sec",
        "payment_poll_interval",
        "crypto_payment_enabled",
        "balance_payment_enabled",
        "balance_rewards_enabled",
        "cashback_withdrawable",
        "maintenance_mode",
        "notify_admin_sales",
        "notify_referral_rewards",
        "backup_interval_hours",
        "backup_retention",
        "page_size",
        "broadcast_rate_per_second",
        "welcome_text",
        "maintenance_text",
    }

    def __init__(self, db: Database) -> None:
        self.db = db

    async def get(self, key: str, default: str | None = None) -> str:
        row = await self.db.fetchone("SELECT value FROM settings WHERE key = ?", (key,))
        if row is None:
            if default is None:
                raise SettingError(f"Unknown setting: {key}")
            return default
        return str(row["value"])

    async def get_int(self, key: str, default: int | None = None) -> int:
        raw = await self.get(key, None if default is None else str(default))
        try:
            return int(raw)
        except ValueError as exc:
            raise SettingError(f"Setting {key} is not an integer") from exc

    async def get_bool(self, key: str, default: bool | None = None) -> bool:
        raw = (
            await self.get(key, None if default is None else str(default).lower())
        ).lower()
        if raw not in {"true", "false"}:
            raise SettingError(f"Setting {key} is not boolean")
        return raw == "true"

    async def all(self) -> list[dict[str, Any]]:
        rows = await self.db.fetchall("SELECT * FROM settings ORDER BY key")
        return [dict(row) for row in rows]

    async def set(self, key: str, value: str, updated_by: int) -> None:
        if key not in self.EDITABLE_KEYS:
            raise SettingError("Эта настройка не поддерживается")
        row = await self.db.fetchone(
            "SELECT value_type FROM settings WHERE key = ?", (key,)
        )
        if row is None:
            raise SettingError("Настройка не найдена")
        normalized = self._validate(key, value, str(row["value_type"]))
        await self.db.execute(
            "UPDATE settings SET value = ?, updated_by = ?, updated_at = CURRENT_TIMESTAMP WHERE key = ?",
            (normalized, updated_by, key),
        )

    @staticmethod
    def _validate(key: str, value: str, value_type: str) -> str:
        text = value.strip()
        if value_type == "bool":
            lowered = text.lower()
            aliases = {
                "1": "true",
                "0": "false",
                "yes": "true",
                "no": "false",
                "да": "true",
                "нет": "false",
            }
            normalized = aliases.get(lowered, lowered)
            if normalized not in {"true", "false"}:
                raise SettingError("Используйте true или false")
            return normalized
        if value_type == "int":
            try:
                number = int(text)
            except ValueError as exc:
                raise SettingError("Нужно целое число") from exc
            limits: dict[str, tuple[int, int]] = {
                "global_referral_bp": (0, 10_000),
                "global_cashback_bp": (0, 10_000),
                "withdrawal_fee_bp": (0, 10_000),
                "min_withdrawal_cents": (1, 100_000_000),
                "invoice_expiry_sec": (60, 2_678_400),
                "payment_poll_interval": (2, 300),
                "backup_interval_hours": (1, 720),
                "backup_retention": (1, 100),
                "page_size": (1, 20),
                "broadcast_rate_per_second": (1, 25),
            }
            low, high = limits.get(key, (-2_147_483_648, 2_147_483_647))
            if not low <= number <= high:
                raise SettingError(f"Допустимый диапазон: {low}..{high}")
            return str(number)
        if key == "withdrawal_mode":
            upper = text.upper()
            if upper not in {"AUTO", "MANUAL", "DISABLED"}:
                raise SettingError("Режим: AUTO, MANUAL или DISABLED")
            return upper
        if key in {"payout_asset", "invoice_asset"}:
            upper = text.upper()
            if upper not in {"USDT", "TON", "BTC", "ETH", "LTC", "BNB", "TRX", "USDC"}:
                raise SettingError("Неподдерживаемый актив")
            if key == "invoice_asset" and upper not in {"USDT", "USDC"}:
                raise SettingError(
                    "Цена каталога номинирована в USD; для счетов используйте USDT или USDC"
                )
            return upper
        if key == "support_username":
            username = text.lstrip("@")
            if username and not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
                raise SettingError("Некорректный Telegram username")
            return username
        if len(text) > 3500:
            raise SettingError("Значение слишком длинное")
        return text
