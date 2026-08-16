import logging

from aiogram import Router
from aiogram.types import ErrorEvent

logger = logging.getLogger(__name__)
router = Router(name="errors")


@router.error()
async def global_error(event: ErrorEvent) -> bool:
    exception = event.exception
    logger.error(
        "Unhandled Telegram update error",
        exc_info=(type(exception), exception, exception.__traceback__),
    )
    update = event.update
    try:
        if update.callback_query:
            await update.callback_query.answer(
                "⚠️ Произошла временная ошибка. Попробуйте ещё раз.", show_alert=True
            )
        elif update.message:
            await update.message.answer(
                "⚠️ Произошла временная ошибка. Попробуйте ещё раз через несколько секунд."
            )
    except Exception:
        logger.exception("Could not send user-facing error notice")
    return True
