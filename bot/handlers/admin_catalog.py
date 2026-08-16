from __future__ import annotations

import io
from decimal import Decimal, InvalidOperation
from html import escape

from aiogram import Bot, F, Router
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
from bot.services.catalog_service import CatalogError, CatalogService
from bot.services.settings_service import SettingsService
from bot.states.admin import CategoryForm, ProductEditForm, ProductForm
from bot.utils.money import (
    MoneyError,
    format_basis_points,
    format_money,
    parse_money_to_cents,
)

router = Router(name="admin_catalog")
router.message.outer_middleware(AdminOnlyMiddleware())
router.callback_query.outer_middleware(AdminOnlyMiddleware())


@router.callback_query(F.data.startswith("admin:categories:"))
async def categories(
    callback: CallbackQuery, catalog: CatalogService, settings: SettingsService
) -> None:
    page = max(0, int(callback.data.rsplit(":", 1)[1]))
    page_size = await settings.get_int("page_size", 8)
    items = await catalog.categories(admin=True, page=page, page_size=page_size + 1)
    rows = [
        [
            InlineKeyboardButton(
                text=f"{'🟢' if item['active'] else '⚫️'} {item['name']}",
                callback_data=f"admin:category:{item['id']}",
            )
        ]
        for item in items[:page_size]
    ]
    rows.append(
        [InlineKeyboardButton(text="➕ Добавить", callback_data="admin:category:add")]
    )
    nav = []
    if page:
        nav.append(
            InlineKeyboardButton(text="◀️", callback_data=f"admin:categories:{page - 1}")
        )
    if len(items) > page_size:
        nav.append(
            InlineKeyboardButton(text="▶️", callback_data=f"admin:categories:{page + 1}")
        )
    if nav:
        rows.append(nav)
    rows.append(
        [InlineKeyboardButton(text="⬅️ Админ-панель", callback_data="admin:menu")]
    )
    await edit_or_answer(
        callback, "📂 <b>Категории</b>", InlineKeyboardMarkup(inline_keyboard=rows)
    )
    await answer_callback(callback)


