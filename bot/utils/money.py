from __future__ import annotations

from decimal import Decimal, InvalidOperation


class MoneyError(ValueError):
    """Raised when a monetary value is invalid."""


MAX_MONEY = Decimal(1_000_000_000_000)


def parse_money_to_cents(value: str | Decimal) -> int:
    try:
        amount = Decimal(str(value).replace(",", ".").strip())
    except (InvalidOperation, ValueError) as exc:
        raise MoneyError("Некорректная сумма") from exc
    if not amount.is_finite() or amount <= 0:
        raise MoneyError("Сумма должна быть больше нуля")
    if amount > MAX_MONEY:
        raise MoneyError("Сумма превышает допустимый лимит")
    scaled = amount * 100
    if scaled != scaled.to_integral_value():
        raise MoneyError("Допустимо не более двух знаков после запятой")
    cents = int(scaled)
    if cents <= 0:
        raise MoneyError("Минимальная сумма — 0.01")
    return cents


def cents_to_api_amount(cents: int) -> str:
    if cents < 0:
        raise MoneyError("Negative API amount is not allowed")
    return f"{cents // 100}.{cents % 100:02d}"


def format_money(cents: int, currency: str = "USD") -> str:
    sign = "-" if cents < 0 else ""
    absolute = abs(cents)
    symbol = "$" if currency.upper() in {"USD", "USDT"} else f" {currency.upper()}"
    if symbol == "$":
        return f"{sign}${absolute // 100}.{absolute % 100:02d}"
    return f"{sign}{absolute // 100}.{absolute % 100:02d}{symbol}"


def basis_points(amount_cents: int, bp: int) -> int:
    if amount_cents < 0 or not 0 <= bp <= 10_000:
        raise MoneyError("Invalid amount or percentage")
    return amount_cents * bp // 10_000


def format_basis_points(bp: int) -> str:
    sign = "-" if bp < 0 else ""
    absolute = abs(bp)
    return f"{sign}{absolute // 100}.{absolute % 100:02d}%"
