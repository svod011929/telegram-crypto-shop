from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ProductType(StrEnum):
    TEXT = "TEXT"
    FILE = "FILE"
    UNIQUE = "UNIQUE"
    MANUAL = "MANUAL"


class OrderStatus(StrEnum):
    CREATED = "CREATED"
    WAITING_PAYMENT = "WAITING_PAYMENT"
    PAID = "PAID"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"
    WAITING_DELIVERY = "WAITING_DELIVERY"
    REFUNDED = "REFUNDED"


class WithdrawalStatus(StrEnum):
    CREATED = "CREATED"
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    REJECTED = "REJECTED"
    REFUNDED = "REFUNDED"


@dataclass(frozen=True, slots=True)
class CreatedOrder:
    id: int
    user_id: int
    telegram_id: int
    product_id: int
    product_name: str
    amount_cents: int
    currency: str
    status: str
    payment_method: str
    existing: bool = False