@router.callback_query(F.data == "admin:category:add")
async def category_add(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(CategoryForm.name)
    await edit_or_answer(
        callback, "Введите название новой категории:", admin_back("admin:categories:0")
    )
    await answer_callback(callback)


@router.message(CategoryForm.name)
async def category_name(
    message: Message, state: FSMContext, catalog: CatalogService
) -> None:
    try:
        category_id = await catalog.add_category(message.text or "")
    except CatalogError as exc:
        await message.answer(f"⚠️ {exc}")
        return
    await state.clear()
    await message.answer(
        f"✅ Категория #{category_id} создана.",
        reply_markup=admin_back("admin:categories:0"),
    )


@router.callback_query(F.data.regexp(r"^admin:category:\d+$"))
async def category_card(callback: CallbackQuery, catalog: CatalogService) -> None:
    category_id = int(callback.data.rsplit(":", 1)[1])
    row = await catalog.db.fetchone(
        "SELECT * FROM categories WHERE id = ?", (category_id,)
    )
    if row is None:
        await callback.answer("Категория не найдена", show_alert=True)
        return
    rows = [
        [
            InlineKeyboardButton(
                text="✏️ Переименовать",
                callback_data=f"admin:category:rename:{category_id}",
            )
        ],
        [
            InlineKeyboardButton(
                text="🔁 Вкл/выкл", callback_data=f"admin:category:toggle:{category_id}"
            )
        ],
        [
            InlineKeyboardButton(
                text="🗑 Архивировать",
                callback_data=f"admin:category:archive:{category_id}",
            )
        ],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:categories:0")],
    ]
    await edit_or_answer(
        callback,
        f"📂 <b>{escape(str(row['name']))}</b>\nID: {category_id}\nАктивна: {'да' if row['active'] else 'нет'}",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await answer_callback(callback)


@router.callback_query(F.data.startswith("admin:category:rename:"))
async def category_rename_start(callback: CallbackQuery, state: FSMContext) -> None:
    category_id = int(callback.data.rsplit(":", 1)[1])
    await state.set_state(CategoryForm.rename)
    await state.update_data(category_id=category_id)
    await edit_or_answer(
        callback, "Введите новое название:", admin_back(f"admin:category:{category_id}")
    )
    await answer_callback(callback)


@router.message(CategoryForm.rename)
async def category_rename(
    message: Message, state: FSMContext, catalog: CatalogService
) -> None:
    data = await state.get_data()
    try:
        await catalog.update_category(int(data["category_id"]), name=message.text or "")
    except CatalogError as exc:
        await message.answer(f"⚠️ {exc}")
        return
    category_id = int(data["category_id"])
    await state.clear()
    await message.answer(
        "✅ Название изменено.",
        reply_markup=admin_back(f"admin:category:{category_id}"),
    )


@router.callback_query(F.data.startswith("admin:category:toggle:"))
async def category_toggle(callback: CallbackQuery, catalog: CatalogService) -> None:
    category_id = int(callback.data.rsplit(":", 1)[1])
    row = await catalog.db.fetchone(
        "SELECT active FROM categories WHERE id = ?", (category_id,)
    )
    if row:
        await catalog.update_category(category_id, active=not bool(row["active"]))
    await callback.answer("Готово")
    copied = callback.model_copy(update={"data": f"admin:category:{category_id}"})
    await category_card(copied, catalog)


@router.callback_query(F.data.startswith("admin:category:archive:"))
async def category_archive(callback: CallbackQuery, catalog: CatalogService) -> None:
    category_id = int(callback.data.rsplit(":", 1)[1])
    await catalog.archive_category(category_id)
    await callback.answer("Категория и её товары архивированы", show_alert=True)
    copied = callback.model_copy(update={"data": "admin:categories:0"})
    await categories(copied, catalog, SettingsService(catalog.db))


@router.callback_query(F.data.startswith("admin:products:"))
async def products(
    callback: CallbackQuery, catalog: CatalogService, settings: SettingsService
) -> None:
    page = max(0, int(callback.data.rsplit(":", 1)[1]))
    page_size = await settings.get_int("page_size", 8)
    items = await catalog.products(admin=True, page=page, page_size=page_size + 1)
    rows = [
        [
            InlineKeyboardButton(
                text=f"{'🟢' if item['status'] == 'ACTIVE' else '⚫️'} #{item['id']} {item['name']}",
                callback_data=f"admin:product:{item['id']}",
            )
        ]
        for item in items[:page_size]
    ]
    rows.append(
        [
            InlineKeyboardButton(
                text="➕ Создать товар", callback_data="admin:product:create"
            )
        ]
    )
    nav = []
    if page:
        nav.append(
            InlineKeyboardButton(text="◀️", callback_data=f"admin:products:{page - 1}")
        )
    if len(items) > page_size:
        nav.append(
            InlineKeyboardButton(text="▶️", callback_data=f"admin:products:{page + 1}")
        )
    if nav:
        rows.append(nav)
    rows.append(
        [InlineKeyboardButton(text="⬅️ Админ-панель", callback_data="admin:menu")]
    )
    await edit_or_answer(
        callback, "🛍 <b>Товары</b>", InlineKeyboardMarkup(inline_keyboard=rows)
    )
    await answer_callback(callback)


@router.callback_query(F.data == "admin:product:create")
async def product_create(
    callback: CallbackQuery, state: FSMContext, catalog: CatalogService
) -> None:
    categories_list = await catalog.categories(admin=True, page_size=50)
    if not categories_list:
        await callback.answer("Сначала создайте категорию", show_alert=True)
        return
    await state.set_state(ProductForm.category)
    rows = [
        [
            InlineKeyboardButton(
                text=item["name"],
                callback_data=f"admin:newproduct:category:{item['id']}",
            )
        ]
        for item in categories_list
    ]
    rows.append(
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:products:0")]
    )
    await edit_or_answer(
        callback,
        "Выберите категорию товара:",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await answer_callback(callback)


@router.callback_query(
    ProductForm.category, F.data.startswith("admin:newproduct:category:")
)
async def product_category(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(category_id=int(callback.data.rsplit(":", 1)[1]))
    await state.set_state(ProductForm.name)
    await edit_or_answer(
        callback, "Введите название товара:", admin_back("admin:products:0")
    )
    await answer_callback(callback)


@router.message(ProductForm.name)
async def product_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not 1 <= len(name) <= 120:
        await message.answer("Название должно содержать 1–120 символов.")
        return
    await state.update_data(name=name)
    await state.set_state(ProductForm.description)
    await message.answer("Введите описание товара (или один дефис для пустого):")


@router.message(ProductForm.description)
async def product_description(message: Message, state: FSMContext) -> None:
    description = (message.text or "").strip()
    await state.update_data(description="" if description == "-" else description)
    await state.set_state(ProductForm.price)
    await message.answer("Введите цену в USDT, например 10.50:")


@router.message(ProductForm.price)
async def product_price(message: Message, state: FSMContext) -> None:
    try:
        price_cents = parse_money_to_cents(message.text or "")
    except MoneyError as exc:
        await message.answer(f"⚠️ {exc}")
        return
    await state.update_data(price_cents=price_cents)
    await state.set_state(ProductForm.kind)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📝 TEXT", callback_data="admin:newproduct:type:TEXT"
                ),
                InlineKeyboardButton(
                    text="📎 FILE", callback_data="admin:newproduct:type:FILE"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🔑 UNIQUE", callback_data="admin:newproduct:type:UNIQUE"
                ),
                InlineKeyboardButton(
                    text="👤 MANUAL", callback_data="admin:newproduct:type:MANUAL"
                ),
            ],
        ]
    )
    await message.answer("Выберите тип выдачи:", reply_markup=keyboard)


