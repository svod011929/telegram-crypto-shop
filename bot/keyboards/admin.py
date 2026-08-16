from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def admin_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 Статистика", callback_data="admin:stats")],
            [
                InlineKeyboardButton(text="🛍 Товары", callback_data="admin:products:0"),
                InlineKeyboardButton(
                    text="📂 Категории", callback_data="admin:categories:0"
                ),
            ],
            [
                InlineKeyboardButton(text="📦 Заказы", callback_data="admin:orders:0"),
                InlineKeyboardButton(
                    text="💳 Платежи", callback_data="admin:payments:0"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="💸 Выплаты", callback_data="admin:withdrawals:0"
                ),
                InlineKeyboardButton(
                    text="👥 Пользователи", callback_data="admin:users"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🤝 Реферальная система",
                    callback_data="admin:setting:global_referral_bp",
                ),
                InlineKeyboardButton(
                    text="🎁 Cashback", callback_data="admin:setting:global_cashback_bp"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="💰 Crypto Pay", callback_data="admin:crypto"
                ),
                InlineKeyboardButton(
                    text="📢 Рассылка", callback_data="admin:broadcast"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🗄 Резервные копии", callback_data="admin:backups"
                ),
                InlineKeyboardButton(
                    text="⚙️ Настройки", callback_data="admin:settings:0"
                ),
            ],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:home")],
        ]
    )


def admin_back(callback_data: str = "admin:menu") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⬅️ Назад", callback_data=callback_data),
                InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:home"),
            ]
        ]
    )
