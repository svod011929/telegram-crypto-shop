from __future__ import annotations

import asyncio
import json
import logging
import uuid
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from typing import Any

from bot.database.db import Database
from bot.database.models import WithdrawalStatus
from bot.services.balance_service import BalanceService
from bot.services.crypto_pay import (
    CryptoPayAPIError,
    CryptoPayClient,
    CryptoPayTransportError,
)
from bot.services.settings_service import SettingsService
from bot.utils.money import basis_points, cents_to_api_amount, format_money

logger = logging.getLogger(__name__)


class WithdrawalError(ValueError):
    """Raised when a withdrawal request is not valid."""


class WithdrawalService:
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
        self._locks = [asyncio.Lock() for _ in range(32)]

    async def request(self, user_id: int, amount_cents: int) -> int:
        mode = (await self.settings.get("withdrawal_mode", "MANUAL")).upper()
        if mode == "DISABLED":
            raise WithdrawalError("Вывод средств временно отключён")
        minimum = await self.settings.get_int("min_withdrawal_cents", 100)
        if amount_cents < minimum:
            raise WithdrawalError("Сумма меньше минимальной")
        fee_bp = await self.settings.get_int("withdrawal_fee_bp", 0)
        fee = basis_points(amount_cents, fee_bp)
        payout = amount_cents - fee
        if payout <= 0:
            raise WithdrawalError(
                "Сумма выплаты после комиссии должна быть положительной"
            )
        asset = await self.settings.get("payout_asset", "USDT")
        payout_amount = await self._convert_payout(payout, asset)
        spend_id = f"wd-{uuid.uuid4().hex}"
        async with self.db.transaction() as connection:
            cursor = await connection.execute(
                "SELECT id FROM withdrawals WHERE user_id = ? AND status IN ('CREATED', 'PENDING', 'PROCESSING') "
                "AND created_at >= datetime('now', '-60 seconds') LIMIT 1",
                (user_id,),
            )
            recent = await cursor.fetchone()
            await cursor.close()
            if recent is not None:
                raise WithdrawalError("Подождите перед созданием следующей заявки")
            cursor = await connection.execute(
                "INSERT INTO withdrawals(user_id, amount_cents, fee_cents, payout_cents, payout_amount, asset, mode, status, spend_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'CREATED', ?)",
                (
                    user_id,
                    amount_cents,
                    fee,
                    payout,
                    payout_amount,
                    asset,
                    mode,
                    spend_id,
                ),
            )
            withdrawal_id = int(cursor.lastrowid)
            await cursor.close()
            await self.balance.apply_in_transaction(
                connection,
                user_id=user_id,
                amount_cents=-amount_cents,
                transaction_type="WITHDRAWAL",
                idempotency_key=f"withdrawal:reserve:{withdrawal_id}",
                description=f"Резерв для вывода #{withdrawal_id}",
                related_withdrawal_id=withdrawal_id,
            )
            await connection.execute(
                "UPDATE withdrawals SET status = 'PENDING', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (withdrawal_id,),
            )
            notice = json.dumps(
                {
                    "text": f"💸 Новая заявка на вывод #{withdrawal_id}\n"
                    f"User ID: {user_id}\nСумма: {format_money(amount_cents, 'USDT')}\nРежим: {mode}"
                },
                ensure_ascii=False,
            )
            await connection.execute(
                "INSERT OR IGNORE INTO outbox(dedupe_key, kind, chat_id, payload_json) "
                "SELECT 'withdrawal-new:' || ? || ':admin:' || telegram_id, 'ADMIN_NOTICE', telegram_id, ? "
                "FROM admins WHERE active = 1",
                (withdrawal_id, notice),
            )
        logger.info("Created withdrawal %s with spend_id %s", withdrawal_id, spend_id)
        return withdrawal_id

    async def _convert_payout(self, payout_cents: int, asset: str) -> str:
        normalized = asset.upper()
        if normalized in {"USDT", "USDC"}:
            return cents_to_api_amount(payout_cents)
        try:
            rates = await self.crypto.get_exchange_rates()
        except (CryptoPayAPIError, CryptoPayTransportError) as exc:
            raise WithdrawalError("Не удалось получить курс для выплаты") from exc
        match = next(
            (
                item
                for item in rates
                if str(item.get("source", "")).upper() == normalized
                and str(item.get("target", "")).upper() == "USD"
                and bool(item.get("is_valid", True))
            ),
            None,
        )
        if match is None:
            raise WithdrawalError("Курс выбранного актива недоступен")
        try:
            rate = Decimal(str(match["rate"]))
            if not rate.is_finite() or rate <= 0:
                raise InvalidOperation
            amount = (Decimal(payout_cents) / Decimal(100) / rate).quantize(
                Decimal("0.00000001"), rounding=ROUND_DOWN
            )
            if not amount.is_finite():
                raise InvalidOperation
        except (InvalidOperation, KeyError, ZeroDivisionError) as exc:
            raise WithdrawalError("Crypto Pay вернул некорректный курс") from exc
        if amount <= 0:
            raise WithdrawalError("Сумма выплаты слишком мала для выбранного актива")
        return format(amount, "f").rstrip("0").rstrip(".")

    async def process(self, withdrawal_id: int) -> str:
        lock = self._locks[withdrawal_id % len(self._locks)]
        async with lock:
            row = await self.db.fetchone(
                "SELECT w.*, u.telegram_id FROM withdrawals w JOIN users u ON u.id = w.user_id WHERE w.id = ?",
                (withdrawal_id,),
            )
            if row is None:
                raise WithdrawalError("Заявка не найдена")
            status = str(row["status"])
            if status in {
                WithdrawalStatus.COMPLETED,
                WithdrawalStatus.REFUNDED,
                WithdrawalStatus.REJECTED,
            }:
                return status
            if status not in {WithdrawalStatus.PENDING, WithdrawalStatus.PROCESSING}:
                raise WithdrawalError("Заявка не готова к обработке")
            try:
                transfers = await self.crypto.get_transfers(
                    spend_id=str(row["spend_id"]), count=1
                )
            except (CryptoPayAPIError, CryptoPayTransportError):
                logger.exception("Could not reconcile withdrawal %s", withdrawal_id)
                return status
            if transfers:
                transfer_id = self._validated_transfer_id(row, transfers[0])
                await self._complete(withdrawal_id, transfer_id)
                return WithdrawalStatus.COMPLETED
            if str(row["mode"]) == "MANUAL" and status == WithdrawalStatus.PENDING:
                return status
            await self.db.execute(
                "UPDATE withdrawals SET status = 'PROCESSING', attempts = attempts + 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (withdrawal_id,),
            )
            try:
                transfer = await self.crypto.transfer(
                    user_id=int(row["telegram_id"]),
                    asset=str(row["asset"]),
                    amount=str(row["payout_amount"]),
                    spend_id=str(row["spend_id"]),
                    comment=f"Выплата #{withdrawal_id}",
                )
            except CryptoPayTransportError:
                logger.warning(
                    "Unknown transfer outcome for withdrawal %s; will reconcile by spend_id",
                    withdrawal_id,
                )
                return WithdrawalStatus.PROCESSING
            except CryptoPayAPIError as exc:
                if status == WithdrawalStatus.PROCESSING:
                    await self.db.execute(
                        "UPDATE withdrawals SET last_error = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (
                            f"Ambiguous retry response: {exc.error}"[:500],
                            withdrawal_id,
                        ),
                    )
                    logger.warning(
                        "Withdrawal %s remains ambiguous after transfer retry",
                        withdrawal_id,
                    )
                    return WithdrawalStatus.PROCESSING
                await self.refund(
                    withdrawal_id, f"Crypto Pay отказал в выплате: {exc.error}"
                )
                return WithdrawalStatus.REFUNDED
            transfer_id = self._validated_transfer_id(row, transfer)
            await self._complete(withdrawal_id, transfer_id)
            return WithdrawalStatus.COMPLETED

    @staticmethod
    def _validated_transfer_id(withdrawal: Any, transfer: dict[str, Any]) -> int:
        try:
            transfer_id = int(transfer["transfer_id"])
            remote_user = int(transfer["user_id"])
            remote_amount = Decimal(str(transfer["amount"]))
            expected_amount = Decimal(str(withdrawal["payout_amount"]))
        except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
            raise WithdrawalError("Crypto Pay вернул некорректный transfer") from exc
        if transfer_id <= 0:
            raise WithdrawalError("Crypto Pay вернул некорректный transfer_id")
        if str(transfer.get("spend_id", "")) != str(withdrawal["spend_id"]):
            raise WithdrawalError("Transfer spend_id не совпадает с заявкой")
        if remote_user != int(withdrawal["telegram_id"]):
            raise WithdrawalError("Transfer предназначен другому получателю")
        if str(transfer.get("asset", "")).upper() != str(withdrawal["asset"]).upper():
            raise WithdrawalError("Transfer asset не совпадает с заявкой")
        if (
            not remote_amount.is_finite()
            or remote_amount != expected_amount
            or remote_amount <= 0
        ):
            raise WithdrawalError("Transfer amount не совпадает с заявкой")
        remote_status = str(transfer.get("status", "completed")).lower()
        if remote_status != "completed":
            raise WithdrawalError("Transfer ещё не завершён")
        return transfer_id

    async def approve(self, withdrawal_id: int) -> str:
        await self.db.execute(
            "UPDATE withdrawals SET mode = 'AUTO', updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ? AND status = 'PENDING'",
            (withdrawal_id,),
        )
        return await self.process(withdrawal_id)

    async def reject(self, withdrawal_id: int, reason: str) -> None:
        lock = self._locks[withdrawal_id % len(self._locks)]
        async with lock:
            row = await self.db.fetchone(
                "SELECT status FROM withdrawals WHERE id = ?", (withdrawal_id,)
            )
            if row is None or str(row["status"]) != WithdrawalStatus.PENDING:
                raise WithdrawalError("Можно отклонить только ожидающую заявку")
            await self.refund(
                withdrawal_id, f"Заявка отклонена: {reason}", rejected=True
            )

    async def _complete(self, withdrawal_id: int, transfer_id: int) -> None:
        async with self.db.transaction() as connection:
            cursor = await connection.execute(
                "SELECT w.*, u.telegram_id FROM withdrawals w JOIN users u ON u.id = w.user_id WHERE w.id = ?",
                (withdrawal_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row is None or str(row["status"]) == WithdrawalStatus.COMPLETED:
                return
            if str(row["status"]) in {
                WithdrawalStatus.REFUNDED,
                WithdrawalStatus.REJECTED,
            }:
                raise WithdrawalError(
                    "Remote transfer exists for a refunded withdrawal; manual audit required"
                )
            await connection.execute(
                "UPDATE withdrawals SET status = 'COMPLETED', transfer_id = ?, completed_at = CURRENT_TIMESTAMP, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (transfer_id, withdrawal_id),
            )
            await connection.execute(
                "UPDATE users SET total_withdrawn_cents = total_withdrawn_cents + ? WHERE id = ?",
                (int(row["amount_cents"]), int(row["user_id"])),
            )
            await connection.execute(
                "INSERT OR IGNORE INTO outbox(dedupe_key, kind, chat_id, payload_json) VALUES (?, 'TEXT', ?, ?)",
                (
                    f"withdrawal-completed:{withdrawal_id}",
                    int(row["telegram_id"]),
                    json.dumps(
                        {
                            "text": f"✅ Выплата #{withdrawal_id} завершена.\n"
                            f"Отправлено: {row['payout_amount']} {row['asset']}"
                        },
                        ensure_ascii=False,
                    ),
                ),
            )
        logger.info(
            "Completed withdrawal %s as transfer %s", withdrawal_id, transfer_id
        )

    async def refund(
        self, withdrawal_id: int, reason: str, *, rejected: bool = False
    ) -> bool:
        async with self.db.transaction() as connection:
            cursor = await connection.execute(
                "SELECT w.*, u.telegram_id FROM withdrawals w JOIN users u ON u.id = w.user_id WHERE w.id = ?",
                (withdrawal_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row is None:
                raise WithdrawalError("Заявка не найдена")
            if str(row["status"]) in {
                WithdrawalStatus.REFUNDED,
                WithdrawalStatus.REJECTED,
            }:
                return False
            if str(row["status"]) == WithdrawalStatus.COMPLETED:
                raise WithdrawalError(
                    "Завершённую выплату нельзя вернуть автоматически"
                )
            await self.balance.apply_in_transaction(
                connection,
                user_id=int(row["user_id"]),
                amount_cents=int(row["amount_cents"]),
                transaction_type="WITHDRAWAL_REFUND",
                idempotency_key=f"withdrawal:refund:{withdrawal_id}",
                description=reason,
                related_withdrawal_id=withdrawal_id,
            )
            status = (
                WithdrawalStatus.REJECTED if rejected else WithdrawalStatus.REFUNDED
            )
            await connection.execute(
                "UPDATE withdrawals SET status = ?, refunded_at = CURRENT_TIMESTAMP, last_error = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (status, reason[:500], withdrawal_id),
            )
            await connection.execute(
                "INSERT OR IGNORE INTO outbox(dedupe_key, kind, chat_id, payload_json) VALUES (?, 'TEXT', ?, ?)",
                (
                    f"withdrawal-refunded:{withdrawal_id}",
                    int(row["telegram_id"]),
                    json.dumps(
                        {
                            "text": f"↩️ Выплата #{withdrawal_id} не выполнена.\n"
                            f"На баланс возвращено {format_money(int(row['amount_cents']), 'USDT')}"
                        },
                        ensure_ascii=False,
                    ),
                ),
            )
            return True

    async def list(
        self, status: str | None, page: int, page_size: int
    ) -> list[dict[str, Any]]:
        if status:
            rows = await self.db.fetchall(
                "SELECT w.*, u.telegram_id FROM withdrawals w JOIN users u ON u.id = w.user_id "
                "WHERE w.status = ? ORDER BY w.id DESC LIMIT ? OFFSET ?",
                (status, page_size, max(0, page) * page_size),
            )
        else:
            rows = await self.db.fetchall(
                "SELECT w.*, u.telegram_id FROM withdrawals w JOIN users u ON u.id = w.user_id "
                "ORDER BY w.id DESC LIMIT ? OFFSET ?",
                (page_size, max(0, page) * page_size),
            )
        return [dict(row) for row in rows]


class WithdrawalWorker:
    def __init__(self, db: Database, service: WithdrawalService) -> None:
        self.db = db
        self.service = service
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        logger.info("Withdrawal worker started")
        while not self._stop.is_set():
            try:
                rows = await self.db.fetchall(
                    "SELECT id FROM withdrawals WHERE status = 'PROCESSING' OR (status = 'PENDING' AND mode = 'AUTO') "
                    "ORDER BY id LIMIT 20"
                )
                for row in rows:
                    await self.service.process(int(row["id"]))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Withdrawal worker tick failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=10)
            except TimeoutError:
                continue
        logger.info("Withdrawal worker stopped")