@router.callback_query(ProductForm.kind, F.data.startswith("admin:newproduct:type:"))
async def product_kind(
    callback: CallbackQuery, state: FSMContext, catalog: CatalogService
) -> None:
    kind = callback.data.rsplit(":", 1)[1]
    await state.update_data(stock_type=kind)
    if kind == "MANUAL":
        await _finish_product(callback.message, state, catalog, None, None)
        await answer_callback(callback)
        return
    await state.set_state(ProductForm.content)
    prompts = {
        "TEXT": "Отправьте текст, который получит покупатель:",
        "FILE": "Отправьте файл, который получит покупатель:",
        "UNIQUE": "Отправьте уникальные позиции строками или .txt-файлом:",
    }
    await edit_or_answer(callback, prompts[kind], admin_back("admin:products:0"))
    await answer_callback(callback)


@router.message(ProductForm.content)
async def product_content(
    message: Message, state: FSMContext, catalog: CatalogService, bot: Bot
) -> None:
    data = await state.get_data()
    kind = str(data["stock_type"])
    if kind == "FILE":
        if not message.document:
            await message.answer("Отправьте документ.")
            return
        await _finish_product(message, state, catalog, None, message.document.file_id)
        return
    if kind == "TEXT":
        if not message.text:
            await message.answer("Отправьте текст.")
            return
        await _finish_product(message, state, catalog, message.text, None)
        return
    try:
        values = await _stock_values(message, bot)
    except CatalogError as exc:
        await message.answer(f"⚠️ {exc}")
        return
    if not values:
        await message.answer("Не удалось прочитать позиции.")
        return
    product_id = await _finish_product(
        message, state, catalog, None, None, announce=False
    )
    added, available = await catalog.add_stock(product_id, values)
    await message.answer(
        f"✅ Товар #{product_id} создан. Добавлено: {added}, доступно: {available}.",
        reply_markup=admin_back("admin:products:0"),
    )


