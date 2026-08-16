from __future__ import annotations

import uuid
from typing import Any

import aiosqlite

from bot.database.db import Database


class InsufficientBalance(ValueError):
    """Raised when a debit would make a balance negative."""


class BalanceService:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def apply_in_transaction(
        self,
        connection: aiosqlite.Connection,
        *,
        user_id: int,
        amount_cents: int,
        transaction_type: str,
        idempotency_key: str,
        description: str,
        related_order_id: int | None = None,
        related_withdrawal_id: int | None = None,
        withdrawable_credit_cents: int | None = None,
    ) -> tuple[bool, int]:
        if amount_cents == 0:
            raise ValueError("Нулевая операция баланса запрещена")
        cursor = await connection.execute(
            "SELECT balance_after_cents FROM balance_transactions WHERE idempotency_key = ?",
            (idempotency_key,),
        )
        existing = await cursor.fetchone()
        await cursor.close()
        if existing is not None:
            return False, int(existing["balance_after_cents"])
        cursor = await connection.execute(
            "SELECT balance_cents, withdrawable_balance_cents FROM users WHERE id = ?",
            (user_id,),
        )
        user = await cursor.fetchone()
        await cursor.close()
        if user is None:
            raise ValueError("Пользователь не найден")
        before = int(user["balance_cents"])
        withdrawable_before = int(user["withdrawable_balance_cents"])
        after = before + amount_cents
        if after < 0:
            raise InsufficientBalance("Недостаточно средств")
        if amount_cents > 0:
            if withdrawable_credit_cents is None:
                withdrawable_credit_cents = (
                    0 if transaction_type == "CASHBACK" else amount_cents
                )
            if not 0 <= withdrawable_credit_cents <= amount_cents:
                raise ValueError("Некорректная withdrawable-часть операции")
            withdrawable_after = withdrawable_before + withdrawable_credit_cents
        else:
            debit = -amount_cents
            if transaction_type == "WITHDRAWAL":
                if withdrawable_before < debit:
                    raise InsufficientBalance(
                        "Недостаточно средств, доступных для вывода"
                    )
                withdrawable_after = withdrawable_before - debit
            else:
                restricted_before = before - withdrawable_before
                withdrawable_debit = max(0, debit - restricted_before)
                withdrawable_after = withdrawable_before - withdrawable_debit
        if not 0 <= withdrawable_after <= after:
            raise RuntimeError("Withdrawable balance invariant violated")
        cursor = await connection.execute(
            "UPDATE users SET balance_cents = ?, withdrawable_balance_cents = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ? AND balance_cents = ? AND withdrawable_balance_cents = ?",
            (after, withdrawable_after, user_id, before, withdrawable_before),
        )
        if cursor.rowcount != 1:
            await cursor.close()
            raise RuntimeError("Concurrent balance update rejected")
        await cursor.close()
        await connection.execute(
            "INSERT INTO balance_transactions(transaction_id, idempotency_key, user_id, type, amount_cents, "
            "balance_before_cents, balance_after_cents, withdrawable_before_cents, withdrawable_after_cents, "
            "related_order_id, related_withdrawal_id, description) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(uuid.uuid4()),
                idempotency_key,
                user_id,
                transaction_type,
                amount_cents,
                before,
                after,
                withdrawable_before,
                withdrawable_after,
                related_order_id,
                related_withdrawal_id,
                description,
            ),
        )
        total_column = {
            "REFERRAL_REWARD": "total_referral_earned_cents",
            "CASHBACK": "total_cashback_cents",
        }.get(transaction_type)
        if total_column and amount_cents > 0:
            await connection.execute(
                f"UPDATE users SET {total_column} = {total_column} + ? WHERE id = ?",
                (amount_cents, user_id),
            )
        if transaction_type == "PURCHASE" and amount_cents < 0:
            await connection.execute(
                "UPDATE users SET total_spent_cents = total_spent_cents + ? WHERE id = ?",
                (-amount_cents, user_id),
            )
        if transaction_type == "PURCHASE_REFUND" and amount_cents > 0:
            await connection.execute(
                "UPDATE users SET total_spent_cents = MAX(0, total_spent_cents - ?) WHERE id = ?",
                (amount_cents, user_id),
            )
        if transaction_type == "CASHBACK_REVERSAL" and amount_cents < 0:
            await connection.execute(
                "UPDATE users SET total_cashback_cents = MAX(0, total_cashback_cents - ?) WHERE id = ?",
                (-amount_cents, user_id),
            )
        if transaction_type == "REFERRAL_REVERSAL" and amount_cents < 0:
            await connection.execute(
                "UPDATE users SET total_referral_earned_cents = MAX(0, total_referral_earned_cents - ?) WHERE id = ?",
                (-amount_cents, user_id),
            )
        return True, after

    async def change(
        self,
        *,
        user_id: int,
        amount_cents: int,
        transaction_type: str,
        idempotency_key: str,
        description: str,
        related_order_id: int | None = None,
        related_withdrawal_id: int | None = None,
        withdrawable_credit_cents: int | None = None,
    ) -> tuple[bool, int]:
        async with self.db.transaction() as connection:
            return await self.apply_in_transaction(
                connection,
                user_id=user_id,
                amount_cents=amount_cents,
                transaction_type=transaction_type,
                idempotency_key=idempotency_key,
                description=description,
                related_order_id=related_order_id,
                related_withdrawal_id=related_withdrawal_id,
                withdrawable_credit_cents=withdrawable_credit_cents,
            )

    async def history(
        self, user_id: int, page: int, page_size: int
    ) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT * FROM balance_transactions WHERE user_id = ? ORDER BY id DESC LIMIT ? OFFSET ?",
            (user_id, page_size, max(0, page) * page_size),
        )
        return [dict(row) for row in rows]
