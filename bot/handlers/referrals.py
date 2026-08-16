from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from bot.handlers.helpers import answer_callback, edit_or_answer
from bot.services.user_service import UserService
from bot.utils.money import format_money

router = Router(name="referrals")


@router.callback_query(F.data == "referral:show")
async def referral_show(callback: CallbackQuery, bot: Bot, users: UserService) -> None:
    user = await users.by_telegram_id(int(callback.from_user.id))
    if user is None:
        await callback.answer("Сначала отправьте /start", show_alert=True)
        return
    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start=ref_{user['telegram_id']}"
    text = (
        f"👥 <b>Партнёрская программа</b>\n\n"
        f"👥 Рефералов: {user['number_of_referrals']}\n"
        f"💵 Заработано всего: {format_money(int(user['total_referral_earned_cents']), 'USDT')}\n"
        f"💰 Доступно к выводу: {format_money(int(user['withdrawable_balance_cents']), 'USDT')}\n\n"
        f"🔗 Ваша ссылка:\n<code>{link}</code>"
    )
    share = f"https://t.me/share/url?url={link}"
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📊 Статистика", callback_data="referral:stats"
                )
            ],
            [InlineKeyboardButton(text="💸 Вывести", callback_data="withdraw:start")],
            [InlineKeyboardButton(text="🔗 Поделиться ссылкой", url=share)],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:home")],
        ]
    )
    await edit_or_answer(callback, text, keyboard)
    await answer_callback(callback)


@router.callback_query(F.data == "referral:stats")
async def referral_stats(callback: CallbackQuery, users: UserService) -> None:
    user = await users.by_telegram_id(int(callback.from_user.id))
    if user is None:
        await callback.answer("Сначала отправьте /start", show_alert=True)
        return
    row = await users.db.fetchone(
        "SELECT "
        "COALESCE(SUM(CASE WHEN created_at >= date('now') THEN amount_cents ELSE 0 END), 0) AS today, "
        "COALESCE(SUM(CASE WHEN created_at >= datetime('now', '-7 days') THEN amount_cents ELSE 0 END), 0) AS week, "
        "COALESCE(SUM(CASE WHEN created_at >= datetime('now', '-30 days') THEN amount_cents ELSE 0 END), 0) AS month, "
        "COALESCE(SUM(amount_cents), 0) AS total, COUNT(*) AS rewarded_orders "
        "FROM referral_events WHERE referrer_user_id = ? AND reversed_at IS NULL",
        (int(user["id"]),),
    )
    text = (
        "📊 <b>Партнёрская статистика</b>\n\n"
        f"Сегодня: {format_money(int(row['today']), 'USDT')}\n"
        f"7 дней: {format_money(int(row['week']), 'USDT')}\n"
        f"30 дней: {format_money(int(row['month']), 'USDT')}\n"
        f"Всего: {format_money(int(row['total']), 'USDT')}\n"
        f"Оплаченных реферальных заказов: {int(row['rewarded_orders'])}"
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⬅️ Назад", callback_data="referral:show"),
                InlineKeyboardButton(text="🏠 Меню", callback_data="menu:home"),
            ]
        ]
    )
    await edit_or_answer(callback, text, keyboard)
    await answer_callback(callback)