async def _finish_product(
    message: Message,
    state: FSMContext,
    catalog: CatalogService,
    content_text: str | None,
    file_id: str | None,
    *,
    announce: bool = True,
) -> int:
    data = await state.get_data()
    product_id = await catalog.create_product(
        category_id=int(data["category_id"]),
        name=str(data["name"]),
        description=str(data["description"]),
        price_cents=int(data["price_cents"]),
        stock_type=str(data["stock_type"]),
        content_text=content_text,
        telegram_file_id=file_id,
    )
    await state.clear()
    if announce:
        await message.answer(
            f"✅ Товар #{product_id} создан.",
            reply_markup=admin_back("admin:products:0"),
        )
    return product_id


async def _stock_values(message: Message, bot: Bot) -> list[str]:
    if message.document:
        if (
            message.document.file_name
            and not message.document.file_name.lower().endswith(".txt")
        ):
            raise CatalogError("Для массового stock нужен .txt-файл")
        if message.document.file_size and message.document.file_size > 2_000_000:
            raise CatalogError("Файл больше 2 МБ")
        buffer = io.BytesIO()
        await bot.download(message.document, destination=buffer)
        try:
            text = buffer.getvalue().decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise CatalogError("Требуется UTF-8 .txt") from exc
    else:
        text = message.text or ""
    return [line.strip() for line in text.splitlines() if line.strip()]


@router.callback_query(
    F.data.startswith("admin:product:") & ~F.data.endswith(":create")
)
async def product_card(callback: CallbackQuery, catalog: CatalogService) -> None:
    product_id = int(callback.data.rsplit(":", 1)[1])
    item = await catalog.product(product_id, include_inactive=True)
    if item is None:
        await callback.answer("Товар не найден", show_alert=True)
        return
    rows = [
        [
            InlineKeyboardButton(
                text="✏️ Название", callback_data=f"admin:productedit:{product_id}:name"
            ),
            InlineKeyboardButton(
                text="📝 Описание",
                callback_data=f"admin:productedit:{product_id}:description",
            ),
        ],
        [
            InlineKeyboardButton(
                text="💵 Цена",
                callback_data=f"admin:productedit:{product_id}:price_cents",
            ),
            InlineKeyboardButton(
                text="🏷 Старая цена",
                callback_data=f"admin:productedit:{product_id}:old_price_cents",
            ),
        ],
        [
            InlineKeyboardButton(
                text="🎁 Cashback",
                callback_data=f"admin:productedit:{product_id}:cashback_bp",
            ),
            InlineKeyboardButton(
                text="🤝 Referral",
                callback_data=f"admin:productedit:{product_id}:referral_bp",
            ),
        ],
        [
            InlineKeyboardButton(
                text="📂 Категория",
                callback_data=f"admin:productedit:{product_id}:category_id",
            ),
            InlineKeyboardButton(
                text="🖼 Изображение", callback_data=f"admin:productimage:{product_id}"
            ),
        ],
        [
            InlineKeyboardButton(
                text="📦 Содержимое/file_id",
                callback_data=f"admin:productedit:{product_id}:content",
            ),
            InlineKeyboardButton(
                text="➕ Stock", callback_data=f"admin:productstock:{product_id}"
            ),
        ],
        [
            InlineKeyboardButton(
                text="🔁 Вкл/выкл", callback_data=f"admin:producttoggle:{product_id}"
            ),
            InlineKeyboardButton(
                text="🗑 Архив", callback_data=f"admin:productarchive:{product_id}"
            ),
        ],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:products:0")],
    ]
    text = (
        f"🛍 <b>#{product_id} {escape(str(item['name']))}</b>\nКатегория: {escape(str(item['category_name']))}\n"
        f"Цена: {format_money(int(item['price_cents']), 'USDT')}\nТип: {item['stock_type']}\n"
        f"Остаток: {item['stock_count']}\nСтатус: {item['status']}\n"
        f"Cashback: {'глобальный' if item['cashback_bp'] is None else format_basis_points(int(item['cashback_bp']))}\n"
        f"Referral: {'глобальный' if item['referral_bp'] is None else format_basis_points(int(item['referral_bp']))}"
    )
    await edit_or_answer(callback, text, InlineKeyboardMarkup(inline_keyboard=rows))
    await answer_callback(callback)


