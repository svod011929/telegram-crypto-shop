from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def main_menu(*, admin: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="🛍 Магазин", callback_data="shop:categories:0")],
        [
            InlineKeyboardButton(text="💰 Баланс", callback_data="balance:show"),
            InlineKeyboardButton(
                text="👥 Партнёрская программа", callback_data="referral:show"
            ),
        ],
        [
            InlineKeyboardButton(text="📦 Мои покупки", callback_data="purchases:0"),
            InlineKeyboardButton(text="💸 Вывести", callback_data="withdraw:start"),
        ],
        [
            InlineKeyboardButton(text="👤 Профиль", callback_data="profile:show"),
            InlineKeyboardButton(text="🆘 Поддержка", callback_data="support:show"),
        ],
    ]
    if admin:
        rows.append(
            [InlineKeyboardButton(text="⚙️ Админ-панель", callback_data="admin:menu")]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def home_back(back_callback: str | None = None) -> InlineKeyboardMarkup:
    row = []
    if back_callback:
        row.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=back_callback))
    row.append(InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:home"))
    return InlineKeyboardMarkup(inline_keyboard=[row])


def pagination(
    prefix: str, page: int, has_next: bool, *, back: str = "menu:home"
) -> list[list[InlineKeyboardButton]]:
    row: list[InlineKeyboardButton] = []
    if page > 0:
        row.append(InlineKeyboardButton(text="◀️", callback_data=f"{prefix}:{page - 1}"))
    row.append(InlineKeyboardButton(text=f"{page + 1}", callback_data="noop"))
    if has_next:
        row.append(InlineKeyboardButton(text="▶️", callback_data=f"{prefix}:{page + 1}"))
    return [
        row,
        [
            InlineKeyboardButton(text="⬅️ Назад", callback_data=back),
            InlineKeyboardButton(text="🏠 Меню", callback_data="menu:home"),
        ],
    ]
