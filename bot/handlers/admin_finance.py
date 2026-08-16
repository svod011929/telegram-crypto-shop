from __future__ import annotations

import uuid
from html import escape

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bot.handlers.helpers import answer_callback, edit_or_answer
from bot.keyboards.admin import admin_back
from bot.middlewares.access import AdminOnlyMiddleware
from bot.services.balance_service import BalanceService, InsufficientBalance
from bot.services.crypto_pay import CryptoPayError
from bot.services.order_service import OrderError, OrderService
from bot.services.settings_service import SettingsService
from bot.services.user_service import UserService
from bot.services.withdrawal_service import WithdrawalError, WithdrawalService
from bot.states.admin import (
    BalanceAdjustForm,
    ManualDeliveryForm,
    UserSearchForm,
    WithdrawalRejectForm,
)
from bot.utils.money import MoneyError, format_money, parse_money_to_cents

router = Router(name="admin_finance")
router.message.outer_middleware(AdminOnlyMiddleware())
router.callback_query.outer_middleware(AdminOnlyMiddleware())


@router.callback_query(F.data == "admin:users")
async def users_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(UserSearchForm.query)
    await edit_or_answer(
        callback, "👥 Введите Telegram ID, внутренний ID или username:", admin_back()
    )
    await answer_callback(callback)


@router.message(UserSearchForm.query)
async def users_search(message: Message, state: FSMContext, users: UserService) -> None:
    matches = await users.find(message.text or "")
    await state.clear()
    if not matches:
        await message.answer("Пользователь не найден.", reply_markup=admin_back())
        return
    rows = [
        [
            InlineKeyboardButton(
                text=f"#{item['id']} · {item['telegram_id']} · @{item['username'] or '-'}",
                callback_data=f"admin:user:{item['id']}",
            )
        ]
        for item in matches
    ]
    rows.append(
        [InlineKeyboardButton(text="⬅️ Админ-панель", callback_data="admin:menu")]
    )
    await message.answer(
        "Результаты:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
    )


@router.callback_query(F.data.regexp(r"^admin:user:\d+$"))
async def user_card(callback: CallbackQuery, users: UserService) -> None:
    user_id = int(callback.data.rsplit(":", 1)[1])
    user = await users.by_id(user_id)
    if user is None:
        await callback.answer("Пользователь не найден", show_alert=True)
        return
    referrer = (
        await users.by_id(int(user["referrer_id"])) if user["referrer_id"] else None
    )
    count = await users.db.fetchone(
        "SELECT COUNT(*) AS count FROM orders WHERE user_id = ?", (user_id,)
    )
    text = (
        f"👤 <b>User #{user_id}</b>\nTelegram ID: <code>{user['telegram_id']}</code>\n"
        f"Username: @{escape(str(user['username'] or '-'))}\nBalance: {format_money(int(user['balance_cents']), 'USDT')}\n"
        f"Withdrawable: {format_money(int(user['withdrawable_balance_cents']), 'USDT')}\n"
        f"Orders: {int(count['count']) if count else 0}\nSpent: {format_money(int(user['total_spent_cents']), 'USDT')}\n"
        f"Referral earned: {format_money(int(user['total_referral_earned_cents']), 'USDT')}\n"
        f"Referrer: {referrer['telegram_id'] if referrer else '-'}\nRegistered: {user['registration_date']}\n"
        f"Status: {'🚫 Banned' if user['banned'] else '✅ Active'}"
    )
    rows = [
        [
            InlineKeyboardButton(
                text="➕ Начислить", callback_data=f"admin:userbalance:{user_id}:add"
            ),
            InlineKeyboardButton(
                text="➖ Списать", callback_data=f"admin:userbalance:{user_id}:subtract"
            ),
        ],
        [
            InlineKeyboardButton(
                text="📦 Заказы", callback_data=f"admin:userorders:{user_id}:0"
            ),
            InlineKeyboardButton(
                text="💰 Транзакции",
                callback_data=f"admin:usertransactions:{user_id}:0",
            ),
        ],
        [
            InlineKeyboardButton(
                text="👥 Рефералы", callback_data=f"admin:userrefs:{user_id}:0"
            )
        ],
        [
            InlineKeyboardButton(
                text="✅ Разблокировать" if user["banned"] else "🚫 Заблокировать",
                callback_data=f"admin:userban:{user_id}",
            )
        ],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:users")],
    ]
    await edit_or_answer(callback, text, InlineKeyboardMarkup(inline_keyboard=rows))
    await answer_callback(callback)