@router.callback_query(F.data.startswith("admin:productedit:"))
async def product_edit_start(
    callback: CallbackQuery, state: FSMContext, catalog: CatalogService
) -> None:
    _, _, product_raw, field = callback.data.split(":")
    product_id = int(product_raw)
    item = await catalog.product(product_id, include_inactive=True)
    if item is None:
        await callback.answer("Товар не найден", show_alert=True)
        return
    if field == "category_id":
        categories_list = await catalog.categories(admin=True, page_size=50)
        rows = [
            [
                InlineKeyboardButton(
                    text=str(category["name"]),
                    callback_data=f"admin:productcategory:{product_id}:{category['id']}",
                )
            ]
            for category in categories_list
        ]
        rows.append(
            [
                InlineKeyboardButton(
                    text="⬅️ Назад", callback_data=f"admin:product:{product_id}"
                )
            ]
        )
        await edit_or_answer(
            callback,
            "Выберите новую категорию:",
            InlineKeyboardMarkup(inline_keyboard=rows),
        )
        await answer_callback(callback)
        return
    await state.set_state(ProductEditForm.value)
    await state.update_data(
        product_id=product_id, field=field, stock_type=item["stock_type"]
    )
    hints = {
        "price_cents": "Введите новую цену в USDT:",
        "old_price_cents": "Введите старую цену в USDT или 0, чтобы убрать:",
        "cashback_bp": "Введите процент (например 5) или '-' для глобального:",
        "referral_bp": "Введите процент (например 10) или '-' для глобального:",
        "content": "Отправьте новый текст или файл (в зависимости от типа товара):",
    }
    await edit_or_answer(
        callback,
        hints.get(field, f"Введите новое значение для {field}:"),
        admin_back(f"admin:product:{product_id}"),
    )
    await answer_callback(callback)


@router.callback_query(F.data.startswith("admin:productcategory:"))
async def product_category_update(
    callback: CallbackQuery, catalog: CatalogService
) -> None:
    _, _, product_raw, category_raw = callback.data.split(":")
    product_id, category_id = int(product_raw), int(category_raw)
    category = await catalog.db.fetchone(
        "SELECT id FROM categories WHERE id = ? AND archived = 0", (category_id,)
    )
    if category is None:
        await callback.answer("Категория не найдена", show_alert=True)
        return
    await catalog.update_product_field(product_id, "category_id", category_id)
    await callback.answer("Категория изменена")
    copied = callback.model_copy(update={"data": f"admin:product:{product_id}"})
    await product_card(copied, catalog)


@router.message(ProductEditForm.value)
async def product_edit_value(
    message: Message, state: FSMContext, catalog: CatalogService
) -> None:
    data = await state.get_data()
    product_id, field = int(data["product_id"]), str(data["field"])
    try:
        if field in {"price_cents", "old_price_cents"}:
            if field == "old_price_cents" and (message.text or "").strip() in {
                "0",
                "-",
            }:
                value = None
            else:
                value = parse_money_to_cents(message.text or "")
            await catalog.update_product_field(product_id, field, value)
        elif field in {"cashback_bp", "referral_bp"}:
            raw = (message.text or "").strip()
            if raw == "-":
                value = None
            else:
                try:
                    percent = Decimal(raw.replace(",", "."))
                except InvalidOperation as exc:
                    raise CatalogError("Некорректный процент") from exc
                points = percent * 100
                if (
                    not percent.is_finite()
                    or not 0 <= percent <= 100
                    or points != points.to_integral_value()
                ):
                    raise CatalogError("Процент должен быть от 0 до 100")
                value = int(points)
            await catalog.update_product_field(product_id, field, value)
        elif field == "category_id":
            category_id = int((message.text or "").strip())
            category = await catalog.db.fetchone(
                "SELECT id FROM categories WHERE id = ? AND archived = 0",
                (category_id,),
            )
            if category is None:
                raise CatalogError("Категория не найдена")
            await catalog.update_product_field(product_id, field, category_id)
        elif field == "content":
            if data["stock_type"] == "FILE":
                if not message.document:
                    raise CatalogError("Отправьте документ")
                await catalog.update_product_field(
                    product_id, "telegram_file_id", message.document.file_id
                )
            else:
                if not message.text:
                    raise CatalogError("Отправьте текст")
                await catalog.update_product_field(
                    product_id, "content_text", message.text
                )
        else:
            await catalog.update_product_field(product_id, field, message.text or "")
    except (CatalogError, MoneyError, ValueError) as exc:
        await message.answer(f"⚠️ {exc}")
        return
    await state.clear()
    await message.answer(
        "✅ Товар обновлён.", reply_markup=admin_back(f"admin:product:{product_id}")
    )


