from __future__ import annotations

from html import escape

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bot.handlers.helpers import answer_callback, edit_or_answer
from bot.keyboards.common import home_back, main_menu, pagination
from bot.services.order_service import OrderService
from bot.services.settings_service import SettingsService
from bot.services.user_service import UserService
from bot.utils.money import format_money

router = Router(name="user")


async def _show_menu(
    target: Message | CallbackQuery, users: UserService, settings: SettingsService
) -> None:
    telegram_id = int(target.from_user.id)
    shop_name = await settings.get("shop_name", "Crypto Shop")
    keyboard = main_menu(admin=await users.is_admin(telegram_id))
    text = f"🏠 <b>{escape(shop_name)}</b>\n\nВыберите раздел:"
    if isinstance(target, CallbackQuery):
        await edit_or_answer(target, text, keyboard)
        await answer_callback(target)
    else:
        await target.answer(text, reply_markup=keyboard, parse_mode="HTML")


@router.message(CommandStart())
async def start(
    message: Message,
    command: CommandObject,
    users: UserService,
    settings: SettingsService,
) -> None:
    referral_id: int | None = None
    argument = (command.args or "").strip()
    if argument.startswith("ref_") and argument[4:].isdigit():
        referral_id = int(argument[4:])
    _, created = await users.register_or_update(
        int(message.from_user.id),
        message.from_user.username,
        message.from_user.first_name,
        message.from_user.language_code,
        referral_id,
    )
    if created:
        await message.answer(
            await settings.get("welcome_text", "Добро пожаловать!"), parse_mode=None
        )
    await _show_menu(message, users, settings)


@router.message(Command("menu"))
async def menu_command(
    message: Message, users: UserService, settings: SettingsService
) -> None:
    await _show_menu(message, users, settings)


@router.callback_query(F.data == "menu:home")
async def menu_callback(
    callback: CallbackQuery, users: UserService, settings: SettingsService
) -> None:
    await _show_menu(callback, users, settings)


@router.callback_query(F.data == "noop")
async def noop(callback: CallbackQuery) -> None:
    await callback.answer()


@router.callback_query(F.data == "profile:show")
async def profile(callback: CallbackQuery, users: UserService) -> None:
    user = await users.by_telegram_id(int(callback.from_user.id))
    if user is None:
        await callback.answer("Сначала отправьте /start", show_alert=True)
        return
    text = (
        f"👤 <b>Профиль</b>\n\nID: <code>{user['telegram_id']}</code>\n"
        f"Баланс: {format_money(int(user['balance_cents']), 'USDT')}\n"
        f"Покупок: {await _order_count(users, int(user['id']))}\n"
        f"Потрачено: {format_money(int(user['total_spent_cents']), 'USDT')}\n"
        f"Регистрация: {user['registration_date']}"
    )
    await edit_or_answer(callback, text, home_back())
    await answer_callback(callback)


async def _order_count(users: UserService, user_id: int) -> int:
    row = await users.db.fetchone(
        "SELECT COUNT(*) AS count FROM orders WHERE user_id = ?", (user_id,)
    )
    return int(row["count"] if row else 0)


@router.callback_query(F.data == "support:show")
async def support(callback: CallbackQuery, settings: SettingsService) -> None:
    username = (await settings.get("support_username", "")).lstrip("@")
    if username:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="💬 Написать поддержке", url=f"https://t.me/{username}"
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="🏠 Главное меню", callback_data="menu:home"
                    )
                ],
            ]
        )
        text = f"🆘 Поддержка: @{username}"
    else:
        keyboard = home_back()
        text = "🆘 Контакт поддержки пока не настроен."
    await edit_or_answer(callback, text, keyboard)
    await answer_callback(callback)


@router.callback_query(F.data.startswith("purchases:"))
async def purchases(
    callback: CallbackQuery,
    users: UserService,
    orders: OrderService,
    settings: SettingsService,
) -> None:
    user = await users.by_telegram_id(int(callback.from_user.id))
    if user is None:
        await callback.answer("Сначала отправьте /start", show_alert=True)
        return
    page = max(0, int(callback.data.rsplit(":", 1)[1]))
    page_size = await settings.get_int("page_size", 8)
    items = await orders.orders_for_user(int(user["id"]), page, page_size + 1)
    has_next = len(items) > page_size
    rows = [
        [
            InlineKeyboardButton(
                text=f"#{item['id']} · {item['product_name']} · {item['status']}",
                callback_data=f"purchase:{item['id']}",
            )
        ]
        for item in items[:page_size]
    ]
    rows += pagination("purchases", page, has_next)
    text = "📦 <b>Мои покупки</b>" if items else "📦 Покупок пока нет."
    await edit_or_answer(callback, text, InlineKeyboardMarkup(inline_keyboard=rows))
    await answer_callback(callback)


@router.callback_query(F.data.startswith("purchase:"))
async def purchase_card(
    callback: CallbackQuery, users: UserService, orders: OrderService
) -> None:
    user = await users.by_telegram_id(int(callback.from_user.id))
    order_id = int(callback.data.rsplit(":", 1)[1])
    order = await orders.order_for_user(order_id, int(user["id"]) if user else -1)
    if order is None:
        await callback.answer("Заказ не найден", show_alert=True)
        return
    rows = []
    if order["delivery_status"] == "SENT" and order["status"] in {
        "COMPLETED",
        "WAITING_DELIVERY",
    }:
        rows.append(
            [
                InlineKeyboardButton(
                    text="📦 Открыть товар", callback_data=f"purchase:open:{order_id}"
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(text="⬅️ Назад", callback_data="purchases:0"),
            InlineKeyboardButton(text="🏠 Меню", callback_data="menu:home"),
        ]
    )
    text = (
        f"📦 <b>Заказ #{order_id}</b>\n\nТовар: {escape(str(order['product_name']))}\n"
        f"Сумма: {format_money(int(order['amount_cents']), str(order['currency']))}\n"
        f"Дата: {order['created_at']}\nСтатус: {order['status']}\nВыдача: {order['delivery_status']}"
    )
    await edit_or_answer(callback, text, InlineKeyboardMarkup(inline_keyboard=rows))
    await answer_callback(callback)


@router.callback_query(F.data.startswith("purchase:open:"))
async def reopen_purchase(
    callback: CallbackQuery, users: UserService, orders: OrderService
) -> None:
    user = await users.by_telegram_id(int(callback.from_user.id))
    order_id = int(callback.data.rsplit(":", 1)[1])
    order = await orders.order_for_user(order_id, int(user["id"]) if user else -1)
    if order is None or order["delivered_at"] is None:
        await callback.answer("Товар ещё не выдан", show_alert=True)
        return
    if order["delivery_file_id"]:
        await callback.message.answer_document(
            str(order["delivery_file_id"]), caption=f"📦 Заказ #{order_id}"
        )
    else:
        value = order["delivery_text"]
        if order["stock_type"] == "UNIQUE" and order["stock_item_id"]:
            row = await orders.db.fetchone(
                "SELECT value FROM product_stock WHERE id = ?",
                (int(order["stock_item_id"]),),
            )
            value = row["value"] if row else None
        body = str(value or "Содержимое недоступно")
        prefix = f"📦 Заказ #{order_id}\n\n"
        if len(prefix) + len(body) <= 4096:
            await callback.message.answer(prefix + body, parse_mode=None)
        else:
            await callback.message.answer_document(
                BufferedInputFile(
                    body.encode("utf-8"), filename=f"order_{order_id}.txt"
                ),
                caption=f"📦 Заказ #{order_id}",
                parse_mode=None,
            )
    await answer_callback(callback, "Отправлено")
