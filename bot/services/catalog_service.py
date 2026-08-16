from __future__ import annotations

from typing import Any

from bot.database.db import Database
from bot.database.models import ProductType


class CatalogError(ValueError):
    """Raised for invalid category, product, or stock operations."""


class CatalogService:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def categories(
        self, *, admin: bool = False, page: int = 0, page_size: int = 20
    ) -> list[dict[str, Any]]:
        where = "archived = 0" if admin else "active = 1 AND archived = 0"
        rows = await self.db.fetchall(
            f"SELECT c.*, (SELECT COUNT(*) FROM products p WHERE p.category_id = c.id AND p.status = 'ACTIVE') AS product_count "
            f"FROM categories c WHERE {where} ORDER BY sort_order, id LIMIT ? OFFSET ?",
            (page_size, max(0, page) * page_size),
        )
        return [dict(row) for row in rows]

    async def add_category(self, name: str) -> int:
        clean = name.strip()
        if not 1 <= len(clean) <= 80:
            raise CatalogError("Название категории: 1–80 символов")
        return await self.db.execute(
            "INSERT INTO categories(name) VALUES (?)", (clean,)
        )

    async def update_category(
        self, category_id: int, *, name: str | None = None, active: bool | None = None
    ) -> None:
        if name is not None:
            clean = name.strip()
            if not 1 <= len(clean) <= 80:
                raise CatalogError("Название категории: 1–80 символов")
            await self.db.execute(
                "UPDATE categories SET name = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND archived = 0",
                (clean, category_id),
            )
        if active is not None:
            await self.db.execute(
                "UPDATE categories SET active = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND archived = 0",
                (int(active), category_id),
            )

    async def archive_category(self, category_id: int) -> None:
        async with self.db.transaction() as connection:
            await connection.execute(
                "UPDATE categories SET archived = 1, active = 0, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (category_id,),
            )
            await connection.execute(
                "UPDATE products SET status = 'ARCHIVED', updated_at = CURRENT_TIMESTAMP WHERE category_id = ?",
                (category_id,),
            )

    async def product(
        self, product_id: int, *, include_inactive: bool = False
    ) -> dict[str, Any] | None:
        query = (
            "SELECT p.*, c.name AS category_name FROM products p JOIN categories c ON c.id = p.category_id "
            "WHERE p.id = ?"
        )
        if not include_inactive:
            query += " AND p.status = 'ACTIVE' AND c.active = 1 AND c.archived = 0"
        row = await self.db.fetchone(query, (product_id,))
        return dict(row) if row else None

    async def products(
        self,
        category_id: int | None = None,
        *,
        admin: bool = False,
        page: int = 0,
        page_size: int = 20,
    ) -> list[dict[str, Any]]:
        clauses = (
            [] if admin else ["p.status = 'ACTIVE'", "c.active = 1", "c.archived = 0"]
        )
        params: list[Any] = []
        if category_id is not None:
            clauses.append("p.category_id = ?")
            params.append(category_id)
        where = " AND ".join(clauses) if clauses else "1 = 1"
        params.extend((page_size, max(0, page) * page_size))
        rows = await self.db.fetchall(
            "SELECT p.*, c.name AS category_name FROM products p JOIN categories c ON c.id = p.category_id "
            f"WHERE {where} ORDER BY p.id DESC LIMIT ? OFFSET ?",
            tuple(params),
        )
        return [dict(row) for row in rows]

    async def create_product(
        self,
        *,
        category_id: int,
        name: str,
        description: str,
        price_cents: int,
        stock_type: str,
        content_text: str | None = None,
        telegram_file_id: str | None = None,
    ) -> int:
        normalized_type = stock_type.upper()
        if normalized_type not in set(ProductType):
            raise CatalogError("Некорректный тип товара")
        if price_cents <= 0:
            raise CatalogError("Цена должна быть больше нуля")
        clean_name = name.strip()
        if not 1 <= len(clean_name) <= 120:
            raise CatalogError("Название товара: 1–120 символов")
        if len(description) > 3500:
            raise CatalogError("Описание не может быть длиннее 3500 символов")
        if content_text is not None and len(content_text) > 4096:
            raise CatalogError("Текст выдачи не может быть длиннее 4096 символов")
        category = await self.db.fetchone(
            "SELECT id FROM categories WHERE id = ? AND archived = 0", (category_id,)
        )
        if category is None:
            raise CatalogError("Категория не найдена")
        if normalized_type == ProductType.TEXT and not content_text:
            raise CatalogError("Для TEXT нужен текст выдачи")
        if normalized_type == ProductType.FILE and not telegram_file_id:
            raise CatalogError("Для FILE нужен Telegram file_id")
        return await self.db.execute(
            "INSERT INTO products(category_id, name, description, price_cents, stock_type, content_text, telegram_file_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                category_id,
                clean_name,
                description.strip(),
                price_cents,
                normalized_type,
                content_text,
                telegram_file_id,
            ),
        )

    async def update_product_field(
        self, product_id: int, field: str, value: Any
    ) -> None:
        allowed = {
            "name",
            "description",
            "price_cents",
            "old_price_cents",
            "category_id",
            "image_file_id",
            "content_text",
            "telegram_file_id",
            "cashback_bp",
            "referral_bp",
            "status",
        }
        if field not in allowed:
            raise CatalogError("Поле нельзя изменить")
        if field == "status" and value not in {"ACTIVE", "INACTIVE", "ARCHIVED"}:
            raise CatalogError("Некорректный статус")
        if (
            field in {"cashback_bp", "referral_bp"}
            and value is not None
            and not 0 <= int(value) <= 10_000
        ):
            raise CatalogError("Процент должен быть от 0 до 100")
        if field == "price_cents" and int(value) <= 0:
            raise CatalogError("Цена должна быть больше нуля")
        if field == "old_price_cents" and value is not None and int(value) <= 0:
            raise CatalogError("Старая цена должна быть больше нуля")
        if field == "name" and not 1 <= len(str(value).strip()) <= 120:
            raise CatalogError("Название товара: 1–120 символов")
        if field == "description" and len(str(value)) > 3500:
            raise CatalogError("Описание не может быть длиннее 3500 символов")
        if (
            field in {"image_file_id", "telegram_file_id"}
            and value is not None
            and not 1 <= len(str(value)) <= 1024
        ):
            raise CatalogError("Некорректный Telegram file_id")
        if field == "content_text" and value is not None and len(str(value)) > 4096:
            raise CatalogError("Текст выдачи не может быть длиннее 4096 символов")
        await self.db.execute(
            f"UPDATE products SET {field} = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (value, product_id),
        )

    async def add_stock(self, product_id: int, values: list[str]) -> tuple[int, int]:
        cleaned = list(
            dict.fromkeys(value.strip() for value in values if value.strip())
        )
        if not cleaned:
            raise CatalogError("Список склада пуст")
        if len(cleaned) > 50_000:
            raise CatalogError(
                "За одну операцию можно добавить не более 50 000 позиций"
            )
        if any(len(value) > 4096 for value in cleaned):
            raise CatalogError(
                "Одна позиция склада не может быть длиннее 4096 символов"
            )
        product = await self.db.fetchone(
            "SELECT stock_type FROM products WHERE id = ?", (product_id,)
        )
        if product is None or str(product["stock_type"]) != ProductType.UNIQUE:
            raise CatalogError("Stock доступен только для UNIQUE товара")
        async with self.db.transaction() as connection:
            cursor = await connection.execute(
                "SELECT COUNT(*) AS count FROM product_stock WHERE product_id = ?",
                (product_id,),
            )
            before = int((await cursor.fetchone())["count"])
            await cursor.close()
            await connection.executemany(
                "INSERT OR IGNORE INTO product_stock(product_id, value) VALUES (?, ?)",
                ((product_id, value) for value in cleaned),
            )
            cursor = await connection.execute(
                "SELECT COUNT(*) AS count FROM product_stock WHERE product_id = ?",
                (product_id,),
            )
            added = int((await cursor.fetchone())["count"]) - before
            await cursor.close()
            cursor = await connection.execute(
                "SELECT COUNT(*) AS count FROM product_stock WHERE product_id = ? AND status = 'AVAILABLE'",
                (product_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
            available = int(row["count"])
            await connection.execute(
                "UPDATE products SET stock_count = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (available, product_id),
            )
        return added, available
