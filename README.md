<div align="center">

# 🛒 Telegram Crypto Shop

**Production-oriented Telegram-магазин цифровых товаров с Crypto Pay, внутренним балансом, реферальной системой и полноценной админ-панелью.**

[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![aiogram](https://img.shields.io/badge/aiogram-3.30-2CA5E0?logo=telegram&logoColor=white)](https://docs.aiogram.dev/)
[![SQLite](https://img.shields.io/badge/SQLite-WAL-003B57?logo=sqlite&logoColor=white)](https://www.sqlite.org/)
[![Tests](../../actions/workflows/tests.yml/badge.svg)](../../actions/workflows/tests.yml)
[![Version](https://img.shields.io/badge/version-0.1.0-6f42c1)](#)

**Long polling · SQLite · Crypto Pay API · Pterodactyl-friendly · без webhook и внешней БД**

</div>

---

## ✨ Что умеет проект

### 🛍 Магазин и каталог
- Категории и товары типов `TEXT`, `FILE`, `UNIQUE` и `MANUAL`.
- Изображения, описания, цены, статусы и массовое управление UNIQUE-остатками.
- История покупок и повторное открытие ранее выданного товара.
- Резервирование UNIQUE-позиций внутри SQLite-транзакции.

### 💳 Оплата и финансы
- Crypto Pay invoices через единый `PaymentChecker`.
- Покупки с внутреннего баланса.
- Независимое включение/отключение Crypto Pay и balance-payment.
- Все денежные значения хранятся целыми центами — без `float`.
- Идемпотентные cashback и referral rewards через ledger.
- Защита от повторной обработки платежей и двойной выдачи.

### 💸 Выплаты
- Режимы `AUTO`, `MANUAL` и `DISABLED`.
- Выплаты в USDT через Crypto Pay Transfers.
- Постоянный `spend_id` и reconciliation через `getTransfers`.
- Защита от повторной выплаты после timeout/неопределённого API-ответа.

### 🧑‍💻 Telegram-админка
- Каталог и склад.
- Пользователи и роли.
- Балансы, заказы, платежи и выплаты.
- Настройки магазина и диагностика Crypto Pay.
- Статистика, рассылки и SQLite backup.

### 🛡 Надёжность
- SQLite в режиме WAL.
- Автоматические миграции.
- Rotating logs.
- Регулярные backup-копии с `PRAGMA integrity_check`.
- Восстановление фоновых задач после рестарта.
- Маскирование токенов в логах.

---

## 🧱 Стек

| Компонент | Технология |
|---|---|
| Telegram framework | `aiogram 3.30.0` |
| Database | `SQLite + aiosqlite` |
| Crypto payments | `Crypto Pay API` |
| HTTP client | `aiohttp` |
| Config | `python-dotenv` |
| Runtime | `Python 3.11+` |
| Deployment | Long polling / Pterodactyl |

---

## 🚀 Быстрый запуск

### 1. Клонирование и зависимости

```bash
git clone https://github.com/svod011929/telegram-crypto-shop.git
cd telegram-crypto-shop
python -m venv .venv
```

Linux / macOS:

```bash
source .venv/bin/activate
pip install -r requirements.txt
```

Windows:

```powershell
.venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Конфигурация

```bash
cp .env.example .env
```

Заполни обязательные значения:

```dotenv
BOT_TOKEN=token_from_BotFather
CRYPTO_PAY_TOKEN=token_from_CryptoBot
CRYPTO_PAY_NETWORK=mainnet
SUPERADMIN_ID=123456789
```

Для тестовой сети используй токен из `@CryptoTestnetBot`:

```dotenv
CRYPTO_PAY_NETWORK=testnet
```

> [!IMPORTANT]
> Файл `.env` содержит секреты и уже исключён через `.gitignore`. Никогда не коммить реальные токены в репозиторий.

### 3. Проверка

Проверка импортов, роутеров, миграций и SQLite без сетевых запросов:

```bash
python main.py --check
```

Полный набор тестов:

```bash
python -m unittest discover -s tests -v
```

### 4. Запуск

```bash
python main.py
```

---

## 🦖 Развёртывание в Pterodactyl

1. Загрузи содержимое проекта в корень сервера — `main.py` должен находиться в рабочем каталоге.
2. Используй Python 3.11+ egg/image.
3. Установи зависимости: `pip install -r requirements.txt`.
4. Скопируй `.env.example` в `.env` и укажи обязательные секреты.
5. Startup command: `python main.py`.
6. Подключи постоянный volume к каталогу сервера, чтобы SQLite и backup не терялись при пересоздании контейнера.

Дополнительный Docker, webhook, домен и внешняя база данных не требуются.

При первом запуске бот автоматически создаёт:

```text
data/bot.db
logs/bot.log
backups/
```

---

## 💎 Crypto Pay

Создай Crypto Pay app через `@CryptoBot`. Для автоматических выплат отдельно включи **Transfers** в настройках Security приложения.

По умолчанию вывод работает в режиме `MANUAL`. Режим можно менять из Telegram-админки.

Счёт создаётся после локального заказа. При неопределённом сетевом результате бот ищет invoice по уникальному payload. Все активные счета обрабатывает один `PaymentChecker`, поэтому webhook не нужен.

Для выплат `spend_id` сохраняется **до** API-вызова. Если запрос завершился timeout, заявка остаётся в `PROCESSING`: worker сначала выполняет reconciliation через `getTransfers` и повторно использует тот же идентификатор вместо создания второго перевода.

---

## ⚙️ Администрирование

SUPERADMIN открывает `/admin` или кнопку **«⚙️ Админ-панель»**.

Из Telegram доступны:

- создание, переименование, архивирование и управление категориями;
- мастер создания и редактирования товаров;
- UNIQUE stock;
- поиск пользователей по Telegram ID, username или внутреннему ID;
- ban, история заказов, рефералы и ledger;
- ручные начисления/списания с обязательной причиной;
- возвраты balance-заказов;
- approve/reject выплат;
- настройки, Crypto Pay diagnostics, статистика, рассылки и backup.

Дополнительные роли:

```text
/admin_add TELEGRAM_ID ADMIN
/admin_add TELEGRAM_ID SUPPORT
/admin_remove TELEGRAM_ID
```

Только SUPERADMIN может управлять ролями. Самого SUPERADMIN отключить или заблокировать нельзя.

---

## 💰 Финансовая модель

Все суммы хранятся как `INTEGER` в минимальных денежных единицах. Изменения внутреннего баланса проходят через `BalanceService` и `balance_transactions`.

Ledger хранит общий и доступный к выводу баланс до/после каждой операции. Поэтому настройка `cashback_withdrawable=false` действительно запрещает вывод соответствующих начислений, но не мешает использовать их для покупок внутри магазина.

Идемпотентность предусмотрена для:

- платежей;
- заказов;
- cashback;
- referral reward;
- ledger-операций;
- withdrawals.

По умолчанию cashback и referral reward начисляются только за внешнюю Crypto Pay оплату. Для balance-покупок это можно включить через `balance_rewards_enabled`.

---

## 🗂 Структура проекта

```text
telegram-crypto-shop/
├── bot/
│   ├── database/       # SQLite, модели и миграции
│   ├── handlers/       # user/admin/payment/shop handlers
│   ├── keyboards/      # Telegram keyboards
│   ├── middlewares/    # access control и rate limiting
│   ├── services/       # бизнес-логика, payments, backup, outbox
│   ├── states/         # FSM states
│   └── utils/
├── tests/              # финансовые и reliability-тесты
├── backups/            # runtime backups
├── data/               # SQLite database
├── logs/               # runtime logs
├── .env.example
├── main.py
└── requirements.txt
```

---

## 🧪 Тесты

В проекте есть **16 unit/integration-style тестов**, покрывающих критичные финансовые сценарии:

- double processing;
- cashback/referral idempotency;
- UNIQUE stock;
- money precision;
- insufficient balance;
- self-referral protection;
- restart recovery;
- backup consistency;
- broadcast persistence;
- withdrawal timeout/reconciliation;
- безопасные refund-сценарии.

GitHub Actions автоматически запускает compile check, self-check и тесты для push/PR.

---

## 🔐 Безопасность

- Не публикуй `.env`, реальные bot tokens и Crypto Pay tokens.
- Перед публикацией секретов отзывай старые токены и выпускай новые.
- Для production используй отдельное Crypto Pay приложение.
- Регулярно скачивай backup базы из Pterodactyl.
- Для security-вопросов см. [`SECURITY.md`](SECURITY.md).

---

## 👤 Автор и контакты

<div align="center">

**KodoDrive**

[![GitHub](https://img.shields.io/badge/GitHub-@svod011929-181717?logo=github)](https://github.com/svod011929)
[![Email](https://img.shields.io/badge/Email-antihype2205%40yandex.ru-FFCC00?logo=maildotru&logoColor=black)](mailto:antihype2205@yandex.ru)

Разработка Telegram-ботов, automation-инструментов и инфраструктурных решений.

</div>

---

<div align="center">

**Telegram Crypto Shop v0.1.0** · built by [KodoDrive](https://github.com/svod011929)

</div>
