from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from bot.handlers.helpers import answer_callback, edit_or_answer
from bot.keyboards.admin import admin_back, admin_menu
from bot.middlewares.access import AdminOnlyMiddleware
from bot.services.user_service import UserService
from bot.utils.money import format_money

router = Router(name="admin")
router.message.outer_middleware(AdminOnlyMiddleware())
router.callback_query.outer_middleware(AdminOnlyMiddleware())


@router.message(Command("admin"))
async def admin_command(message: Message) -> None:
    await message.answer(
        "⚙️ <b>Админ-панель</b>", reply_markup=admin_menu(), parse_mode="HTML"
    )


@router.callback_query(F.data == "admin:menu")
async def admin_menu_callback(callback: CallbackQuery) -> None:
    await edit_or_answer(
        callback, "⚙️ <b>Админ-панель</b>\n\nВыберите раздел:", admin_menu()
    )
    await answer_callback(callback)


@router.callback_query(F.data == "admin:stats")
async def statistics(callback: CallbackQuery, users: UserService) -> None:
    periods = [
        ("Сегодня", "date('now')"),
        ("7 дней", "datetime('now', '-7 days')"),
        ("30 дней", "datetime('now', '-30 days')"),
        ("Всё время", None),
    ]
    blocks = []
    for title, boundary in periods:
        blocks.append(await _period_stats(users, title, boundary))
    await edit_or_answer(
        callback, "📊 <b>Статистика</b>\n\n" + "\n\n".join(blocks), admin_back()
    )
    await answer_callback(callback)


async def _period_stats(users: UserService, title: str, boundary: str | None) -> str:
    user_where = f"WHERE registration_date >= {boundary}" if boundary else ""
    order_where = "WHERE financial_processed_at IS NOT NULL AND status != 'REFUNDED'"
    event_where = ""
    withdrawal_where = "WHERE status = 'COMPLETED'"
    if boundary:
        order_where += f" AND COALESCE(paid_at, created_at) >= {boundary}"
        event_where = f"WHERE reversed_at IS NULL AND created_at >= {boundary}"
        withdrawal_where += f" AND completed_at >= {boundary}"
    new_users = await users.db.fetchone(
        f"SELECT COUNT(*) AS count FROM users {user_where}"
    )
    sales = await users.db.fetchone(
        f"SELECT COUNT(*) AS count, COALESCE(SUM(amount_cents), 0) AS volume FROM orders {order_where}"
    )
    if not event_where:
        event_where = "WHERE reversed_at IS NULL"
    cashback = await users.db.fetchone(
        f"SELECT COALESCE(SUM(amount_cents), 0) AS total FROM cashback_events {event_where}"
    )
    referrals = await users.db.fetchone(
        f"SELECT COALESCE(SUM(amount_cents), 0) AS total FROM referral_events {event_where}"
    )
    withdrawals = await users.db.fetchone(
        f"SELECT COALESCE(SUM(amount_cents), 0) AS total FROM withdrawals {withdrawal_where}"
    )
    return (
        f"<b>{title}</b>\n"
        f"Пользователи: {int(new_users['count'])}\nПродажи: {int(sales['count'])}\n"
        f"Оборот: {format_money(int(sales['volume']), 'USDT')}\n"
        f"Cashback: {format_money(int(cashback['total']), 'USDT')}\n"
        f"Referral: {format_money(int(referrals['total']), 'USDT')}\n"
        f"Выплаты: {format_money(int(withdrawals['total']), 'USDT')}"
    )
