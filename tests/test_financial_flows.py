from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import Any

from bot.database.db import Database
from bot.services.backup_service import BackupService
from bot.services.balance_service import BalanceService, InsufficientBalance
from bot.services.broadcast_service import BroadcastService
from bot.services.catalog_service import CatalogService
from bot.services.crypto_pay import CryptoPayAPIError, CryptoPayTransportError
from bot.services.order_service import OrderError, OrderService
from bot.services.outbox_service import OutboxWorker
from bot.services.payment_checker import PaymentChecker
from bot.services.settings_service import SettingsService
from bot.services.user_service import UserService
from bot.services.withdrawal_service import WithdrawalService
from bot.utils.money import MoneyError, parse_money_to_cents


class FakeCryptoPay:
    def __init__(self) -> None:
        self.invoices: list[dict[str, Any]] = []
        self.transfers: list[dict[str, Any]] = []
        self.transfer_calls = 0
        self.timeout_after_transfer = False
        self.api_error_on_transfer = False

    async def create_invoice(self, **params: Any) -> dict[str, Any]:
        invoice = {
            "invoice_id": len(self.invoices) + 1000,
            "bot_invoice_url": f"https://example.test/invoice/{len(self.invoices) + 1000}",
            "asset": params["asset"],
            "amount": params["amount"],
            "payload": params["payload"],
            "status": "active",
            "expiration_date": "2099-01-01T00:00:00+00:00",
        }
        self.invoices.append(invoice)
        return dict(invoice)

    async def get_invoices(self, **params: Any) -> list[dict[str, Any]]:
        invoice_ids = params.get("invoice_ids")
        if not invoice_ids:
            return [dict(item) for item in self.invoices]
        wanted = {int(value) for value in str(invoice_ids).split(",")}
        return [
            dict(item) for item in self.invoices if int(item["invoice_id"]) in wanted
        ]

    async def transfer(self, **params: Any) -> dict[str, Any]:
        self.transfer_calls += 1
        if self.api_error_on_transfer:
            raise CryptoPayAPIError("transfer", "SPEND_ID_ALREADY_USED")
        existing = next(
            (item for item in self.transfers if item["spend_id"] == params["spend_id"]),
            None,
        )
        if existing is None:
            existing = {
                "transfer_id": len(self.transfers) + 9000,
                "spend_id": params["spend_id"],
                "user_id": str(params["user_id"]),
                "asset": params["asset"],
                "amount": params["amount"],
                "status": "completed",
            }
            self.transfers.append(existing)
        if self.timeout_after_transfer:
            self.timeout_after_transfer = False
            raise CryptoPayTransportError("simulated timeout after accepted transfer")
        return dict(existing)

    async def get_transfers(self, **params: Any) -> list[dict[str, Any]]:
        spend_id = params.get("spend_id")
        return [
            dict(item)
            for item in self.transfers
            if not spend_id or item["spend_id"] == spend_id
        ]


class BlockingBot:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def send_message(self, *_args: Any, **_kwargs: Any) -> None:
        self.started.set()
        await self.release.wait()

    async def send_document(self, *_args: Any, **_kwargs: Any) -> None:
        self.started.set()
        await self.release.wait()


class CaptureBot:
    def __init__(self) -> None:
        self.messages = 0
        self.documents = 0

    async def send_message(self, *_args: Any, **_kwargs: Any) -> None:
        self.messages += 1

    async def send_document(self, *_args: Any, **_kwargs: Any) -> None:
        self.documents += 1


class FinancialFlowsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="crypto_shop_tests_")
        self.path = Path(self.temporary.name) / "bot.db"
        self.db = Database(self.path)
        await self.db.connect()
        self.settings = SettingsService(self.db)
        self.users = UserService(self.db, 999)
        await self.users.ensure_superadmin()
        self.balance = BalanceService(self.db)
        self.catalog = CatalogService(self.db)
        self.crypto = FakeCryptoPay()
        self.orders = OrderService(self.db, self.balance, self.settings, self.crypto)  # type: ignore[arg-type]
        self.category_id = await self.catalog.add_category("Ключи")

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self.temporary.cleanup()

    async def _user(
        self, telegram_id: int, referrer: int | None = None
    ) -> dict[str, Any]:
        user, _ = await self.users.register_or_update(
            telegram_id, f"user{telegram_id}", f"User {telegram_id}", "ru", referrer
        )
        return user

    def test_money_parser_never_silently_rounds(self) -> None:
        self.assertEqual(parse_money_to_cents("10.25"), 1025)
        with self.assertRaises(MoneyError):
            parse_money_to_cents("10.259")

    async def test_external_payment_is_idempotent_with_cashback_and_referral(
        self,
    ) -> None:
        referrer = await self._user(101)
        buyer = await self._user(202, 101)
        product_id = await self.catalog.create_product(
            category_id=self.category_id,
            name="Premium Key",
            description="Key",
            price_cents=1000,
            stock_type="TEXT",
            content_text="SECRET",
            telegram_file_id=None,
        )
        order, _ = await self.orders.create_crypto_purchase(202, product_id)
        payment = await self.orders.payment_for_order(order.id)
        self.assertIsNotNone(payment)
        self.crypto.invoices[0].update(
            status="paid", paid_at="2026-08-16T12:00:00+00:00"
        )

        result = await self.orders.observe_invoice(
            int(payment["id"]), dict(self.crypto.invoices[0])
        )
        duplicate = await self.orders.observe_invoice(
            int(payment["id"]), dict(self.crypto.invoices[0])
        )

        self.assertEqual(result, "paid")
        self.assertEqual(duplicate, "paid")
        buyer_after = await self.users.by_id(int(buyer["id"]))
        referrer_after = await self.users.by_id(int(referrer["id"]))
        self.assertEqual(int(buyer_after["balance_cents"]), 50)
        self.assertEqual(int(referrer_after["balance_cents"]), 100)
        ledger = await self.db.fetchall(
            "SELECT type FROM balance_transactions ORDER BY id"
        )
        self.assertEqual(
            [row["type"] for row in ledger], ["CASHBACK", "REFERRAL_REWARD"]
        )
        saved_order = await self.db.fetchone(
            "SELECT * FROM orders WHERE id = ?", (order.id,)
        )
        self.assertEqual(saved_order["status"], "COMPLETED")
        self.assertIsNotNone(saved_order["financial_processed_at"])
        deliveries = await self.db.fetchone(
            "SELECT COUNT(*) AS count FROM outbox WHERE dedupe_key = ?",
            (f"delivery:order:{order.id}",),
        )
        self.assertEqual(int(deliveries["count"]), 1)

    async def test_unique_stock_and_double_callback(self) -> None:
        first = await self._user(301)
        second = await self._user(302)
        for user in (first, second):
            await self.balance.change(
                user_id=int(user["id"]),
                amount_cents=1000,
                transaction_type="BONUS",
                idempotency_key=f"seed:{user['id']}",
                description="test funding",
            )
        product_id = await self.catalog.create_product(
            category_id=self.category_id,
            name="Unique",
            description="",
            price_cents=300,
            stock_type="UNIQUE",
            content_text=None,
            telegram_file_id=None,
        )
        added, available = await self.catalog.add_stock(product_id, ["KEY-A", "KEY-B"])
        self.assertEqual((added, available), (2, 2))

        order_one = await self.orders.purchase_with_balance(301, product_id)
        duplicate = await self.orders.purchase_with_balance(301, product_id)
        order_two = await self.orders.purchase_with_balance(302, product_id)

        self.assertEqual(order_one.id, duplicate.id)
        rows = await self.db.fetchall(
            "SELECT value, order_id FROM product_stock WHERE product_id = ? AND status = 'SOLD' ORDER BY id",
            (product_id,),
        )
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0]["order_id"], rows[1]["order_id"])
        first_after = await self.users.by_id(int(first["id"]))
        self.assertEqual(int(first_after["balance_cents"]), 700)
        debits = await self.db.fetchone(
            "SELECT COUNT(*) AS count FROM balance_transactions WHERE user_id = ? AND type = 'PURCHASE'",
            (int(first["id"]),),
        )
        self.assertEqual(int(debits["count"]), 1)
        self.assertNotEqual(order_one.id, order_two.id)

    async def test_restart_resumes_paid_invoice(self) -> None:
        await self._user(401)
        product_id = await self.catalog.create_product(
            category_id=self.category_id,
            name="Restart",
            description="",
            price_cents=500,
            stock_type="TEXT",
            content_text="AFTER-RESTART",
            telegram_file_id=None,
        )
        order, _ = await self.orders.create_crypto_purchase(401, product_id)
        self.crypto.invoices[0].update(
            status="paid", paid_at="2026-08-16T12:30:00+00:00"
        )
        await self.db.close()

        self.db = Database(self.path)
        await self.db.connect()
        self.settings = SettingsService(self.db)
        self.balance = BalanceService(self.db)
        restarted_orders = OrderService(
            self.db, self.balance, self.settings, self.crypto
        )  # type: ignore[arg-type]
        checker = PaymentChecker(self.db, restarted_orders, self.settings, 5, 100)
        await checker.tick()

        saved = await self.db.fetchone(
            "SELECT status, financial_processed_at FROM orders WHERE id = ?",
            (order.id,),
        )
        self.assertEqual(saved["status"], "COMPLETED")
        self.assertIsNotNone(saved["financial_processed_at"])

    async def test_withdrawal_timeout_is_reconciled_without_second_transfer(
        self,
    ) -> None:
        user = await self._user(501)
        await self.balance.change(
            user_id=int(user["id"]),
            amount_cents=2000,
            transaction_type="BONUS",
            idempotency_key="seed:withdrawal",
            description="test funding",
        )
        await self.settings.set("withdrawal_mode", "AUTO", 999)
        service = WithdrawalService(self.db, self.balance, self.settings, self.crypto)  # type: ignore[arg-type]
        withdrawal_id = await service.request(int(user["id"]), 1000)
        self.crypto.timeout_after_transfer = True

        first_status = await service.process(withdrawal_id)
        second_status = await service.process(withdrawal_id)

        self.assertEqual(first_status, "PROCESSING")
        self.assertEqual(second_status, "COMPLETED")
        self.assertEqual(self.crypto.transfer_calls, 1)
        row = await self.db.fetchone(
            "SELECT * FROM withdrawals WHERE id = ?", (withdrawal_id,)
        )
        self.assertEqual(row["status"], "COMPLETED")
        user_after = await self.users.by_id(int(user["id"]))
        self.assertEqual(int(user_after["balance_cents"]), 1000)

    async def test_ambiguous_withdrawal_api_error_never_refunds_reserved_money(
        self,
    ) -> None:
        user = await self._user(550)
        await self.balance.change(
            user_id=int(user["id"]),
            amount_cents=1000,
            transaction_type="BONUS",
            idempotency_key="ambiguous-withdrawal:seed",
            description="test funding",
        )
        await self.settings.set("withdrawal_mode", "AUTO", 999)
        service = WithdrawalService(self.db, self.balance, self.settings, self.crypto)  # type: ignore[arg-type]
        withdrawal_id = await service.request(int(user["id"]), 500)
        await self.db.execute(
            "UPDATE withdrawals SET status = 'PROCESSING', attempts = 1 WHERE id = ?",
            (withdrawal_id,),
        )
        self.crypto.api_error_on_transfer = True

        status = await service.process(withdrawal_id)

        self.assertEqual(status, "PROCESSING")
        row = await self.db.fetchone(
            "SELECT status, refunded_at FROM withdrawals WHERE id = ?",
            (withdrawal_id,),
        )
        self.assertEqual(row["status"], "PROCESSING")
        self.assertIsNone(row["refunded_at"])
        saved = await self.users.by_id(int(user["id"]))
        self.assertEqual(int(saved["balance_cents"]), 500)

    async def test_definitive_first_withdrawal_api_error_refunds_money(self) -> None:
        user = await self._user(560)
        await self.balance.change(
            user_id=int(user["id"]),
            amount_cents=1000,
            transaction_type="BONUS",
            idempotency_key="failed-withdrawal:seed",
            description="test funding",
        )
        await self.settings.set("withdrawal_mode", "AUTO", 999)
        service = WithdrawalService(self.db, self.balance, self.settings, self.crypto)  # type: ignore[arg-type]
        withdrawal_id = await service.request(int(user["id"]), 500)
        self.crypto.api_error_on_transfer = True

        status = await service.process(withdrawal_id)

        self.assertEqual(status, "REFUNDED")
        saved = await self.users.by_id(int(user["id"]))
        self.assertEqual(int(saved["balance_cents"]), 1000)

    async def test_insufficient_balance_never_becomes_negative(self) -> None:
        user = await self._user(601)
        with self.assertRaises(InsufficientBalance):
            await self.balance.change(
                user_id=int(user["id"]),
                amount_cents=-1,
                transaction_type="ADMIN_SUBTRACT",
                idempotency_key="negative:test",
                description="must fail",
            )
        saved = await self.users.by_id(int(user["id"]))
        self.assertEqual(int(saved["balance_cents"]), 0)
        count = await self.db.fetchone(
            "SELECT COUNT(*) AS count FROM balance_transactions"
        )
        self.assertEqual(int(count["count"]), 0)

    async def test_disabled_payment_methods_are_enforced_in_service(self) -> None:
        user = await self._user(620)
        await self.balance.change(
            user_id=int(user["id"]),
            amount_cents=1000,
            transaction_type="BONUS",
            idempotency_key="disabled-payments:seed",
            description="test funding",
        )
        product_id = await self.catalog.create_product(
            category_id=self.category_id,
            name="Payment controls",
            description="",
            price_cents=500,
            stock_type="TEXT",
            content_text="ITEM",
            telegram_file_id=None,
        )
        await self.settings.set("crypto_payment_enabled", "false", 999)
        await self.settings.set("balance_payment_enabled", "false", 999)

        with self.assertRaises(OrderError):
            await self.orders.create_crypto_purchase(620, product_id)
        with self.assertRaises(OrderError):
            await self.orders.purchase_with_balance(620, product_id)

        count = await self.db.fetchone("SELECT COUNT(*) AS count FROM orders")
        self.assertEqual(int(count["count"]), 0)

    async def test_nonwithdrawable_cashback_is_enforced_by_ledger(self) -> None:
        user = await self._user(650)
        await self.settings.set("cashback_withdrawable", "false", 999)
        product_id = await self.catalog.create_product(
            category_id=self.category_id,
            name="Restricted cashback",
            description="",
            price_cents=1000,
            stock_type="TEXT",
            content_text="ITEM",
            telegram_file_id=None,
        )
        order, _ = await self.orders.create_crypto_purchase(650, product_id)
        payment = await self.orders.payment_for_order(order.id)
        self.crypto.invoices[0].update(
            status="paid", paid_at="2026-08-16T13:00:00+00:00"
        )
        await self.orders.observe_invoice(
            int(payment["id"]), dict(self.crypto.invoices[0])
        )
        await self.balance.change(
            user_id=int(user["id"]),
            amount_cents=100,
            transaction_type="BONUS",
            idempotency_key="withdrawable:seed",
            description="test funding",
        )
        current = await self.users.by_id(int(user["id"]))
        self.assertEqual(int(current["balance_cents"]), 150)
        self.assertEqual(int(current["withdrawable_balance_cents"]), 100)
        service = WithdrawalService(self.db, self.balance, self.settings, self.crypto)  # type: ignore[arg-type]
        with self.assertRaises(InsufficientBalance):
            await service.request(int(user["id"]), 101)
        withdrawal_id = await service.request(int(user["id"]), 100)
        saved = await self.users.by_id(int(user["id"]))
        self.assertEqual(int(saved["balance_cents"]), 50)
        self.assertEqual(int(saved["withdrawable_balance_cents"]), 0)
        self.assertGreater(withdrawal_id, 0)

    async def test_balance_order_refund_reverses_optional_rewards(self) -> None:
        referrer = await self._user(660)
        buyer = await self._user(661, 660)
        await self.settings.set("balance_rewards_enabled", "true", 999)
        await self.balance.change(
            user_id=int(buyer["id"]),
            amount_cents=1000,
            transaction_type="BONUS",
            idempotency_key="refund:seed",
            description="test funding",
        )
        product_id = await self.catalog.create_product(
            category_id=self.category_id,
            name="Refundable",
            description="",
            price_cents=1000,
            stock_type="TEXT",
            content_text="ITEM",
            telegram_file_id=None,
        )
        order = await self.orders.purchase_with_balance(661, product_id)
        buyer_rewarded = await self.users.by_id(int(buyer["id"]))
        referrer_rewarded = await self.users.by_id(int(referrer["id"]))
        self.assertEqual(int(buyer_rewarded["balance_cents"]), 50)
        self.assertEqual(int(referrer_rewarded["balance_cents"]), 100)

        changed = await self.orders.refund_balance_order(order.id, "test refund")

        self.assertTrue(changed)
        buyer_after = await self.users.by_id(int(buyer["id"]))
        referrer_after = await self.users.by_id(int(referrer["id"]))
        self.assertEqual(int(buyer_after["balance_cents"]), 1000)
        self.assertEqual(int(referrer_after["balance_cents"]), 0)
        self.assertEqual(int(buyer_after["total_cashback_cents"]), 0)
        self.assertEqual(int(referrer_after["total_referral_earned_cents"]), 0)
        cashback = await self.db.fetchone(
            "SELECT reversed_at FROM cashback_events WHERE order_id = ?", (order.id,)
        )
        referral = await self.db.fetchone(
            "SELECT reversed_at FROM referral_events WHERE order_id = ?", (order.id,)
        )
        self.assertIsNotNone(cashback["reversed_at"])
        self.assertIsNotNone(referral["reversed_at"])

    async def test_refund_is_blocked_while_delivery_is_sending(self) -> None:
        user = await self._user(670)
        await self.balance.change(
            user_id=int(user["id"]),
            amount_cents=1000,
            transaction_type="BONUS",
            idempotency_key="delivery-race:seed",
            description="test funding",
        )
        product_id = await self.catalog.create_product(
            category_id=self.category_id,
            name="Race-safe delivery",
            description="",
            price_cents=500,
            stock_type="TEXT",
            content_text="ITEM",
            telegram_file_id=None,
        )
        order = await self.orders.purchase_with_balance(670, product_id)
        bot = BlockingBot()
        worker = OutboxWorker(self.db, bot, self.settings)  # type: ignore[arg-type]
        sending = asyncio.create_task(worker.tick())
        await asyncio.wait_for(bot.started.wait(), timeout=1)
        row = await self.db.fetchone(
            "SELECT delivery_status FROM orders WHERE id = ?", (order.id,)
        )
        self.assertEqual(row["delivery_status"], "SENDING")
        with self.assertRaises(OrderError):
            await self.orders.refund_balance_order(order.id, "must wait")
        bot.release.set()
        await sending
        delivered = await self.db.fetchone(
            "SELECT delivery_status FROM orders WHERE id = ?", (order.id,)
        )
        self.assertEqual(delivered["delivery_status"], "SENT")

    async def test_long_text_delivery_uses_single_document(self) -> None:
        user = await self._user(680)
        await self.balance.change(
            user_id=int(user["id"]),
            amount_cents=1000,
            transaction_type="BONUS",
            idempotency_key="long-delivery:seed",
            description="test funding",
        )
        product_id = await self.catalog.create_product(
            category_id=self.category_id,
            name="Long delivery",
            description="",
            price_cents=500,
            stock_type="TEXT",
            content_text="X" * 4096,
            telegram_file_id=None,
        )
        order = await self.orders.purchase_with_balance(680, product_id)
        bot = CaptureBot()
        worker = OutboxWorker(self.db, bot, self.settings)  # type: ignore[arg-type]

        await worker.tick()

        self.assertEqual(bot.messages, 0)
        self.assertEqual(bot.documents, 1)
        delivered = await self.db.fetchone(
            "SELECT delivery_status FROM orders WHERE id = ?", (order.id,)
        )
        self.assertEqual(delivered["delivery_status"], "SENT")

    async def test_self_referral_and_referrer_change_are_rejected(self) -> None:
        self_user = await self._user(701, 701)
        self.assertIsNone(self_user["referrer_id"])
        referrer = await self._user(702)
        existing, created = await self.users.register_or_update(
            701, "changed", "Changed", "ru", 702
        )
        self.assertFalse(created)
        self.assertIsNone(existing["referrer_id"])
        referrer_after = await self.users.by_id(int(referrer["id"]))
        self.assertEqual(int(referrer_after["number_of_referrals"]), 0)

    async def test_sqlite_backup_is_consistent(self) -> None:
        await self._user(801)
        service = BackupService(
            self.db, self.settings, Path(self.temporary.name) / "backups"
        )
        backup = await service.create(999)
        self.assertTrue(backup.is_file())
        self.assertGreater(backup.stat().st_size, 0)
        records = await service.list(1)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["filename"], backup.name)

    async def test_broadcast_is_persisted_and_can_be_cancelled(self) -> None:
        await self._user(850)
        service = BroadcastService(self.db)
        broadcast_id = await service.create_draft(
            999,
            text="Service notice",
            buttons=[{"text": "Open", "url": "https://example.test"}],
        )

        recipients = await service.launch(broadcast_id)

        self.assertEqual(recipients, 1)
        queued = await self.db.fetchone(
            "SELECT COUNT(*) AS count FROM outbox WHERE kind = 'BROADCAST'"
        )
        self.assertEqual(int(queued["count"]), 1)
        await service.cancel(broadcast_id)
        row = await self.db.fetchone(
            "SELECT status FROM broadcasts WHERE id = ?", (broadcast_id,)
        )
        outbox = await self.db.fetchone(
            "SELECT status FROM outbox WHERE kind = 'BROADCAST'"
        )
        self.assertEqual(row["status"], "CANCELLED")
        self.assertEqual(outbox["status"], "FAILED")


if __name__ == "__main__":
    unittest.main()
