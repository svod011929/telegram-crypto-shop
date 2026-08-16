from __future__ import annotations

import aiosqlite

SCHEMA_V1 = r"""
CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL UNIQUE,
    username TEXT,
    first_name TEXT NOT NULL DEFAULT '',
    language TEXT NOT NULL DEFAULT 'ru',
    registration_date TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    referrer_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    balance_cents INTEGER NOT NULL DEFAULT 0 CHECK(balance_cents >= 0),
    withdrawable_balance_cents INTEGER NOT NULL DEFAULT 0 CHECK(withdrawable_balance_cents >= 0 AND withdrawable_balance_cents <= balance_cents),
    total_deposited_cents INTEGER NOT NULL DEFAULT 0 CHECK(total_deposited_cents >= 0),
    total_spent_cents INTEGER NOT NULL DEFAULT 0 CHECK(total_spent_cents >= 0),
    total_referral_earned_cents INTEGER NOT NULL DEFAULT 0 CHECK(total_referral_earned_cents >= 0),
    total_cashback_cents INTEGER NOT NULL DEFAULT 0 CHECK(total_cashback_cents >= 0),
    total_withdrawn_cents INTEGER NOT NULL DEFAULT 0 CHECK(total_withdrawn_cents >= 0),
    number_of_referrals INTEGER NOT NULL DEFAULT 0 CHECK(number_of_referrals >= 0),
    banned INTEGER NOT NULL DEFAULT 0 CHECK(banned IN (0, 1)),
    bot_blocked INTEGER NOT NULL DEFAULT 0 CHECK(bot_blocked IN (0, 1)),
    last_activity TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE admins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL UNIQUE,
    role TEXT NOT NULL CHECK(role IN ('SUPERADMIN', 'ADMIN', 'SUPPORT')),
    permissions_json TEXT NOT NULL DEFAULT '{}',
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
    created_by INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
    archived INTEGER NOT NULL DEFAULT 0 CHECK(archived IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category_id INTEGER NOT NULL REFERENCES categories(id),
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    price_cents INTEGER NOT NULL CHECK(price_cents > 0),
    old_price_cents INTEGER CHECK(old_price_cents IS NULL OR old_price_cents > 0),
    image_file_id TEXT,
    status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK(status IN ('ACTIVE', 'INACTIVE', 'ARCHIVED')),
    stock_type TEXT NOT NULL CHECK(stock_type IN ('TEXT', 'FILE', 'UNIQUE', 'MANUAL')),
    stock_count INTEGER NOT NULL DEFAULT 0 CHECK(stock_count >= 0),
    content_text TEXT,
    telegram_file_id TEXT,
    referral_bp INTEGER CHECK(referral_bp IS NULL OR referral_bp BETWEEN 0 AND 10000),
    cashback_bp INTEGER CHECK(cashback_bp IS NULL OR cashback_bp BETWEEN 0 AND 10000),
    purchases_count INTEGER NOT NULL DEFAULT 0 CHECK(purchases_count >= 0),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE product_stock (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    value TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'AVAILABLE' CHECK(status IN ('AVAILABLE', 'RESERVED', 'SOLD')),
    order_id INTEGER UNIQUE REFERENCES orders(id),
    reserved_at TEXT,
    sold_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(product_id, value)
);

CREATE TABLE orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    product_id INTEGER REFERENCES products(id) ON DELETE SET NULL,
    product_name TEXT NOT NULL,
    amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
    currency TEXT NOT NULL DEFAULT 'USDT',
    payment_method TEXT NOT NULL CHECK(payment_method IN ('CRYPTO', 'BALANCE')),
    status TEXT NOT NULL,
    request_key TEXT NOT NULL UNIQUE,
    stock_type TEXT NOT NULL,
    delivery_text TEXT,
    delivery_file_id TEXT,
    stock_item_id INTEGER UNIQUE REFERENCES product_stock(id),
    referral_bp INTEGER NOT NULL DEFAULT 0 CHECK(referral_bp BETWEEN 0 AND 10000),
    cashback_bp INTEGER NOT NULL DEFAULT 0 CHECK(cashback_bp BETWEEN 0 AND 10000),
    financial_processed_at TEXT,
    delivered_at TEXT,
    delivery_status TEXT NOT NULL DEFAULT 'PENDING' CHECK(delivery_status IN ('PENDING', 'SENDING', 'UNKNOWN', 'SENT', 'MANUAL', 'FAILED')),
    delivery_attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    paid_at TEXT,
    completed_at TEXT,
    expires_at TEXT,
    refunded_at TEXT
);

CREATE TABLE payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL UNIQUE REFERENCES orders(id) ON DELETE CASCADE,
    invoice_id INTEGER NOT NULL UNIQUE,
    invoice_url TEXT NOT NULL,
    amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
    currency TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('ACTIVE', 'PAID', 'EXPIRED', 'DELETED', 'FAILED')),
    expiration_date TEXT,
    paid_at TEXT,
    raw_response_json TEXT,
    last_checked_at TEXT,
    check_attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE balance_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_id TEXT NOT NULL UNIQUE,
    idempotency_key TEXT NOT NULL UNIQUE,
    user_id INTEGER NOT NULL REFERENCES users(id),
    type TEXT NOT NULL,
    amount_cents INTEGER NOT NULL CHECK(amount_cents != 0),
    balance_before_cents INTEGER NOT NULL CHECK(balance_before_cents >= 0),
    balance_after_cents INTEGER NOT NULL CHECK(balance_after_cents >= 0),
    withdrawable_before_cents INTEGER NOT NULL CHECK(withdrawable_before_cents >= 0),
    withdrawable_after_cents INTEGER NOT NULL CHECK(withdrawable_after_cents >= 0),
    related_order_id INTEGER REFERENCES orders(id),
    related_withdrawal_id INTEGER REFERENCES withdrawals(id),
    description TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE withdrawals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
    fee_cents INTEGER NOT NULL DEFAULT 0 CHECK(fee_cents >= 0),
    payout_cents INTEGER NOT NULL CHECK(payout_cents > 0),
    payout_amount TEXT NOT NULL,
    asset TEXT NOT NULL DEFAULT 'USDT',
    mode TEXT NOT NULL CHECK(mode IN ('AUTO', 'MANUAL')),
    status TEXT NOT NULL,
    spend_id TEXT NOT NULL UNIQUE,
    transfer_id INTEGER UNIQUE,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT,
    refunded_at TEXT
);

CREATE TABLE referral_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL UNIQUE REFERENCES orders(id),
    buyer_user_id INTEGER NOT NULL REFERENCES users(id),
    referrer_user_id INTEGER NOT NULL REFERENCES users(id),
    percent_bp INTEGER NOT NULL,
    amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    reversed_at TEXT
);

CREATE TABLE cashback_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL UNIQUE REFERENCES orders(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    percent_bp INTEGER NOT NULL,
    amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    reversed_at TEXT
);

CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    value_type TEXT NOT NULL DEFAULT 'str',
    description TEXT NOT NULL DEFAULT '',
    updated_by INTEGER,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE backups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT NOT NULL UNIQUE,
    size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
    created_by INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING', 'SENDING', 'SENT', 'FAILED')),
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_error TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    sent_at TEXT
);

CREATE TABLE broadcasts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_by INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'DRAFT' CHECK(status IN ('DRAFT', 'RUNNING', 'COMPLETED', 'CANCELLED')),
    total_count INTEGER NOT NULL DEFAULT 0,
    sent_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at TEXT,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_users_referrer ON users(referrer_id);
CREATE INDEX idx_users_username ON users(username COLLATE NOCASE);
CREATE INDEX idx_orders_user ON orders(user_id, id DESC);
CREATE INDEX idx_orders_status ON orders(status, id);
CREATE INDEX idx_payments_status ON payments(status, id);
CREATE INDEX idx_withdrawals_user ON withdrawals(user_id, id DESC);
CREATE INDEX idx_withdrawals_status ON withdrawals(status, id);
CREATE INDEX idx_balance_user ON balance_transactions(user_id, id DESC);
CREATE INDEX idx_stock_product_status ON product_stock(product_id, status, id);
CREATE INDEX idx_products_category ON products(category_id, status, id);
CREATE INDEX idx_outbox_status ON outbox(status, available_at, id);

INSERT INTO settings(key, value, value_type, description) VALUES
('shop_name', 'Crypto Shop', 'str', 'Название магазина'),
('support_username', '', 'str', 'Username поддержки'),
('global_referral_bp', '1000', 'int', 'Глобальная партнёрская ставка, basis points'),
('global_cashback_bp', '500', 'int', 'Глобальный cashback, basis points'),
('min_withdrawal_cents', '100', 'int', 'Минимальный вывод'),
('withdrawal_fee_bp', '0', 'int', 'Комиссия вывода, basis points'),
('withdrawal_mode', 'MANUAL', 'str', 'AUTO, MANUAL или DISABLED'),
('payout_asset', 'USDT', 'str', 'Актив выплат'),
('invoice_asset', 'USDT', 'str', 'Актив счетов'),
('invoice_expiry_sec', '3600', 'int', 'Срок жизни счёта'),
('payment_poll_interval', '5', 'int', 'Интервал проверки платежей'),
('crypto_payment_enabled', 'true', 'bool', 'Принимать оплату через Crypto Pay'),
('balance_payment_enabled', 'true', 'bool', 'Разрешить оплату с внутреннего баланса'),
('balance_rewards_enabled', 'false', 'bool', 'Начислять награды за покупки с баланса'),
('cashback_withdrawable', 'true', 'bool', 'Разрешить вывод cashback'),
('maintenance_mode', 'false', 'bool', 'Технические работы'),
('notify_admin_sales', 'true', 'bool', 'Уведомления администраторам'),
('notify_referral_rewards', 'true', 'bool', 'Уведомления реферерам'),
('backup_interval_hours', '24', 'int', 'Интервал автоматических backup'),
('backup_retention', '10', 'int', 'Количество сохраняемых backup'),
('page_size', '8', 'int', 'Размер страницы'),
('broadcast_rate_per_second', '20', 'int', 'Скорость рассылки'),
('welcome_text', 'Добро пожаловать в магазин!', 'str', 'Приветственный текст'),
('maintenance_text', '🔧 Ведутся технические работы. Попробуйте позже.', 'str', 'Текст техработ');
"""


async def apply_migrations(connection: aiosqlite.Connection) -> None:
    await connection.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    await connection.commit()
    cursor = await connection.execute("SELECT version FROM schema_migrations")
    applied = {int(row[0]) for row in await cursor.fetchall()}
    await cursor.close()
    if 1 not in applied:
        script = (
            "BEGIN IMMEDIATE;\n"
            + SCHEMA_V1
            + "\nINSERT OR IGNORE INTO schema_migrations(version) VALUES (1);\nCOMMIT;"
        )
        await connection.executescript(script)
