from __future__ import annotations

from html import escape

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bot.handlers.helpers import answer_callback, edit_or_answer
from bot.keyboards.admin import admin_back
from bot.middlewares.access import AdminOnlyMiddleware
from bot.services.backup_service import BackupService
from bot.services.broadcast_service import BroadcastService
from bot.services.settings_service import SettingError, SettingsService
from bot.services.user_service import UserService
from bot.states.admin import BroadcastForm, SettingEditForm
from bot.utils.money import MoneyError, format_money, parse_money_to_cents

router = Router(name="admin_system")
router.message.outer_middleware(AdminOnlyMiddleware())
router.callback_query.outer_middleware(AdminOnlyMiddleware())


@router.callback_query(F.data.startswith("admin:settings:"))
async def settings_list(callback: CallbackQuery, settings: SettingsService) -> None:
    page = int(callback.data.rsplit(":", 1)[1])
    page_size = 8
    items = await settings.all()
    window = items[page * page_size : (page + 1) * page_size]
    rows = [
        [
            InlineKeyboardButton(
                text=f"{item['key']} = {str(item['value'])[:24]}",
                callback_data=f"admin:setting:{item['key']}",
            )
        ]
        for item in window
    ]
    nav = []
    if page > 0:
        nav.append(
            InlineKeyboardButton(text="◀️", callback_data=f"admin:settings:{page - 1}")
        )
    if (page + 1) * page_size < len(items):
        nav.append(
            InlineKeyboardButton(text="▶️", callback_data=f"admin:settings:{page + 1}")
        )
    if nav:
        rows.append(nav)
    rows.append(
        [InlineKeyboardButton(text="⬅️ Админ-панель", callback_data="admin:menu")]
    )
    await edit_or_answer(
        callback,
        "⚙️ <b>Настройки</b>\nВсе значения сохраняются в SQLite.",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await answer_callback(callback)


@router.callback_query(
    F.data.startswith("admin:setting:") & ~F.data.startswith("admin:settings:")
)
async def setting_card(callback: CallbackQuery, settings: SettingsService) -> None:
    key = callback.data.split(":", 2)[2]
    row = await settings.db.fetchone("SELECT * FROM settings WHERE key = ?", (key,))
    if row is None:
        await callback.answer("Настройка не найдена", show_alert=True)
        return
    rendered_value = str(row["value"])
    if key in {"global_referral_bp", "global_cashback_bp", "withdrawal_fee_bp"}:
        points = int(rendered_value)
        rendered_value = f"{points // 100}.{points % 100:02d}%"
    elif key == "min_withdrawal_cents":
        rendered_value = format_money(int(rendered_value), "USDT")
    text = (
        f"⚙️ <b>{escape(key)}</b>\n\nЗначение: <code>{escape(rendered_value)}</code>\n"
        f"Тип: {row['value_type']}\n{escape(str(row['description']))}"
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✏️ Изменить", callback_data=f"admin:settingedit:{key}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Настройки", callback_data="admin:settings:0"
                )
            ],
        ]
    )
    await edit_or_answer(callback, text, keyboard)
    await answer_callback(callback)


@router.callback_query(F.data.startswith("admin:settingedit:"))
async def setting_edit_start(callback: CallbackQuery, state: FSMContext) -> None:
    key = callback.data.split(":", 2)[2]
    await state.set_state(SettingEditForm.value)
    await state.update_data(setting_key=key)
    hint = ""
    if key in {"global_referral_bp", "global_cashback_bp", "withdrawal_fee_bp"}:
        hint = " в процентах, например 7.5"
    elif key == "min_withdrawal_cents":
        hint = " в USDT, например 5.00"
    await edit_or_answer(
        callback,
        f"Введите новое значение для <code>{escape(key)}</code>{hint}:",
        admin_back(f"admin:setting:{key}"),
    )
    await answer_callback(callback)


