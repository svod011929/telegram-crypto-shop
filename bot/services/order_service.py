from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from decimal import Decimal, InvalidOperation
from typing import Any

import aiosqlite

from bot.database.db import Database
from bot.database.models import CreatedOrder, OrderStatus, ProductType
from bot.services.balance_service import BalanceService
from bot.services.crypto_pay import (
    CryptoPayAPIError,
    CryptoPayClient,
    CryptoPayTransportError,
)
from bot.services.settings_service import SettingsService
from bot.utils.money import basis_points, cents_to_api_amount

logger = logging.getLogger(__name__)


class OrderError(ValueError):
    """Raised when an order cannot be created or transitioned."""


class OutOfStock(OrderError):
    """Raised when no unique item is available."""


class OrderService:
    def __init__(
        self,
        db: Database,
        balance: BalanceService,
        settings: SettingsService,
        crypto: CryptoPayClient,
    ) -> None:
        self.db = db
        self.balance = balance
        self.settings = settings
        self.crypto = crypto
        self._stripes = [asyncio.Lock() for _ in range(64)]

    def _lock_for(self, user_id: int) -> asyncio.Lock:
        return self._stripes[user_id % len(self._stripes)]

    async def _resolved_rates(self, product: dict[str, Any]) -> tuple[int, int]:
        referral = product["referral_bp"]
        cashback = product["cashback_bp"]
        if referral is None:
            referral = await self.settings.get_int("global_referral_bp", 0)
        if cashback is None:
            cashback = await self.settings.get_int("global_cashback_bp", 0)
        return int(referral), int(cashback)

    async def _insert_order(
        self,
        connection: aiosqlite.Connection,
        *,
        user_id: int,
        product: dict[str, Any],
        payment_method: str,
        request_key: str,
        referral_bp: int,
        cashback_bp: int,
    ) -> int:
        cursor = await connection.execute(
            "INSERT INTO orders(user_id, product_id, product_name, amount_cents, currency, payment_method, status, "
            "request_key, stock_type, delivery_text, delivery_file_id, referral_bp, cashback_bp) "
            "VALUES (?, ?, ?, ?, 'USDT', ?, 'CREATED', ?, ?, ?, ?, ?, ?)",
            (
                user_id,
                int(product["id"]),
                str(product["name"]),
                int(product["price_cents"]),
                payment_method,
                request_key,
                str(product["stock_type"]),
                product.get("content_text"),
                product.get("telegram_file_id"),
                referral_bp,
                cashback_bp,
            ),
        )
        order_id = int(cursor.lastrowid)
        await cursor.close()
        if str(product["stock_type"]) == ProductType.UNIQUE:
            cursor = await connection.execute(
                "SELECT id FROM product_stock WHERE product_id = ? AND status = 'AVAILABLE' ORDER BY id LIMIT 1",
                (int(product["id"]),),
            )
            stock = await cursor.fetchone()
            await cursor.close()
            if stock is None:
                raise OutOfStock("Товар закончился")
            stock_id = int(stock["id"])
            cursor = await connection.execute(
                "UPDATE product_stock SET status = 'RESERVED', order_id = ?, reserved_at = CURRENT_TIMESTAMP "
                "WHERE id = ? AND status = 'AVAILABLE'",
                (order_id, stock_id),
            )
            if cursor.rowcount != 1:
                await cursor.close()
                raise OutOfStock("Товар только что закончился")
            await cursor.close()
            await connection.execute(
                "UPDATE orders SET stock_item_id = ? WHERE id = ?", (stock_id, order_id)
            )
            await connection.execute(
                "UPDATE products SET stock_count = stock_count - 1 WHERE id = ? AND stock_count > 0",
                (int(product["id"]),),
            )
        return order_id

    async def _load_product_and_user(
        self, connection: aiosqlite.Connection, telegram_id: int, product_id: int
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        cursor = await connection.execute(
            "SELECT * FROM users WHERE telegram_id = ? AND banned = 0", (telegram_id,)
        )
        user_row = await cursor.fetchone()
        await cursor.close()
        if user_row is None:
            raise OrderError("Пользователь не найден или заблокирован")
        cursor = await connection.execute(
            "SELECT p.* FROM products p JOIN categories c ON c.id = p.category_id "
            "WHERE p.id = ? AND p.status = 'ACTIVE' AND c.active = 1 AND c.archived = 0",
            (product_id,),
        )
        product_row = await cursor.fetchone()
        await cursor.close()
        if product_row is None:
            raise OrderError("Товар недоступен")
        return dict(user_row), dict(product_row)

    async def create_crypto_purchase(
        self, telegram_id: int, product_id: int
    ) -> tuple[CreatedOrder, dict[str, Any] | None]:
        if not await self.settings.get_bool("crypto_payment_enabled", True):
            raise OrderError("Оплата через Crypto Pay временно отключена")
        lock = self._lock_for(telegram_id)
        async with lock:
            active = await self.db.fetchone(
                "SELECT o.*, u.telegram_id FROM orders o JOIN users u ON u.id = o.user_id "
                "WHERE u.telegram_id = ? AND o.product_id = ? AND o.payment_method = 'CRYPTO' "
                "AND o.status IN ('CREATED', 'WAITING_PAYMENT') ORDER BY o.id DESC LIMIT 1",
                (telegram_id, product_id),
            )
            if active is not None:
                active_order = self._created(dict(active), telegram_id, True)
                payment = await self.payment_for_order(active_order.id)
                if payment is not None:
                    return active_order, payment
                payload = f"order:{active_order.id}:user:{telegram_id}"
                invoice = await self.find_remote_invoice(payload)
                if invoice is not None:
                    await self.attach_invoice(active_order.id, invoice, payload)
                    return active_order, invoice
                return active_order, None
            request_key = f"crypto:{telegram_id}:{product_id}:{int(time.time()) // 20}"
            product_row = await self.db.fetchone(
                "SELECT * FROM products WHERE id = ?", (product_id,)
            )
            if product_row is None:
                raise OrderError("Товар не найден")
            referral_bp, cashback_bp = await self._resolved_rates(dict(product_row))
            try:
                async with self.db.transaction() as connection:
                    user, product = await self._load_product_and_user(
                        connection, telegram_id, product_id
                    )
                    order_id = await self._insert_order(
                        connection,
                        user_id=int(user["id"]),
                        product=product,
                        payment_method="CRYPTO",
                        request_key=request_key,
                        referral_bp=referral_bp,
                        cashback_bp=cashback_bp,
                    )
            except sqlite3.IntegrityError:
                existing = await self.db.fetchone(
                    "SELECT * FROM orders WHERE request_key = ?", (request_key,)
                )
                if existing is None:
                    raise
                payment = await self.db.fetchone(
                    "SELECT * FROM payments WHERE order_id = ?", (int(existing["id"]),)
                )
                user = await self.db.fetchone(
                    "SELECT telegram_id FROM users WHERE id = ?",
                    (int(existing["user_id"]),),
                )
                created = self._created(dict(existing), int(user["telegram_id"]), True)
                return created, dict(payment) if payment else None

            order = await self.db.fetchone(
                "SELECT * FROM orders WHERE id = ?", (order_id,)
            )
            if order is None:
                raise RuntimeError("Created order disappeared")
            created = self._created(dict(order), telegram_id, False)
            invoice = await self._create_or_recover_invoice(created)
            return created, invoice

    async def _create_or_recover_invoice(
        self, order: CreatedOrder
    ) -> dict[str, Any] | None:
        asset = await self.settings.get("invoice_asset", "USDT")
        expiry = await self.settings.get_int("invoice_expiry_sec", 3600)
        payload = f"order:{order.id}:user:{order.telegram_id}"
        try:
            invoice = await self.crypto.create_invoice(
                currency_type="crypto",
                asset=asset,
                amount=cents_to_api_amount(order.amount_cents),
                description=f"Заказ #{order.id}: {order.product_name}"[:1024],
                payload=payload,
                allow_comments=False,
                allow_anonymous=False,
                expires_in=expiry,
            )
        except CryptoPayTransportError:
            invoice = await self.find_remote_invoice(payload)
            if invoice is None:
                logger.warning(
                    "Invoice outcome is uncertain for order %s; recovery worker will retry lookup",
                    order.id,
                )
                return None
        except CryptoPayAPIError:
            await self.cancel_unpaid_order(
                order.id, "Crypto Pay rejected invoice creation"
            )
            raise
        await self.attach_invoice(order.id, invoice, payload)
        return invoice

    async def find_remote_invoice(self, payload: str) -> dict[str, Any] | None:
        try:
            invoices = await self.crypto.get_invoices(count=100)
        except (CryptoPayAPIError, CryptoPayTransportError):
            return None
        return next(
            (
                invoice
                for invoice in invoices
                if str(invoice.get("payload", "")) == payload
            ),
            None,
        )

    async def attach_invoice(
        self, order_id: int, invoice: dict[str, Any], payload: str
    ) -> None:
        invoice_id = int(invoice["invoice_id"])
        if invoice_id <= 0:
            raise OrderError("Crypto Pay вернул некорректный invoice_id")
        invoice_url = str(
            invoice.get("bot_invoice_url")
            or invoice.get("mini_app_invoice_url")
            or invoice.get("pay_url")
            or ""
        )
        if not invoice_url.startswith("https://"):
            raise OrderError("Crypto Pay не вернул ссылку на оплату")
        expected_asset = (await self.settings.get("invoice_asset", "USDT")).upper()
        remote_asset = str(invoice.get("asset") or "").upper()
        if remote_asset != expected_asset:
            raise OrderError("Crypto Pay вернул счёт в неожиданном активе")
        if str(invoice.get("payload", "")) != payload:
            raise OrderError("Crypto Pay вернул счёт с неожиданным payload")
        remote_cents = self._invoice_amount_cents(invoice)
        async with self.db.transaction() as connection:
            cursor = await connection.execute(
                "SELECT * FROM orders WHERE id = ?", (order_id,)
            )
            order = await cursor.fetchone()
            await cursor.close()
            if order is None:
                raise OrderError("Заказ не найден")
            if remote_cents != int(order["amount_cents"]):
                raise OrderError("Crypto Pay вернул счёт с неожиданной суммой")
            if str(order["status"]) != OrderStatus.CREATED:
                cursor = await connection.execute(
                    "SELECT invoice_id FROM payments WHERE order_id = ?", (order_id,)
                )
                known = await cursor.fetchone()
                await cursor.close()
                if known and int(known["invoice_id"]) == invoice_id:
                    return
                raise OrderError("К заказу уже привязан другой счёт")
            await connection.execute(
                "INSERT INTO payments(order_id, invoice_id, invoice_url, amount_cents, currency, payload, status, expiration_date, raw_response_json) "
                "VALUES (?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?)",
                (
                    order_id,
                    invoice_id,
                    invoice_url,
                    int(order["amount_cents"]),
                    str(
                        invoice.get("asset") or invoice.get("fiat") or order["currency"]
                    ),
                    payload,
                    invoice.get("expiration_date"),
                    json.dumps(invoice, ensure_ascii=False),
                ),
            )
            await connection.execute(
                "UPDATE orders SET status = 'WAITING_PAYMENT', expires_at = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (invoice.get("expiration_date"), order_id),
            )
        logger.info("Created Crypto Pay invoice %s for order %s", invoice_id, order_id)

    async def purchase_with_balance(
        self, telegram_id: int, product_id: int
    ) -> CreatedOrder:
        if not await self.settings.get_bool("balance_payment_enabled", True):
            raise OrderError("Оплата с баланса временно отключена")
        lock = self._lock_for(telegram_id)
        async with lock:
            request_key = f"balance:{telegram_id}:{product_id}:{int(time.time()) // 20}"
            product_row = await self.db.fetchone(
                "SELECT * FROM products WHERE id = ?", (product_id,)
            )
            if product_row is None:
                raise OrderError("Товар не найден")
            referral_bp, cashback_bp = await self._resolved_rates(dict(product_row))
            reward_balance = await self.settings.get_bool(
                "balance_rewards_enabled", False
            )
            cashback_withdrawable = await self.settings.get_bool(
                "cashback_withdrawable", True
            )
            notify_referrer = await self.settings.get_bool(
                "notify_referral_rewards", True
            )
            notify_admin = await self.settings.get_bool("notify_admin_sales", True)
            try:
                async with self.db.transaction() as connection:
                    user, product = await self._load_product_and_user(
                        connection, telegram_id, product_id
                    )
                    order_id = await self._insert_order(
                        connection,
                        user_id=int(user["id"]),
                        product=product,
                        payment_method="BALANCE",
                        request_key=request_key,
                        referral_bp=referral_bp,
                        cashback_bp=cashback_bp,
                    )
                    await self.balance.apply_in_transaction(
                        connection,
                        user_id=int(user["id"]),
                        amount_cents=-int(product["price_cents"]),
                        transaction_type="PURCHASE",
                        idempotency_key=f"purchase:order:{order_id}",
                        description=f"Покупка заказа #{order_id}",
                        related_order_id=order_id,
                    )
                    await self._finalize_in_transaction(
                        connection,
                        order_id,
                        reward_balance,
                        cashback_withdrawable,
                        notify_referrer,
                        notify_admin,
                    )
            except sqlite3.IntegrityError:
                existing = await self.db.fetchone(
                    "SELECT * FROM orders WHERE request_key = ?", (request_key,)
                )
                if existing is None:
                    raise
                user = await self.db.fetchone(
                    "SELECT telegram_id FROM users WHERE id = ?",
                    (int(existing["user_id"]),),
                )
                return self._created(dict(existing), int(user["telegram_id"]), True)
            row = await self.db.fetchone(
                "SELECT * FROM orders WHERE id = ?", (order_id,)
            )
            if row is None:
                raise RuntimeError("Balance order disappeared")
            logger.info("Created balance order %s", order_id)
            return self._created(dict(row), telegram_id, False)

    @staticmethod
    def _created(
        order: dict[str, Any], telegram_id: int, existing: bool
    ) -> CreatedOrder:
        return CreatedOrder(
            id=int(order["id"]),
            user_id=int(order["user_id"]),
            telegram_id=telegram_id,
            product_id=int(order["product_id"]),
            product_name=str(order["product_name"]),
            amount_cents=int(order["amount_cents"]),
            currency=str(order["currency"]),
            status=str(order["status"]),
            payment_method=str(order["payment_method"]),
            existing=existing,
        )

    async def observe_invoice(self, payment_id: int, invoice: dict[str, Any]) -> str:
        remote_status = str(invoice.get("status", "")).lower()
        payment = await self.db.fetchone(
            "SELECT p.*, o.user_id, u.telegram_id FROM payments p JOIN orders o ON o.id = p.order_id "
            "JOIN users u ON u.id = o.user_id WHERE p.id = ?",
            (payment_id,),
        )
        if payment is None:
            return "missing"
        expected_payload = str(payment["payload"])
        if int(invoice.get("invoice_id", -1)) != int(payment["invoice_id"]):
            raise OrderError("Invoice ID mismatch")
        if str(invoice.get("payload", "")) != expected_payload:
            raise OrderError("Invoice payload mismatch")
        remote_cents = self._invoice_amount_cents(invoice)
        if remote_cents != int(payment["amount_cents"]):
            raise OrderError("Invoice amount mismatch")
        remote_currency = str(invoice.get("asset") or invoice.get("fiat") or "").upper()
        if remote_currency != str(payment["currency"]).upper():
            raise OrderError("Invoice currency mismatch")
        if remote_status == "paid":
            async with self.db.transaction() as connection:
                await connection.execute(
                    "UPDATE payments SET status = 'PAID', paid_at = ?, raw_response_json = ?, last_checked_at = CURRENT_TIMESTAMP, "
                    "check_attempts = check_attempts + 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (
                        invoice.get("paid_at"),
                        json.dumps(invoice, ensure_ascii=False),
                        payment_id,
                    ),
                )
                await connection.execute(
                    "UPDATE orders SET status = CASE WHEN financial_processed_at IS NULL THEN 'PAID' ELSE status END, "
                    "paid_at = COALESCE(paid_at, ?), updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (invoice.get("paid_at"), int(payment["order_id"])),
                )
            await self.process_paid_order(int(payment["order_id"]))
            return "paid"
        if remote_status == "expired":
            await self.expire_order(int(payment["order_id"]), payment_id)
            return "expired"
        await self.db.execute(
            "UPDATE payments SET last_checked_at = CURRENT_TIMESTAMP, check_attempts = check_attempts + 1, "
            "raw_response_json = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (json.dumps(invoice, ensure_ascii=False), payment_id),
        )
        return "active"

    @staticmethod
    def _invoice_amount_cents(invoice: dict[str, Any]) -> int:
        try:
            scaled_amount = Decimal(str(invoice.get("amount"))) * 100
            if (
                not scaled_amount.is_finite()
                or scaled_amount <= 0
                or scaled_amount > 9_000_000_000_000_000_000
                or scaled_amount != scaled_amount.to_integral_value()
            ):
                raise OrderError("Invoice amount has unsupported precision")
            return int(scaled_amount)
        except (InvalidOperation, ValueError) as exc:
            raise OrderError("Invoice amount is malformed") from exc

    async def process_paid_order(self, order_id: int) -> bool:
        reward_balance = await self.settings.get_bool("balance_rewards_enabled", False)
        cashback_withdrawable = await self.settings.get_bool(
            "cashback_withdrawable", True
        )
        notify_referrer = await self.settings.get_bool("notify_referral_rewards", True)
        notify_admin = await self.settings.get_bool("notify_admin_sales", True)
        async with self.db.transaction() as connection:
            return await self._finalize_in_transaction(
                connection,
                order_id,
                reward_balance,
                cashback_withdrawable,
                notify_referrer,
                notify_admin,
            )

    async def _finalize_in_transaction(
        self,
        connection: aiosqlite.Connection,
        order_id: int,
        reward_balance_purchases: bool,
        cashback_withdrawable: bool,
        notify_referrer: bool,
        notify_admin: bool,
    ) -> bool:
        cursor = await connection.execute(
            "SELECT o.*, u.telegram_id, u.referrer_id FROM orders o JOIN users u ON u.id = o.user_id WHERE o.id = ?",
            (order_id,),
        )
        order = await cursor.fetchone()
        await cursor.close()
        if order is None:
            raise OrderError("Заказ не найден")
        if order["financial_processed_at"] is not None:
            return False
        if str(order["payment_method"]) == "CRYPTO":
            cursor = await connection.execute(
                "SELECT status FROM payments WHERE order_id = ?", (order_id,)
            )
            payment = await cursor.fetchone()
            await cursor.close()
            if payment is None or str(payment["status"]) != "PAID":
                raise OrderError("Оплата заказа не подтверждена")
        await connection.execute(
            "UPDATE orders SET status = 'PROCESSING', updated_at = CURRENT_TIMESTAMP WHERE id = ? AND financial_processed_at IS NULL",
            (order_id,),
        )
        if str(order["stock_type"]) == ProductType.UNIQUE:
            cursor = await connection.execute(
                "UPDATE product_stock SET status = 'SOLD', sold_at = CURRENT_TIMESTAMP "
                "WHERE id = ? AND order_id = ? AND status = 'RESERVED'",
                (int(order["stock_item_id"]), order_id),
            )
            if cursor.rowcount != 1:
                await cursor.close()
                raise OutOfStock("Reserved stock item is unavailable")
            await cursor.close()
        eligible = str(order["payment_method"]) == "CRYPTO" or reward_balance_purchases
        cashback = (
            basis_points(int(order["amount_cents"]), int(order["cashback_bp"]))
            if eligible
            else 0
        )
        referral = (
            basis_points(int(order["amount_cents"]), int(order["referral_bp"]))
            if eligible
            else 0
        )
        if cashback > 0:
            await self.balance.apply_in_transaction(
                connection,
                user_id=int(order["user_id"]),
                amount_cents=cashback,
                transaction_type="CASHBACK",
                idempotency_key=f"cashback:order:{order_id}",
                description=f"Cashback за заказ #{order_id}",
                related_order_id=order_id,
                withdrawable_credit_cents=cashback if cashback_withdrawable else 0,
            )
            await connection.execute(
                "INSERT OR IGNORE INTO cashback_events(order_id, user_id, percent_bp, amount_cents) VALUES (?, ?, ?, ?)",
                (order_id, int(order["user_id"]), int(order["cashback_bp"]), cashback),
            )
        referrer_tg: int | None = None
        if referral > 0 and order["referrer_id"] is not None:
            referrer_id = int(order["referrer_id"])
            await self.balance.apply_in_transaction(
                connection,
                user_id=referrer_id,
                amount_cents=referral,
                transaction_type="REFERRAL_REWARD",
                idempotency_key=f"referral:order:{order_id}",
                description=f"Партнёрское начисление за заказ #{order_id}",
                related_order_id=order_id,
            )
            await connection.execute(
                "INSERT OR IGNORE INTO referral_events(order_id, buyer_user_id, referrer_user_id, percent_bp, amount_cents) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    order_id,
                    int(order["user_id"]),
                    referrer_id,
                    int(order["referral_bp"]),
                    referral,
                ),
            )
            cursor = await connection.execute(
                "SELECT telegram_id FROM users WHERE id = ?", (referrer_id,)
            )
            referrer = await cursor.fetchone()
            await cursor.close()
            referrer_tg = int(referrer["telegram_id"]) if referrer else None
        if str(order["payment_method"]) == "CRYPTO":
            await connection.execute(
                "UPDATE users SET total_deposited_cents = total_deposited_cents + ?, "
                "total_spent_cents = total_spent_cents + ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (
                    int(order["amount_cents"]),
                    int(order["amount_cents"]),
                    int(order["user_id"]),
                ),
            )
        manual = str(order["stock_type"]) == ProductType.MANUAL
        final_status = OrderStatus.WAITING_DELIVERY if manual else OrderStatus.COMPLETED
        delivery_status = "MANUAL" if manual else "PENDING"
        await connection.execute(
            "UPDATE orders SET status = ?, delivery_status = ?, financial_processed_at = CURRENT_TIMESTAMP, "
            "completed_at = CASE WHEN ? = 'COMPLETED' THEN CURRENT_TIMESTAMP ELSE completed_at END, "
            "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (final_status, delivery_status, final_status, order_id),
        )
        await connection.execute(
            "UPDATE products SET purchases_count = purchases_count + 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (int(order["product_id"]),),
        )
        if not manual:
            payload: dict[str, Any] = {
                "order_id": order_id,
                "product_name": str(order["product_name"]),
                "amount_cents": int(order["amount_cents"]),
                "cashback_cents": cashback,
                "stock_type": str(order["stock_type"]),
                "text": order["delivery_text"],
                "file_id": order["delivery_file_id"],
            }
            if str(order["stock_type"]) == ProductType.UNIQUE:
                cursor = await connection.execute(
                    "SELECT value FROM product_stock WHERE id = ?",
                    (int(order["stock_item_id"]),),
                )
                stock = await cursor.fetchone()
                await cursor.close()
                payload["text"] = str(stock["value"])
            await self._enqueue(
                connection,
                f"delivery:order:{order_id}",
                "ORDER_DELIVERY",
                int(order["telegram_id"]),
                payload,
            )
        else:
            await self._enqueue_admins(
                connection,
                f"manual-order:{order_id}",
                "ADMIN_NOTICE",
                {
                    "text": f"📦 Заказ #{order_id} ожидает ручной выдачи: {order['product_name']}"
                },
            )
        if referral > 0 and referrer_tg and notify_referrer:
            await self._enqueue(
                connection,
                f"referral:order:{order_id}",
                "TEXT",
                referrer_tg,
                {
                    "text": f"💰 Новое партнёрское начисление!\nЗаказ: #{order_id}\nНачислено: ${referral // 100}.{referral % 100:02d}"
                },
            )
        if notify_admin:
            await self._enqueue_admins(
                connection,
                f"sale:{order_id}",
                "ADMIN_NOTICE",
                {
                    "text": f"🛍 Новая продажа\nOrder: #{order_id}\nUser: {order['telegram_id']}\n"
                    f"Product: {order['product_name']}\nAmount: ${int(order['amount_cents']) // 100}.{int(order['amount_cents']) % 100:02d}\n"
                    f"Payment: {order['payment_method']}"
                },
            )
        logger.info("Financially finalized order %s", order_id)
        return True

    @staticmethod
    async def _enqueue(
        connection: aiosqlite.Connection,
        dedupe_key: str,
        kind: str,
        chat_id: int,
        payload: dict[str, Any],
    ) -> None:
        await connection.execute(
            "INSERT OR IGNORE INTO outbox(dedupe_key, kind, chat_id, payload_json) VALUES (?, ?, ?, ?)",
            (dedupe_key, kind, chat_id, json.dumps(payload, ensure_ascii=False)),
        )

    async def _enqueue_admins(
        self,
        connection: aiosqlite.Connection,
        key_prefix: str,
        kind: str,
        payload: dict[str, Any],
    ) -> None:
        cursor = await connection.execute(
            "SELECT telegram_id FROM admins WHERE active = 1"
        )
        admins = await cursor.fetchall()
        await cursor.close()
        for admin in admins:
            telegram_id = int(admin["telegram_id"])
            await self._enqueue(
                connection,
                f"{key_prefix}:admin:{telegram_id}",
                kind,
                telegram_id,
                payload,
            )

    async def expire_order(self, order_id: int, payment_id: int | None = None) -> None:
        async with self.db.transaction() as connection:
            cursor = await connection.execute(
                "SELECT * FROM orders WHERE id = ?", (order_id,)
            )
            order = await cursor.fetchone()
            await cursor.close()
            if order is None or order["financial_processed_at"] is not None:
                return
            if str(order["status"]) not in {
                OrderStatus.CREATED,
                OrderStatus.WAITING_PAYMENT,
            }:
                return
            await connection.execute(
                "UPDATE orders SET status = 'EXPIRED', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (order_id,),
            )
            if payment_id is not None:
                await connection.execute(
                    "UPDATE payments SET status = 'EXPIRED', last_checked_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (payment_id,),
                )
            await self._release_stock(connection, order)

    async def cancel_unpaid_order(self, order_id: int, reason: str) -> None:
        async with self.db.transaction() as connection:
            cursor = await connection.execute(
                "SELECT * FROM orders WHERE id = ?", (order_id,)
            )
            order = await cursor.fetchone()
            await cursor.close()
            if order is None or order["financial_processed_at"] is not None:
                return
            await connection.execute(
                "UPDATE orders SET status = 'FAILED', last_error = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (reason[:500], order_id),
            )
            await self._release_stock(connection, order)

    @staticmethod
    async def _release_stock(
        connection: aiosqlite.Connection, order: aiosqlite.Row
    ) -> None:
        if order["stock_item_id"] is None:
            return
        cursor = await connection.execute(
            "UPDATE product_stock SET status = 'AVAILABLE', order_id = NULL, reserved_at = NULL "
            "WHERE id = ? AND order_id = ? AND status = 'RESERVED'",
            (int(order["stock_item_id"]), int(order["id"])),
        )
        released = cursor.rowcount == 1
        await cursor.close()
        if released and order["product_id"] is not None:
            await connection.execute(
                "UPDATE products SET stock_count = stock_count + 1 WHERE id = ?",
                (int(order["product_id"]),),
            )

    async def refund_balance_order(self, order_id: int, reason: str) -> bool:
        async with self.db.transaction() as connection:
            cursor = await connection.execute(
                "SELECT * FROM orders WHERE id = ?", (order_id,)
            )
            order = await cursor.fetchone()
            await cursor.close()
            if order is None or str(order["payment_method"]) != "BALANCE":
                raise OrderError("Возврат применим только к заказу с баланса")
            if order["refunded_at"] is not None:
                return False
            if order["delivered_at"] is not None:
                raise OrderError("Уже выданный заказ нельзя автоматически вернуть")
            if str(order["delivery_status"]) in {"SENDING", "UNKNOWN"}:
                raise OrderError(
                    "Результат сетевой выдачи ещё уточняется; дождитесь результата перед возвратом"
                )
            await self.balance.apply_in_transaction(
                connection,
                user_id=int(order["user_id"]),
                amount_cents=int(order["amount_cents"]),
                transaction_type="PURCHASE_REFUND",
                idempotency_key=f"order-refund:{order_id}",
                description=reason,
                related_order_id=order_id,
                withdrawable_credit_cents=await self._purchase_withdrawable_debit(
                    connection, order_id
                ),
            )
            await self._reverse_balance_rewards(connection, order_id)
            if order["stock_item_id"] is not None:
                cursor = await connection.execute(
                    "UPDATE product_stock SET status = 'AVAILABLE', order_id = NULL, reserved_at = NULL, sold_at = NULL "
                    "WHERE id = ? AND order_id = ?",
                    (int(order["stock_item_id"]), order_id),
                )
                if cursor.rowcount == 1 and order["product_id"] is not None:
                    await connection.execute(
                        "UPDATE products SET stock_count = stock_count + 1 WHERE id = ?",
                        (int(order["product_id"]),),
                    )
                await cursor.close()
            await connection.execute(
                "UPDATE outbox SET status = 'FAILED', last_error = 'Order refunded' "
                "WHERE dedupe_key = ? AND status != 'SENT'",
                (f"delivery:order:{order_id}",),
            )
            await connection.execute(
                "UPDATE orders SET status = 'REFUNDED', refunded_at = CURRENT_TIMESTAMP, last_error = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (reason[:500], order_id),
            )
            if order["product_id"] is not None:
                await connection.execute(
                    "UPDATE products SET purchases_count = MAX(0, purchases_count - 1) WHERE id = ?",
                    (int(order["product_id"]),),
                )
            return True

    async def _reverse_balance_rewards(
        self, connection: aiosqlite.Connection, order_id: int
    ) -> None:
        cursor = await connection.execute(
            "SELECT user_id, amount_cents FROM cashback_events "
            "WHERE order_id = ? AND reversed_at IS NULL",
            (order_id,),
        )
        cashback = await cursor.fetchone()
        await cursor.close()
        if cashback is not None:
            await self.balance.apply_in_transaction(
                connection,
                user_id=int(cashback["user_id"]),
                amount_cents=-int(cashback["amount_cents"]),
                transaction_type="CASHBACK_REVERSAL",
                idempotency_key=f"cashback-reversal:order:{order_id}",
                description=f"Отмена cashback по возврату заказа #{order_id}",
                related_order_id=order_id,
            )
            await connection.execute(
                "UPDATE cashback_events SET reversed_at = CURRENT_TIMESTAMP WHERE order_id = ?",
                (order_id,),
            )
        cursor = await connection.execute(
            "SELECT referrer_user_id, amount_cents FROM referral_events "
            "WHERE order_id = ? AND reversed_at IS NULL",
            (order_id,),
        )
        referral = await cursor.fetchone()
        await cursor.close()
        if referral is not None:
            await self.balance.apply_in_transaction(
                connection,
                user_id=int(referral["referrer_user_id"]),
                amount_cents=-int(referral["amount_cents"]),
                transaction_type="REFERRAL_REVERSAL",
                idempotency_key=f"referral-reversal:order:{order_id}",
                description=f"Отмена партнёрского начисления по возврату заказа #{order_id}",
                related_order_id=order_id,
            )
            await connection.execute(
                "UPDATE referral_events SET reversed_at = CURRENT_TIMESTAMP WHERE order_id = ?",
                (order_id,),
            )

    @staticmethod
    async def _purchase_withdrawable_debit(
        connection: aiosqlite.Connection, order_id: int
    ) -> int:
        cursor = await connection.execute(
            "SELECT withdrawable_before_cents, withdrawable_after_cents FROM balance_transactions "
            "WHERE idempotency_key = ?",
            (f"purchase:order:{order_id}",),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return 0
        return max(
            0,
            int(row["withdrawable_before_cents"])
            - int(row["withdrawable_after_cents"]),
        )

    async def complete_manual_order(
        self, order_id: int, *, text: str | None, file_id: str | None
    ) -> None:
        if not text and not file_id:
            raise OrderError("Нужен текст или файл для выдачи")
        async with self.db.transaction() as connection:
            cursor = await connection.execute(
                "SELECT o.*, u.telegram_id FROM orders o JOIN users u ON u.id = o.user_id WHERE o.id = ?",
                (order_id,),
            )
            order = await cursor.fetchone()
            await cursor.close()
            retry_manual = bool(
                order
                and str(order["stock_type"]) == ProductType.MANUAL
                and str(order["delivery_status"]) == "FAILED"
            )
            if order is None or (
                str(order["status"]) != OrderStatus.WAITING_DELIVERY
                and not retry_manual
            ):
                raise OrderError("Заказ не ожидает ручную выдачу")
            cursor = await connection.execute(
                "SELECT amount_cents FROM cashback_events WHERE order_id = ?",
                (order_id,),
            )
            cashback_row = await cursor.fetchone()
            await cursor.close()
            payload = {
                "order_id": order_id,
                "product_name": str(order["product_name"]),
                "amount_cents": int(order["amount_cents"]),
                "cashback_cents": int(cashback_row["amount_cents"])
                if cashback_row
                else 0,
                "stock_type": "FILE" if file_id else "TEXT",
                "text": text,
                "file_id": file_id,
            }
            await connection.execute(
                "INSERT INTO outbox(dedupe_key, kind, chat_id, payload_json) VALUES (?, 'ORDER_DELIVERY', ?, ?) "
                "ON CONFLICT(dedupe_key) DO UPDATE SET payload_json = excluded.payload_json, status = 'PENDING', "
                "attempts = 0, available_at = CURRENT_TIMESTAMP, last_error = NULL",
                (
                    f"delivery:order:{order_id}",
                    int(order["telegram_id"]),
                    json.dumps(payload, ensure_ascii=False),
                ),
            )
            await connection.execute(
                "UPDATE orders SET status = 'COMPLETED', delivery_status = 'PENDING', delivery_text = ?, "
                "delivery_file_id = ?, completed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (text, file_id, order_id),
            )

    async def retry_delivery(self, order_id: int) -> bool:
        async with self.db.transaction() as connection:
            cursor = await connection.execute(
                "UPDATE outbox SET status = 'PENDING', attempts = 0, available_at = CURRENT_TIMESTAMP, last_error = NULL "
                "WHERE dedupe_key = ? AND status = 'FAILED'",
                (f"delivery:order:{order_id}",),
            )
            changed = cursor.rowcount == 1
            await cursor.close()
            if changed:
                await connection.execute(
                    "UPDATE orders SET delivery_status = 'PENDING', last_error = NULL, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (order_id,),
                )
            return changed

    async def orders_for_user(
        self, user_id: int, page: int, page_size: int
    ) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT * FROM orders WHERE user_id = ? ORDER BY id DESC LIMIT ? OFFSET ?",
            (user_id, page_size, max(0, page) * page_size),
        )
        return [dict(row) for row in rows]

    async def order_for_user(
        self, order_id: int, user_id: int
    ) -> dict[str, Any] | None:
        row = await self.db.fetchone(
            "SELECT * FROM orders WHERE id = ? AND user_id = ?", (order_id, user_id)
        )
        return dict(row) if row else None

    async def payment_for_order(self, order_id: int) -> dict[str, Any] | None:
        row = await self.db.fetchone(
            "SELECT * FROM payments WHERE order_id = ?", (order_id,)
        )
        return dict(row) if row else None

    async def list_orders(
        self, status: str | None, page: int, page_size: int
    ) -> list[dict[str, Any]]:
        if status:
            rows = await self.db.fetchall(
                "SELECT o.*, u.telegram_id FROM orders o JOIN users u ON u.id = o.user_id "
                "WHERE o.status = ? ORDER BY o.id DESC LIMIT ? OFFSET ?",
                (status, page_size, max(0, page) * page_size),
            )
        else:
            rows = await self.db.fetchall(
                "SELECT o.*, u.telegram_id FROM orders o JOIN users u ON u.id = o.user_id "
                "ORDER BY o.id DESC LIMIT ? OFFSET ?",
                (page_size, max(0, page) * page_size),
            )
        return [dict(row) for row in rows]
