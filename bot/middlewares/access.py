from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from bot.services.settings_service import SettingsService
from bot.services.user_service import UserService


class UserAccessMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        actor = data.get("event_from_user")
        if actor is None:
            return await handler(event, data)
        users: UserService = data["users"]
        settings: SettingsService = data["settings"]
        is_admin = await users.is_admin(int(actor.id))
        user = await users.by_telegram_id(int(actor.id))
        if user is not None:
            await users.touch(int(actor.id))
        if user and int(user["banned"]) and not is_admin:
            await self._respond(event, "🚫 Доступ к боту ограничен.")
            return None
        if await settings.get_bool("maintenance_mode", False) and not is_admin:
            await self._respond(event, await settings.get("maintenance_text"))
            return None
        return await handler(event, data)

    @staticmethod
    async def _respond(event: TelegramObject, text: str) -> None:
        if isinstance(event, CallbackQuery):
            await event.answer(text, show_alert=True)
        elif isinstance(event, Message):
            await event.answer(text, parse_mode=None)


class AdminOnlyMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        actor = data.get("event_from_user")
        users: UserService = data["users"]
        role = await users.admin_role(int(actor.id)) if actor is not None else None
        if role is None:
            if isinstance(event, CallbackQuery):
                await event.answer("Нет доступа", show_alert=True)
            elif isinstance(event, Message):
                await event.answer("Нет доступа")
            return None
        if role == "SUPPORT" and not self._support_allowed(event, data):
            if isinstance(event, CallbackQuery):
                await event.answer(
                    "Для роли SUPPORT это действие недоступно", show_alert=True
                )
            elif isinstance(event, Message):
                await event.answer("Для роли SUPPORT это действие недоступно")
            return None
        return await handler(event, data)

    @staticmethod
    def _support_allowed(event: TelegramObject, data: dict[str, Any]) -> bool:
        if isinstance(event, CallbackQuery):
            value = event.data or ""
            allowed = (
                "admin:menu",
                "admin:stats",
                "admin:users",
                "admin:user:",
                "admin:userorders:",
                "admin:usertransactions:",
                "admin:userrefs:",
                "admin:orders:",
                "admin:order:",
                "admin:orderdeliver:",
                "admin:orderretry:",
                "admin:payments:",
            )
            return value.startswith(allowed)
        if isinstance(event, Message):
            text = event.text or ""
            raw_state = str(data.get("raw_state") or "")
            return text.startswith("/admin") or raw_state in {
                "UserSearchForm:query",
                "ManualDeliveryForm:content",
            }
        return False
