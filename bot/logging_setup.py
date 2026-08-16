from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


class SecretFilter(logging.Filter):
    def __init__(self, secrets: list[str]) -> None:
        super().__init__()
        self._secrets = [secret for secret in secrets if secret]

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        for secret in self._secrets:
            message = message.replace(secret, "[REDACTED]")
        record.msg = message
        record.args = ()
        return True


class RedactingFormatter(logging.Formatter):
    """Redact secrets from the final line, including formatted tracebacks."""

    def __init__(self, format_string: str, secrets: list[str]) -> None:
        super().__init__(format_string)
        self._secrets = [secret for secret in secrets if secret]

    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        for secret in self._secrets:
            rendered = rendered.replace(secret, "[REDACTED]")
        return rendered


def setup_logging(
    level: str, secrets: list[str], directory: Path | str = "logs"
) -> None:
    log_directory = Path(directory)
    log_directory.mkdir(parents=True, exist_ok=True)
    formatter = RedactingFormatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s", secrets
    )
    secret_filter = SecretFilter(secrets)
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console.addFilter(secret_filter)
    file_handler = RotatingFileHandler(
        log_directory / "bot.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(secret_filter)
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.handlers.clear()
    root.addHandler(console)
    root.addHandler(file_handler)
