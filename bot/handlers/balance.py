from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bot.handlers.helpers import answer_callback, edit_or_answer
from bot.keyboards.common import home_back
from bot.services.balance_service import BalanceService
from bot.services.settings_service import SettingsService
from bot.services.user_service import UserService
from bot.services.withdrawal_service import WithdrawalError, WithdrawalService
from bot.states.user import WithdrawalForm
from bot.utils.money import (
    MoneyError,
    format_basis_points,
    format_money,
    parse_money_to_cents,
)

router = Router(name="balance")


@router.callback_query(F.data == "balance:show")
async def show_balance(
    callback: CallbackQuery,
    users: UserService,
    balance: BalanceService,
    settings: SettingsService,
) -> None:
    user = await users.by_telegram_id(int(callback.from_user.id))
    if user is None:
        await callback.answer("Сначала отправьте /start", show_alert=True)
        return
    history = await balance.history(int(user["id"]), 0, 5)
    lines = [
        f"{item['created_at']} · {item['type']} · {format_money(int(item['amount_cents']), 'USDT')}"
        for item in history
    ]
    text = (
        f"💰 <b>Баланс: {format_money(int(user['balance_cents']), 'USDT')}</b>\n"
        f"Доступно к выводу: {format_money(int(user['withdrawable_balance_cents']), 'USDT')}"
    )
    if lines:
        text += "\n\nПоследние операции:\n" + "\n".join(lines)
    rows = [
        [InlineKeyboardButton(text="💸 Вывести", callback_data="withdraw:start")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:home")],
    ]
    await edit_or_answer(callback, text, InlineKeyboardMarkup(inline_keyboard=rows))
    await answer_callback(callback)


@router.callback_query(F.data == "withdraw:start")
async def withdraw_start(
    callback: CallbackQuery,
    state: FSMContext,
    users: UserService,
    settings: SettingsService,
) -> None:
    user = await users.by_telegram_id(int(callback.from_user.id))
    if user is None:
        await callback.answer("Сначала отправьте /start", show_alert=True)
        return
    mode = await settings.get("withdrawal_mode", "MANUAL")
    minimum = await settings.get_int("min_withdrawal_cents", 100)
    fee_bp = await settings.get_int("withdrawal_fee_bp", 0)
    asset = await settings.get("payout_asset", "USDT")
    if mode == "DISABLED":
        await callback.answer("Вывод временно отключён", show_alert=True)
        return
    await state.set_state(WithdrawalForm.amount)
    await edit_or_answer(
        callback,
        f"💸 Доступно к выводу: {format_money(int(user['withdrawable_balance_cents']), 'USDT')}\n"
        f"Минимум: {format_money(minimum, 'USDT')}\nКомиссия: {format_basis_points(fee_bp)}\n"
        f"Актив выплаты: {asset}\n\nВведите сумму баланса в USDT:",
        home_back("balance:show"),
    )
    await answer_callback(callback)


@router.message(WithdrawalForm.amount)
async def withdraw_amount(
    message: Message,
    state: FSMContext,
    users: UserService,
    withdrawals: WithdrawalService,
) -> None:
    try:
        amount = parse_money_to_cents(message.text or "")
        user = await users.by_telegram_id(int(message.from_user.id))
        if user is None:
            raise WithdrawalError("Пользователь не найден")
        withdrawal_id = await withdrawals.request(int(user["id"]), amount)
    except (MoneyError, WithdrawalError, ValueError) as exc:
        await message.answer(
            f"⚠️ {exc}\nВведите другую сумму или откройте главное меню."
        )
        return
    await state.clear()
    await message.answer(
        f"✅ Заявка на вывод #{withdrawal_id} создана.\nСумма: {format_money(amount, 'USDT')}",
        reply_markup=home_back(),
    )