@router.callback_query(F.data.startswith("admin:userban:"))
async def user_ban(callback: CallbackQuery, users: UserService) -> None:
    user_id = int(callback.data.rsplit(":", 1)[1])
    user = await users.by_id(user_id)
    if user is None:
        await callback.answer("Пользователь не найден", show_alert=True)
        return
    if int(user["telegram_id"]) == users.superadmin_id:
        await callback.answer("Нельзя заблокировать SUPERADMIN", show_alert=True)
        return
    await users.set_banned(user_id, not bool(user["banned"]))
    await callback.answer("Статус изменён")
    await _render_user_card(callback, users, user_id)


async def _render_user_card(
    callback: CallbackQuery, users: UserService, user_id: int
) -> None:
    copied = callback.model_copy(update={"data": f"admin:user:{user_id}"})
    await user_card(copied, users)


@router.callback_query(F.data.startswith("admin:userbalance:"))
async def balance_adjust_start(callback: CallbackQuery, state: FSMContext) -> None:
    _, _, user_raw, operation = callback.data.split(":")
    await state.set_state(BalanceAdjustForm.amount)
    await state.update_data(user_id=int(user_raw), operation=operation)
    await edit_or_answer(
        callback, "Введите сумму в USDT:", admin_back(f"admin:user:{user_raw}")
    )
    await answer_callback(callback)


@router.message(BalanceAdjustForm.amount)
async def balance_adjust_amount(message: Message, state: FSMContext) -> None:
    try:
        amount = parse_money_to_cents(message.text or "")
    except MoneyError as exc:
        await message.answer(f"⚠️ {exc}")
        return
    await state.update_data(amount_cents=amount)
    await state.set_state(BalanceAdjustForm.reason)
    await message.answer("Обязательно укажите причину операции:")


@router.message(BalanceAdjustForm.reason)
async def balance_adjust_reason(
    message: Message, state: FSMContext, balance: BalanceService
) -> None:
    reason = (message.text or "").strip()
    if len(reason) < 3:
        await message.answer("Причина слишком короткая.")
        return
    data = await state.get_data()
    amount = int(data["amount_cents"])
    operation = str(data["operation"])
    signed = amount if operation == "add" else -amount
    transaction_type = "ADMIN_ADD" if signed > 0 else "ADMIN_SUBTRACT"
    try:
        _, resulting = await balance.change(
            user_id=int(data["user_id"]),
            amount_cents=signed,
            transaction_type=transaction_type,
            idempotency_key=f"admin-adjust:{message.from_user.id}:{uuid.uuid4().hex}",
            description=f"Администратор {message.from_user.id}: {reason}",
        )
    except InsufficientBalance as exc:
        await message.answer(f"⚠️ {exc}")
        return
    user_id = int(data["user_id"])
    await state.clear()
    await message.answer(
        f"✅ Операция записана в ledger. Новый баланс: {format_money(resulting, 'USDT')}",
        reply_markup=admin_back(f"admin:user:{user_id}"),
    )


