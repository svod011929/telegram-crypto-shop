from __future__ import annotations

from html import escape

from aiogram import F, Router
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bot.handlers.helpers import answer_callback, edit_or_answer
from bot.keyboards.common import pagination
from bot.middlewares.rate_limit import Cooldowns
from bot.services.catalog_service import CatalogService
from bot.services.crypto_pay import CryptoPayError
from bot.services.order_service import OrderError, OrderService
from bot.services.settings_service import SettingsService
from bot.services.user_service import UserService
from bot.utils.money import format_money

router = Router(name="shop")


@router.callback_query(F.data.startswith("shop:categories:"))
async def categories(
    callback: CallbackQuery, catalog: CatalogService, settings: SettingsService
) -> None:
    page = max(0, int(callback.data.rsplit(":", 1)[1]))
    page_size = await settings.get_int("page_size", 8)
    items = await catalog.categories(page=page, page_size=page_size + 1)
    rows = [
        [
            InlineKeyboardButton(
                text=f"{item['name']} ({item['product_count']})",
                callback_data=f"shop:cat:{item['id']}:0",
            )
        ]
        for item in items[:page_size]
    ]
    rows += pagination("shop:categories", page, len(items) > page_size)
    await edit_or_answer(
        callback,
        "🛍 <b>Категории</b>\n\nВыберите категорию:",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await answer_callback(callback)


@router.callback_query(F.data.startswith("shop:cat:"))
async def products(
    callback: CallbackQuery, catalog: CatalogService, settings: SettingsService
) -> None:
    _, _, category_raw, page_raw = callback.data.split(":")
    category_id, page = int(category_raw), max(0, int(page_raw))
    page_size = await settings.get_int("page_size", 8)
    items = await catalog.products(category_id, page=page, page_size=page_size + 1)
    rows = [
        [
            InlineKeyboardButton(
                text=f"{item['name']} · {format_money(int(item['price_cents']), 'USDT')}",
                callback_data=f"shop:product:{item['id']}",
            )
        ]
        for item in items[:page_size]
    ]
    rows += pagination(
        f"shop:cat:{category_id}",
        page,
        len(items) > page_size,
        back="shop:categories:0",
    )
    await edit_or_answer(
        callback, "🛍 <b>Товары</b>", InlineKeyboardMarkup(inline_keyboard=rows)
    )
    await answer_callback(callback)


@router.callback_query(F.data.startswith("shop:product:"))
async def product(callback: CallbackQuery, catalog: CatalogService) -> None:
    product_id = int(callback.data.rsplit(":", 1)[1])
    item = await catalog.product(product_id)
    if item is None:
        await callback.answer("Товар недоступен", show_alert=True)
        return
    availability = ""
    if item["stock_type"] == "UNIQUE":
        availability = f"\nОстаток: {item['stock_count']}"
    old_price = (
        f" (вместо {format_money(int(item['old_price_cents']), 'USDT')})"
        if item["old_price_cents"]
        else ""
    )
    text = (
        f"🛍 <b>{escape(str(item['name']))}</b>\n\n{escape(str(item['description']))}\n\n"
        f"Цена: <b>{format_money(int(item['price_cents']), 'USDT')}</b>{old_price}{availability}"
    )
    rows = [
        [
            InlineKeyboardButton(
                text="🛒 Купить", callback_data=f"shop:buy:{product_id}"
            )
        ],
        [
            InlineKeyboardButton(
                text="⬅️ Назад", callback_data=f"shop:cat:{item['category_id']}:0"
            ),
            InlineKeyboardButton(text="🏠 Меню", callback_data="menu:home"),
        ],
    ]
    if item.get("image_file_id") and isinstance(callback.message, Message):
        await callback.message.answer_photo(
            str(item["image_file_id"]),
            caption=(
                f"🛍 <b>{escape(str(item['name']))}</b>\n"
                f"Цена: <b>{format_money(int(item['price_cents']), 'USDT')}</b>"
            ),
            parse_mode="HTML",
        )
    await edit_or_answer(callback, text, InlineKeyboardMarkup(inline_keyboard=rows))
    await answer_callback(callback)


@router.callback_query(F.data.startswith("shop:buy:"))
async def choose_payment(
    callback: CallbackQuery,
    catalog: CatalogService,
    users: UserService,
    settings: SettingsService,
) -> None:
    product_id = int(callback.data.rsplit(":", 1)[1])
    item = await catalog.product(product_id)
    user = await users.by_telegram_id(int(callback.from_user.id))
    if item is None or user is None:
        await callback.answer("Товар или пользователь не найден", show_alert=True)
        return
    rows: list[list[InlineKeyboardButton]] = []
    if await settings.get_bool("crypto_payment_enabled", True):
        rows.append(
            [
                InlineKeyboardButton(
                    text="💎 Crypto Pay", callback_data=f"pay:crypto:{product_id}"
                )
            ]
        )
    if await settings.get_bool("balance_payment_enabled", True):
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"💰 С баланса ({format_money(int(user['balance_cents']), 'USDT')})",
                    callback_data=f"pay:balance:{product_id}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="⬅️ Назад", callback_data=f"shop:product:{product_id}"
            ),
            InlineKeyboardButton(text="🏠 Меню", callback_data="menu:home"),
        ]
    )
    prompt = (
        f"Выберите оплату для <b>{escape(str(item['name']))}</b>:"
        if len(rows) > 1
        else "⚠️ Все способы оплаты временно отключены."
    )
    await edit_or_answer(
        callback,
        prompt,
        InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await answer_callback(callback)


@router.callback_query(F.data.startswith("pay:crypto:"))
async def pay_crypto(
    callback: CallbackQuery, orders: OrderService, cooldowns: Cooldowns
) -> None:
    if not await cooldowns.allow(f"buy:{callback.from_user.id}", 3):
        await callback.answer("Подождите несколько секунд", show_alert=True)
        return
    product_id = int(callback.data.rsplit(":", 1)[1])
    try:
        order, invoice = await orders.create_crypto_purchase(
            int(callback.from_user.id), product_id
        )
    except (OrderError, CryptoPayError) as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    payment = await orders.payment_for_order(order.id)
    if invoice is None or payment is None:
        text = (
            f"⏳ Заказ #{order.id} создан. Ответ Crypto Pay задерживается.\n"
            "Бот автоматически восстановит счёт; попробуйте открыть товар снова через минуту."
        )
        await edit_or_answer(
            callback,
            text,
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="🏠 Главное меню", callback_data="menu:home"
                        )
                    ]
                ]
            ),
        )
    else:
        rows = [
            [InlineKeyboardButton(text="💎 Оплатить", url=str(payment["invoice_url"]))],
            [
                InlineKeyboardButton(
                    text="🔄 Проверить оплату",
                    callback_data=f"payment:check:{payment['id']}",
                )
            ],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:home")],
        ]
        await edit_or_answer(
            callback,
            f"🧾 <b>Заказ #{order.id}</b>\n\nТовар: {escape(order.product_name)}\n"
            f"Сумма счёта: {format_money(order.amount_cents, str(payment['currency']))}\n\n"
            "Оплата будет обнаружена автоматически.",
            InlineKeyboardMarkup(inline_keyboard=rows),
        )
    await answer_callback(callback)


@router.callback_query(F.data.startswith("pay:balance:"))
async def pay_balance(
    callback: CallbackQuery, orders: OrderService, cooldowns: Cooldowns
) -> None:
    if not await cooldowns.allow(f"buy:{callback.from_user.id}", 3):
        await callback.answer("Подождите несколько секунд", show_alert=True)
        return
    product_id = int(callback.data.rsplit(":", 1)[1])
    try:
        order = await orders.purchase_with_balance(
            int(callback.from_user.id), product_id
        )
    except (OrderError, ValueError) as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await edit_or_answer(
        callback,
        f"✅ Заказ #{order.id} оплачен с внутреннего баланса.\nТовар будет отправлен отдельным сообщением.",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🏠 Главное меню", callback_data="menu:home"
                    )
                ]
            ]
        ),
    )
    await answer_callback(callback)
