from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from bot.database.db import Database
from bot.services.crypto_pay import CryptoPayError
from bot.services.order_service import OrderService
from bot.services.settings_service import SettingsService

logger = logging.getLogger(__name__)


class PaymentChecker:
    """One centralized polling worker for all active Crypto Pay invoices."""

    def __init__(
        self,
        db: Database,
        orders: OrderService,
        settings: SettingsService,
        default_interval: int,
        batch_size: int,
    ) -> None:
        self.db = db
        self.orders = orders
        self.settings = settings
        self.default_interval = default_interval
        self.batch_size = min(1000, max(1, batch_size))
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        logger.info("Payment checker started")
        while not self._stop.is_set():
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Payment checker tick failed")
            interval = await self.settings.get_int(
                "payment_poll_interval", self.default_interval
            )
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=max(2, interval))
            except TimeoutError:
                continue
        logger.info("Payment checker stopped")

    async def tick(self) -> None:
        await self._recover_unattached()
        paid_orders = await self.db.fetchall(
            "SELECT id FROM orders WHERE status IN ('PAID', 'PROCESSING') AND financial_processed_at IS NULL LIMIT ?",
            (self.batch_size,),
        )
        for order in paid_orders:
            try:
                await self.orders.process_paid_order(int(order["id"]))
            except Exception:
                logger.exception("Failed to resume paid order %s", order["id"])
        payments = await self.db.fetchall(
            "SELECT id, invoice_id, order_id, expiration_date FROM payments WHERE status = 'ACTIVE' ORDER BY id LIMIT ?",
            (self.batch_size,),
        )
        if not payments:
            return
        ids = ",".join(str(int(row["invoice_id"])) for row in payments)
        try:
            remote = await self.orders.crypto.get_invoices(
                invoice_ids=ids, count=len(payments)
            )
        except CryptoPayError:
            logger.exception("Could not poll Crypto Pay invoices")
            return
        by_id = {
            int(item["invoice_id"]): item
            for item in remote
            if item.get("invoice_id") is not None
        }
        for payment in payments:
            invoice = by_id.get(int(payment["invoice_id"]))
            if invoice is None:
                logger.warning(
                    "Crypto Pay omitted active invoice %s; keeping it pending for reconciliation",
                    payment["invoice_id"],
                )
                continue
            try:
                await self.orders.observe_invoice(int(payment["id"]), invoice)
            except Exception:
                logger.exception("Failed to process invoice %s", payment["invoice_id"])

    async def _recover_unattached(self) -> None:
        pending = await self.db.fetchall(
            "SELECT o.id, o.created_at, u.telegram_id FROM orders o JOIN users u ON u.id = o.user_id "
            "WHERE o.payment_method = 'CRYPTO' AND o.status = 'CREATED' ORDER BY o.id LIMIT ?",
            (self.batch_size,),
        )
        if not pending:
            return
        try:
            remote = await self.orders.crypto.get_invoices(count=1000)
        except CryptoPayError:
            return
        by_payload = {str(item.get("payload", "")): item for item in remote}
        for row in pending:
            order_id = int(row["id"])
            payload = f"order:{order_id}:user:{int(row['telegram_id'])}"
            invoice = by_payload.get(payload)
            if invoice is not None:
                try:
                    await self.orders.attach_invoice(order_id, invoice, payload)
                except Exception:
                    logger.exception("Could not recover invoice for order %s", order_id)
            elif self._older_than(str(row["created_at"]), 300):
                await self.orders.cancel_unpaid_order(
                    order_id, "Invoice creation outcome was not recoverable"
                )

    async def check_for_user(self, payment_id: int, telegram_id: int) -> str:
        payment = await self.db.fetchone(
            "SELECT p.* FROM payments p JOIN orders o ON o.id = p.order_id JOIN users u ON u.id = o.user_id "
            "WHERE p.id = ? AND u.telegram_id = ?",
            (payment_id, telegram_id),
        )
        if payment is None:
            raise ValueError("Счёт не найден")
        if str(payment["status"]) == "PAID":
            await self.orders.process_paid_order(int(payment["order_id"]))
            return "paid"
        if str(payment["status"]) != "ACTIVE":
            return str(payment["status"]).lower()
        invoices = await self.orders.crypto.get_invoices(
            invoice_ids=str(int(payment["invoice_id"])), count=1
        )
        if not invoices:
            return "active"
        return await self.orders.observe_invoice(int(payment["id"]), invoices[0])

    @staticmethod
    def _older_than(value: str, seconds: int) -> bool:
        try:
            created = datetime.fromisoformat(value)
            if created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            return (datetime.now(UTC) - created).total_seconds() > seconds
        except ValueError:
            return False