@router.message(SettingEditForm.value)
async def setting_edit(
    message: Message, state: FSMContext, settings: SettingsService
) -> None:
    data = await state.get_data()
    key = str(data["setting_key"])
    raw_value = message.text or ""
    try:
        if key in {"global_referral_bp", "global_cashback_bp", "withdrawal_fee_bp"}:
            from decimal import Decimal, InvalidOperation

            try:
                percent = Decimal(raw_value.replace(",", ".").strip())
            except InvalidOperation as exc:
                raise SettingError("Некорректный процент") from exc
            points = percent * 100
            if (
                not percent.is_finite()
                or not 0 <= percent <= 100
                or points != points.to_integral_value()
            ):
                raise SettingError("Процент должен быть от 0 до 100")
            raw_value = str(int(points))
        elif key == "min_withdrawal_cents":
            raw_value = str(parse_money_to_cents(raw_value))
        await settings.set(key, raw_value, int(message.from_user.id))
    except (SettingError, MoneyError) as exc:
        await message.answer(f"⚠️ {exc}")
        return
    await state.clear()
    await message.answer(
        "✅ Настройка обновлена.", reply_markup=admin_back(f"admin:setting:{key}")
    )


@router.callback_query(F.data == "admin:backups")
async def backups_menu(callback: CallbackQuery, backups: BackupService) -> None:
    items = await backups.list(10)
    lines = [
        f"#{item['id']} · {item['filename']} · {item['size_bytes']} B" for item in items
    ]
    rows = [
        [
            InlineKeyboardButton(
                text="➕ Создать backup", callback_data="admin:backup:create"
            )
        ]
    ]
    if items:
        rows.append(
            [
                InlineKeyboardButton(
                    text="📤 Получить последний",
                    callback_data=f"admin:backup:send:{items[0]['id']}",
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(text="⬅️ Админ-панель", callback_data="admin:menu")]
    )
    await edit_or_answer(
        callback,
        "🗄 <b>Резервные копии</b>\n\n" + ("\n".join(lines) or "Копий пока нет"),
        InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await answer_callback(callback)


@router.callback_query(F.data == "admin:backup:create")
async def backup_create(callback: CallbackQuery, backups: BackupService) -> None:
    await callback.answer("Создаю backup…")
    try:
        path = await backups.create(int(callback.from_user.id))
    except Exception as exc:  # noqa: BLE001 - the admin must receive any backup failure
        await callback.message.answer(
            f"⚠️ Backup не создан: {exc}", reply_markup=admin_back("admin:backups")
        )
        return
    await callback.message.answer_document(FSInputFile(path), caption=f"✅ {path.name}")


@router.callback_query(F.data.startswith("admin:backup:send:"))
async def backup_send(callback: CallbackQuery, backups: BackupService) -> None:
    backup_id = int(callback.data.rsplit(":", 1)[1])
    row = await backups.db.fetchone("SELECT * FROM backups WHERE id = ?", (backup_id,))
    if row is None:
        await callback.answer("Backup не найден", show_alert=True)
        return
    path = backups.directory / str(row["filename"])
    if not path.is_file():
        await callback.answer("Файл backup отсутствует", show_alert=True)
        return
    await callback.message.answer_document(FSInputFile(path), caption=f"🗄 {path.name}")
    await answer_callback(callback)


@router.callback_query(F.data == "admin:broadcast")
async def broadcast_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(BroadcastForm.content)
    await edit_or_answer(
        callback, "📢 Отправьте текст или фото с подписью для рассылки:", admin_back()
    )
    await answer_callback(callback)


@router.message(BroadcastForm.content)
async def broadcast_content(message: Message, state: FSMContext) -> None:
    text = message.caption if message.photo else message.text
    photo = message.photo[-1].file_id if message.photo else None
    if not text and not photo:
        await message.answer("Отправьте текст или фото.")
        return
    await state.update_data(broadcast_text=text or "", photo_file_id=photo)
    await state.set_state(BroadcastForm.buttons)
    await message.answer(
        "Добавьте кнопки, по одной на строку:\nНазвание | https://example.com\n\n"
        "Отправьте '-' без кнопок."
    )


@router.message(BroadcastForm.buttons)
async def broadcast_buttons(
    message: Message, state: FSMContext, broadcasts: BroadcastService
) -> None:
    raw = (message.text or "").strip()
    buttons: list[dict[str, str]] = []
    if raw != "-":
        for line in raw.splitlines():
            if "|" not in line:
                await message.answer("Формат кнопки: Название | https://example.com")
                return
            title, url = (part.strip() for part in line.split("|", 1))
            if not title or not url.startswith(("https://", "http://", "tg://")):
                await message.answer("Проверьте название и URL кнопки.")
                return
            buttons.append({"text": title, "url": url})
            if len(buttons) > 8:
                await message.answer("Можно добавить не более 8 кнопок.")
                return
    data = await state.get_data()
    try:
        broadcast_id = await broadcasts.create_draft(
            int(message.from_user.id),
            text=str(data["broadcast_text"]),
            photo_file_id=data.get("photo_file_id"),
            buttons=buttons,
        )
    except ValueError as exc:
        await message.answer(f"⚠️ {exc}")
        return
    await state.clear()
    preview = await broadcasts.preview(broadcast_id)
    rows = [
        [
            InlineKeyboardButton(
                text="🚀 Запустить",
                callback_data=f"admin:broadcast:launch:{broadcast_id}",
            )
        ],
        [
            InlineKeyboardButton(
                text="❌ Отмена", callback_data=f"admin:broadcast:cancel:{broadcast_id}"
            )
        ],
    ]
    text = (
        f"👁 <b>Предпросмотр рассылки #{broadcast_id}</b>\nПолучателей: {preview['recipients']}\n\n"
        f"{escape(str(data['broadcast_text']))}"
    )
    await message.answer(
        text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML"
    )


@router.callback_query(F.data.startswith("admin:broadcast:launch:"))
async def broadcast_launch(
    callback: CallbackQuery, broadcasts: BroadcastService
) -> None:
    broadcast_id = int(callback.data.rsplit(":", 1)[1])
    try:
        count = await broadcasts.launch(broadcast_id)
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await edit_or_answer(
        callback,
        f"🚀 Рассылка #{broadcast_id} запущена. Получателей: {count}",
        admin_back(),
    )
    await answer_callback(callback)


@router.callback_query(F.data.startswith("admin:broadcast:cancel:"))
async def broadcast_cancel(
    callback: CallbackQuery, broadcasts: BroadcastService
) -> None:
    broadcast_id = int(callback.data.rsplit(":", 1)[1])
    await broadcasts.cancel(broadcast_id)
    await edit_or_answer(
        callback, f"❌ Рассылка #{broadcast_id} отменена.", admin_back()
    )
    await answer_callback(callback)


@router.message(Command("admin_add"))
async def admin_add(
    message: Message, command: CommandObject, users: UserService
) -> None:
    parts = (command.args or "").split()
    if len(parts) != 2 or not parts[0].isdigit():
        await message.answer("Использование: /admin_add TELEGRAM_ID ADMIN|SUPPORT")
        return
    try:
        await users.add_admin(int(message.from_user.id), int(parts[0]), parts[1])
    except (PermissionError, ValueError) as exc:
        await message.answer(f"⚠️ {exc}")
        return
    await message.answer("✅ Администратор добавлен.")


@router.message(Command("admin_remove"))
async def admin_remove(
    message: Message, command: CommandObject, users: UserService
) -> None:
    raw = (command.args or "").strip()
    if not raw.isdigit():
        await message.answer("Использование: /admin_remove TELEGRAM_ID")
        return
    try:
        await users.deactivate_admin(int(message.from_user.id), int(raw))
    except (PermissionError, ValueError) as exc:
        await message.answer(f"⚠️ {exc}")
        return
    await message.answer("✅ Доступ администратора отключён.")