@router.callback_query(F.data.startswith("admin:usertransactions:"))
async def user_transactions(
    callback: CallbackQuery, balance: BalanceService, settings: SettingsService
) -> None:
    _, _, user_raw, page_raw = callback.data.split(":")
    user_id, page = int(user_raw), int(page_raw)
    size = await settings.get_int("page_size", 8)
    items = await balance.history(user_id, page, size + 1)
    lines = [
        f"#{item['id']} {item['type']} {format_money(int(item['amount_cents']), 'USDT')} → {format_money(int(item['balance_after_cents']), 'USDT')}"
        for item in items[:size]
    ]
    rows = _pager(
        f"admin:usertransactions:{user_id}",
        page,
        len(items) > size,
        f"admin:user:{user_id}",
    )
    await edit_or_answer(
        callback,
        "💰 <b>Транзакции</b>\n\n" + ("\n".join(lines) or "Нет операций"),
        InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await answer_callback(callback)


@router.callback_query(F.data.startswith("admin:userorders:"))
async def user_orders(
    callback: CallbackQuery, orders: OrderService, settings: SettingsService
) -> None:
    _, _, user_raw, page_raw = callback.data.split(":")
    user_id, page = int(user_raw), int(page_raw)
    size = await settings.get_int("page_size", 8)
    items = await orders.orders_for_user(user_id, page, size + 1)
    rows = [
        [
            InlineKeyboardButton(
                text=f"#{item['id']} {item['product_name']} {item['status']}",
                callback_data=f"admin:order:{item['id']}",
            )
        ]
        for item in items[:size]
    ]
    rows += _pager(
        f"admin:userorders:{user_id}", page, len(items) > size, f"admin:user:{user_id}"
    )
    await edit_or_answer(
        callback, "📦 Заказы пользователя", InlineKeyboardMarkup(inline_keyboard=rows)
    )
    await answer_callback(callback)


@router.callback_query(F.data.startswith("admin:userrefs:"))
async def user_referrals(
    callback: CallbackQuery, users: UserService, settings: SettingsService
) -> None:
    _, _, user_raw, page_raw = callback.data.split(":")
    user_id, page = int(user_raw), int(page_raw)
    size = await settings.get_int("page_size", 8)
    rows_data = await users.db.fetchall(
        "SELECT * FROM users WHERE referrer_id = ? ORDER BY id DESC LIMIT ? OFFSET ?",
        (user_id, size + 1, page * size),
    )
    lines = [
        f"#{row['id']} · {row['telegram_id']} · @{row['username'] or '-'}"
        for row in rows_data[:size]
    ]
    rows = _pager(
        f"admin:userrefs:{user_id}",
        page,
        len(rows_data) > size,
        f"admin:user:{user_id}",
    )
    await edit_or_answer(
        callback,
        "👥 <b>Рефералы</b>\n\n" + ("\n".join(lines) or "Нет рефералов"),
        InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await answer_callback(callback)


@router.callback_query(F.data.startswith("admin:orders:"))
async def orders_list(
    callback: CallbackQuery, orders: OrderService, settings: SettingsService
) -> None:
    page = int(callback.data.rsplit(":", 1)[1])
    size = await settings.get_int("page_size", 8)
    items = await orders.list_orders(None, page, size + 1)
    rows = [
        [
            InlineKeyboardButton(
                text=f"#{item['id']} · {item['status']} · {item['product_name']}",
                callback_data=f"admin:order:{item['id']}",
            )
        ]
        for item in items[:size]
    ]
    rows += _pager("admin:orders", page, len(items) > size, "admin:menu")
    await edit_or_answer(
        callback, "📦 <b>Заказы</b>", InlineKeyboardMarkup(inline_keyboard=rows)
    )
    await answer_callback(callback)


@router.callback_query(F.data.regexp(r"^admin:order:\d+$"))
async def order_card(callback: CallbackQuery, orders: OrderService) -> None:
    order_id = int(callback.data.rsplit(":", 1)[1])
    row = await orders.db.fetchone(
        "SELECT o.*, u.telegram_id FROM orders o JOIN users u ON u.id = o.user_id WHERE o.id = ?",
        (order_id,),
    )
    if row is None:
        await callback.answer("Заказ не найден", show_alert=True)
        return
    rows = []
    if row["status"] == "WAITING_DELIVERY" or (
        row["stock_type"] == "MANUAL" and row["delivery_status"] == "FAILED"
    ):
        rows.append(
            [
                InlineKeyboardButton(
                    text=(
                        "📤 Заменить выдачу"
                        if row["delivery_status"] == "FAILED"
                        else "📤 Выдать вручную"
                    ),
                    callback_data=f"admin:orderdeliver:{order_id}",
                )
            ]
        )
    if (
        row["payment_method"] == "BALANCE"
        and row["delivered_at"] is None
        and row["refunded_at"] is None
        and row["delivery_status"] not in {"SENDING", "UNKNOWN"}
    ):
        rows.append(
            [
                InlineKeyboardButton(
                    text="↩️ Вернуть на баланс",
                    callback_data=f"admin:orderrefund:{order_id}",
                )
            ]
        )
    if row["delivery_status"] == "FAILED":
        rows.append(
            [
                InlineKeyboardButton(
                    text="🔄 Повторить выдачу",
                    callback_data=f"admin:orderretry:{order_id}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:orders:0")])
    text = (
        f"📦 <b>Заказ #{order_id}</b>\nUser: {row['telegram_id']}\nТовар: {escape(str(row['product_name']))}\n"
        f"Сумма: {format_money(int(row['amount_cents']), 'USDT')}\nОплата: {row['payment_method']}\n"
        f"Статус: {row['status']}\nВыдача: {row['delivery_status']}\nСоздан: {row['created_at']}"
    )
    await edit_or_answer(callback, text, InlineKeyboardMarkup(inline_keyboard=rows))
    await answer_callback(callback)


@router.callback_query(F.data.startswith("admin:orderdeliver:"))
async def manual_delivery_start(callback: CallbackQuery, state: FSMContext) -> None:
    order_id = int(callback.data.rsplit(":", 1)[1])
    await state.set_state(ManualDeliveryForm.content)
    await state.update_data(order_id=order_id)
    await edit_or_answer(
        callback,
        "Отправьте текст или файл для покупателя:",
        admin_back(f"admin:order:{order_id}"),
    )
    await answer_callback(callback)


@router.message(ManualDeliveryForm.content)
async def manual_delivery(
    message: Message, state: FSMContext, orders: OrderService
) -> None:
    data = await state.get_data()
    text = message.text
    file_id = message.document.file_id if message.document else None
    try:
        await orders.complete_manual_order(
            int(data["order_id"]), text=text, file_id=file_id
        )
    except OrderError as exc:
        await message.answer(f"⚠️ {exc}")
        return
    order_id = int(data["order_id"])
    await state.clear()
    await message.answer(
        "✅ Выдача поставлена в очередь.",
        reply_markup=admin_back(f"admin:order:{order_id}"),
    )


@router.callback_query(F.data.startswith("admin:orderrefund:"))
async def order_refund(callback: CallbackQuery, orders: OrderService) -> None:
    order_id = int(callback.data.rsplit(":", 1)[1])
    try:
        changed = await orders.refund_balance_order(
            order_id, f"Возврат администратором {callback.from_user.id}"
        )
    except (OrderError, InsufficientBalance) as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.answer(
        "Средства возвращены" if changed else "Возврат уже выполнен", show_alert=True
    )


@router.callback_query(F.data.startswith("admin:orderretry:"))
async def order_retry(callback: CallbackQuery, orders: OrderService) -> None:
    order_id = int(callback.data.rsplit(":", 1)[1])
    changed = await orders.retry_delivery(order_id)
    await callback.answer(
        "Выдача снова поставлена в очередь" if changed else "Нет failed-выдачи",
        show_alert=True,
    )


@router.callback_query(F.data.startswith("admin:payments:"))
async def payments_list(
    callback: CallbackQuery, users: UserService, settings: SettingsService
) -> None:
    page = int(callback.data.rsplit(":", 1)[1])
    size = await settings.get_int("page_size", 8)
    items = await users.db.fetchall(
        "SELECT p.*, o.id AS local_order_id, u.telegram_id FROM payments p JOIN orders o ON o.id = p.order_id "
        "JOIN users u ON u.id = o.user_id ORDER BY p.id DESC LIMIT ? OFFSET ?",
        (size + 1, page * size),
    )
    lines = [
        f"Invoice {row['invoice_id']} · Order #{row['local_order_id']} · {row['status']} · {format_money(int(row['amount_cents']), str(row['currency']))}"
        for row in items[:size]
    ]
    rows = _pager("admin:payments", page, len(items) > size, "admin:menu")
    await edit_or_answer(
        callback,
        "💳 <b>Платежи</b>\n\n" + ("\n".join(lines) or "Нет платежей"),
        InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await answer_callback(callback)


@router.callback_query(F.data.startswith("admin:withdrawals:"))
async def withdrawals_list(
    callback: CallbackQuery, withdrawals: WithdrawalService, settings: SettingsService
) -> None:
    page = int(callback.data.rsplit(":", 1)[1])
    size = await settings.get_int("page_size", 8)
    items = await withdrawals.list(None, page, size + 1)
    rows = [
        [
            InlineKeyboardButton(
                text=f"#{item['id']} · {item['status']} · {format_money(int(item['amount_cents']), 'USDT')}",
                callback_data=f"admin:withdrawal:{item['id']}",
            )
        ]
        for item in items[:size]
    ]
    rows += _pager("admin:withdrawals", page, len(items) > size, "admin:menu")
    await edit_or_answer(
        callback, "💸 <b>Выплаты</b>", InlineKeyboardMarkup(inline_keyboard=rows)
    )
    await answer_callback(callback)


@router.callback_query(F.data.regexp(r"^admin:withdrawal:\d+$"))
async def withdrawal_card(
    callback: CallbackQuery, withdrawals: WithdrawalService
) -> None:
    withdrawal_id = int(callback.data.rsplit(":", 1)[1])
    row = await withdrawals.db.fetchone(
        "SELECT w.*, u.telegram_id FROM withdrawals w JOIN users u ON u.id = w.user_id WHERE w.id = ?",
        (withdrawal_id,),
    )
    if row is None:
        await callback.answer("Заявка не найдена", show_alert=True)
        return
    rows = []
    if row["status"] == "PENDING":
        rows.append(
            [
                InlineKeyboardButton(
                    text="✅ Одобрить",
                    callback_data=f"admin:withdrawapprove:{withdrawal_id}",
                ),
                InlineKeyboardButton(
                    text="❌ Отклонить",
                    callback_data=f"admin:withdrawreject:{withdrawal_id}",
                ),
            ]
        )
    if row["status"] == "PROCESSING":
        rows.append(
            [
                InlineKeyboardButton(
                    text="🔄 Сверить",
                    callback_data=f"admin:withdrawapprove:{withdrawal_id}",
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:withdrawals:0")]
    )
    text = (
        f"💸 <b>Выплата #{withdrawal_id}</b>\nUser: {row['telegram_id']}\n"
        f"Списано: {format_money(int(row['amount_cents']), 'USDT')}\n"
        f"Комиссия: {format_money(int(row['fee_cents']), 'USDT')}\n"
        f"К выплате: {row['payout_amount']} {row['asset']}\n"
        f"Статус: {row['status']}\nSpend ID: <code>{row['spend_id']}</code>"
    )
    await edit_or_answer(callback, text, InlineKeyboardMarkup(inline_keyboard=rows))
    await answer_callback(callback)


@router.callback_query(F.data.startswith("admin:withdrawapprove:"))
async def withdrawal_approve(
    callback: CallbackQuery, withdrawals: WithdrawalService
) -> None:
    withdrawal_id = int(callback.data.rsplit(":", 1)[1])
    try:
        status = await withdrawals.approve(withdrawal_id)
    except (WithdrawalError, CryptoPayError) as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.answer(f"Статус: {status}", show_alert=True)


@router.callback_query(F.data.startswith("admin:withdrawreject:"))
async def withdrawal_reject_start(callback: CallbackQuery, state: FSMContext) -> None:
    withdrawal_id = int(callback.data.rsplit(":", 1)[1])
    await state.set_state(WithdrawalRejectForm.reason)
    await state.update_data(withdrawal_id=withdrawal_id)
    await edit_or_answer(
        callback,
        "Укажите причину отклонения:",
        admin_back(f"admin:withdrawal:{withdrawal_id}"),
    )
    await answer_callback(callback)


@router.message(WithdrawalRejectForm.reason)
async def withdrawal_reject(
    message: Message, state: FSMContext, withdrawals: WithdrawalService
) -> None:
    reason = (message.text or "").strip()
    if len(reason) < 3:
        await message.answer("Причина слишком короткая.")
        return
    data = await state.get_data()
    try:
        await withdrawals.reject(int(data["withdrawal_id"]), reason)
    except WithdrawalError as exc:
        await message.answer(f"⚠️ {exc}")
        return
    await state.clear()
    await message.answer(
        "✅ Заявка отклонена, средства возвращены.",
        reply_markup=admin_back("admin:withdrawals:0"),
    )


@router.callback_query(F.data == "admin:crypto")
async def crypto_info(callback: CallbackQuery, orders: OrderService) -> None:
    try:
        me = await orders.crypto.get_me()
        balances = await orders.crypto.get_balance()
        stats = await orders.crypto.get_stats()
        active = await orders.db.fetchone(
            "SELECT COUNT(*) AS count FROM payments WHERE status = 'ACTIVE'"
        )
        paid_out = await orders.db.fetchone(
            "SELECT COALESCE(SUM(amount_cents), 0) AS total FROM withdrawals WHERE status = 'COMPLETED'"
        )
    except CryptoPayError as exc:
        await edit_or_answer(
            callback, f"🔴 Crypto Pay недоступен\n\n{exc}", admin_back()
        )
        await answer_callback(callback)
        return
    balance_lines = [
        f"{item.get('currency_code')}: {item.get('available')}" for item in balances
    ]
    text = (
        f"🟢 <b>Crypto Pay: Connected</b>\nApp: {escape(str(me.get('name', me.get('app_id', '-'))))}\n\n"
        f"💰 Баланс:\n"
        + ("\n".join(balance_lines) or "—")
        + f"\n\n📥 Получено: {stats.get('volume', '0')} USD\n"
        f"📤 Выплачено: {format_money(int(paid_out['total']) if paid_out else 0, 'USDT')}\n"
        f"🧾 Активных счетов: {int(active['count']) if active else 0}"
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить", callback_data="admin:crypto")],
            [
                InlineKeyboardButton(
                    text="🧪 Проверить API", callback_data="admin:crypto:test"
                )
            ],
            [InlineKeyboardButton(text="⬅️ Админ-панель", callback_data="admin:menu")],
        ]
    )
    await edit_or_answer(callback, text, keyboard)
    await answer_callback(callback)


@router.callback_query(F.data == "admin:crypto:test")
async def crypto_test(callback: CallbackQuery, orders: OrderService) -> None:
    try:
        await orders.crypto.get_me()
    except CryptoPayError as exc:
        await callback.answer(f"Ошибка: {exc}", show_alert=True)
    else:
        await callback.answer("Crypto Pay API отвечает", show_alert=True)


def _pager(
    prefix: str, page: int, has_next: bool, back: str
) -> list[list[InlineKeyboardButton]]:
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"{prefix}:{page - 1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"{prefix}:{page + 1}"))
    rows = [nav] if nav else []
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=back)])
    return rows
