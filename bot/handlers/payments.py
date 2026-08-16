from aiogram import F, Router
from aiogram.types import CallbackQuery

from bot.middlewares.rate_limit import Cooldowns
from bot.services.crypto_pay import CryptoPayError
from bot.services.payment_checker import PaymentChecker

router = Router(name="payments")


@router.callback_query(F.data.startswith("payment:check:"))
async def check_payment(
    callback: CallbackQuery, payment_checker: PaymentChecker, cooldowns: Cooldowns
) -> None:
    if not await cooldowns.allow(f"check:{callback.from_user.id}", 10):
        await callback.answer(
            "Повторная проверка доступна через 10 секунд", show_alert=True
        )
        return
    payment_id = int(callback.data.rsplit(":", 1)[1])
    try:
        status = await payment_checker.check_for_user(
            payment_id, int(callback.from_user.id)
        )
    except (ValueError, CryptoPayError) as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    messages = {
        "paid": "✅ Оплата найдена. Товар будет отправлен отдельным сообщением.",
        "active": "⏳ Платёж пока не найден.",
        "expired": "⌛ Счёт истёк.",
    }
    await callback.answer(messages.get(status, f"Статус: {status}"), show_alert=True)
