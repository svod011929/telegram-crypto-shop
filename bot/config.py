from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


class ConfigurationError(RuntimeError):
    """Raised when required system configuration is absent or malformed."""


@dataclass(frozen=True, slots=True)
class Config:
    bot_token: str
    crypto_pay_token: str
    crypto_pay_network: str
    superadmin_id: int
    database_path: Path
    log_level: str
    payment_poll_interval: int
    payment_batch_size: int
    crypto_timeout: int

    @property
    def crypto_base_url(self) -> str:
        if self.crypto_pay_network == "testnet":
            return "https://testnet-pay.crypt.bot/api"
        return "https://pay.crypt.bot/api"

    @classmethod
    def from_env(cls, *, require_tokens: bool = True) -> Config:
        load_dotenv()
        token = os.getenv("BOT_TOKEN", "").strip()
        crypto_token = os.getenv("CRYPTO_PAY_TOKEN", "").strip()
        superadmin_raw = os.getenv("SUPERADMIN_ID", "").strip()
        network = os.getenv("CRYPTO_PAY_NETWORK", "mainnet").strip().lower()
        if require_tokens and not token:
            raise ConfigurationError("BOT_TOKEN is required")
        if require_tokens and not re.fullmatch(r"\d+:[A-Za-z0-9_-]{20,}", token):
            raise ConfigurationError("BOT_TOKEN has an invalid format")
        if require_tokens and not crypto_token:
            raise ConfigurationError("CRYPTO_PAY_TOKEN is required")
        if require_tokens and not superadmin_raw:
            raise ConfigurationError("SUPERADMIN_ID is required")
        if network not in {"mainnet", "testnet"}:
            raise ConfigurationError("CRYPTO_PAY_NETWORK must be mainnet or testnet")
        try:
            superadmin_id = int(superadmin_raw or "1")
            poll_interval = max(2, int(os.getenv("PAYMENT_POLL_INTERVAL", "5")))
            batch_size = min(1000, max(1, int(os.getenv("PAYMENT_BATCH_SIZE", "100"))))
            crypto_timeout = max(3, int(os.getenv("CRYPTO_PAY_TIMEOUT", "15")))
        except ValueError as exc:
            raise ConfigurationError("Numeric environment value is malformed") from exc
        if superadmin_id <= 0:
            raise ConfigurationError("SUPERADMIN_ID must be a positive Telegram ID")
        return cls(
            bot_token=token,
            crypto_pay_token=crypto_token,
            crypto_pay_network=network,
            superadmin_id=superadmin_id,
            database_path=Path(os.getenv("DATABASE_PATH", "data/bot.db")),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            payment_poll_interval=poll_interval,
            payment_batch_size=batch_size,
            crypto_timeout=crypto_timeout,
        )