@router.callback_query(F.data.startswith("admin:productimage:"))
async def product_image_start(callback: CallbackQuery, state: FSMContext) -> None:
    product_id = int(callback.data.rsplit(":", 1)[1])
    await state.set_state(ProductEditForm.image)
    await state.update_data(product_id=product_id)
    await edit_or_answer(
        callback,
        "Отправьте изображение или '-' для удаления:",
        admin_back(f"admin:product:{product_id}"),
    )
    await answer_callback(callback)


@router.message(ProductEditForm.image)
async def product_image(
    message: Message, state: FSMContext, catalog: CatalogService
) -> None:
    data = await state.get_data()
    product_id = int(data["product_id"])
    if message.text == "-":
        file_id = None
    elif message.photo:
        file_id = message.photo[-1].file_id
    else:
        await message.answer("Отправьте изображение или '-'.")
        return
    await catalog.update_product_field(product_id, "image_file_id", file_id)
    await state.clear()
    await message.answer(
        "✅ Изображение обновлено.",
        reply_markup=admin_back(f"admin:product:{product_id}"),
    )


@router.callback_query(F.data.startswith("admin:productstock:"))
async def product_stock_start(callback: CallbackQuery, state: FSMContext) -> None:
    product_id = int(callback.data.rsplit(":", 1)[1])
    await state.set_state(ProductEditForm.stock)
    await state.update_data(product_id=product_id)
    await edit_or_answer(
        callback,
        "Отправьте позиции строками или UTF-8 .txt (до 2 МБ):",
        admin_back(f"admin:product:{product_id}"),
    )
    await answer_callback(callback)


@router.message(ProductEditForm.stock)
async def product_stock(
    message: Message, state: FSMContext, catalog: CatalogService, bot: Bot
) -> None:
    data = await state.get_data()
    try:
        values = await _stock_values(message, bot)
        added, available = await catalog.add_stock(int(data["product_id"]), values)
    except CatalogError as exc:
        await message.answer(f"⚠️ {exc}")
        return
    product_id = int(data["product_id"])
    await state.clear()
    await message.answer(
        f"✅ Добавлено: {added}. Доступно: {available}.",
        reply_markup=admin_back(f"admin:product:{product_id}"),
    )


@router.callback_query(F.data.startswith("admin:producttoggle:"))
async def product_toggle(callback: CallbackQuery, catalog: CatalogService) -> None:
    product_id = int(callback.data.rsplit(":", 1)[1])
    item = await catalog.product(product_id, include_inactive=True)
    if item:
        await catalog.update_product_field(
            product_id, "status", "INACTIVE" if item["status"] == "ACTIVE" else "ACTIVE"
        )
    await callback.answer("Готово")
    copied = callback.model_copy(update={"data": f"admin:product:{product_id}"})
    await product_card(copied, catalog)


@router.callback_query(F.data.startswith("admin:productarchive:"))
async def product_archive(callback: CallbackQuery, catalog: CatalogService) -> None:
    product_id = int(callback.data.rsplit(":", 1)[1])
    await catalog.update_product_field(product_id, "status", "ARCHIVED")
    await callback.answer("Товар архивирован", show_alert=True)
    copied = callback.model_copy(update={"data": "admin:products:0"})
    await products(copied, catalog, SettingsService(catalog.db))
