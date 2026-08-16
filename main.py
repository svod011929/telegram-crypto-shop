from __future__ import annotations

import argparse
import asyncio
import logging
import tempfile
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from bot.config import Config, ConfigurationError
from bot.database.db import Database
from bot.handlers import (
    admin,
    admin_catalog,
    admin_finance,
    admin_system,
    balance,
    errors,
    payments,
    referrals,
    shop,
    user,
)
from bot.logging_setup import setup_logging
from bot.middlewares.access import UserAccessMiddleware
from bot.middlewares.rate_limit import Cooldowns
from bot.services.backup_service import BackupService
from bot.services.balance_service import BalanceService
from bot.services.broadcast_service import BroadcastService
from bot.services.catalog_service import CatalogService
from bot.services.crypto_pay import CryptoPayClient, CryptoPayError
from bot.services.order_service import OrderService
from bot.services.outbox_service import OutboxWorker
from bot.services.payment_checker import PaymentChecker
from bot.services.settings_service import SettingsService
from bot.services.user_service import UserService
from bot.services.withdrawal_service import WithdrawalService, WithdrawalWorker

logger = logging.getLogger(__name__)


async def run_bot(config: Config) -> None:
    Path("data").mkdir(parents=True, exist_ok=True)
    Path("logs").mkdir(parents=True, exist_ok=True)
    Path("backups").mkdir(parents=True, exist_ok=True)
    setup_logging(config.log_level, [config.bot_token, config.crypto_pay_token])
    logger.info("Starting Telegram Crypto Shop")

    db = Database(config.database_path)
    await db.connect()
    settings = SettingsService(db)
    await db.execute(
        "UPDATE settings SET value = ?, updated_by = 0, updated_at = CURRENT_TIMESTAMP "
        "WHERE key = 'payment_poll_interval' AND updated_by IS NULL",
        (str(config.payment_poll_interval),),
    )
    users = UserService(db, config.superadmin_id)
    await users.ensure_superadmin()
    balance_service = BalanceService(db)
    crypto = CryptoPayClient(
        config.crypto_pay_token, config.crypto_base_url, config.crypto_timeout
    )
    await crypto.start()
    try:
        me = await crypto.get_me()
        logger.info("Crypto Pay connected: app_id=%s", me.get("app_id", "unknown"))
    except CryptoPayError:
        logger.exception(
            "Crypto Pay startup diagnostic failed; Telegram bot will continue"
        )

    catalog = CatalogService(db)
    orders = OrderService(db, balance_service, settings, crypto)
    payment_checker = PaymentChecker(
        db, orders, settings, config.payment_poll_interval, config.payment_batch_size
    )
    withdrawals = WithdrawalService(db, balance_service, settings, crypto)
    withdrawal_worker = WithdrawalWorker(db, withdrawals)
    backups = BackupService(db, settings)
    broadcasts = BroadcastService(db)
    cooldowns = Cooldowns()

    bot = Bot(config.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    outbox = OutboxWorker(db, bot, settings)
    dispatcher = Dispatcher(storage=MemoryStorage())
    normal_routers = [
        user.router,
        shop.router,
        payments.router,
        balance.router,
        referrals.router,
    ]
    for normal_router in normal_routers:
        normal_router.message.outer_middleware(UserAccessMiddleware())
        normal_router.callback_query.outer_middleware(UserAccessMiddleware())
        dispatcher.include_router(normal_router)
    dispatcher.include_routers(
        admin.router,
        admin_catalog.router,
        admin_finance.router,
        admin_system.router,
        errors.router,
    )

    background = [
        asyncio.create_task(payment_checker.run(), name="payment-checker"),
        asyncio.create_task(outbox.run(), name="outbox-worker"),
        asyncio.create_task(withdrawal_worker.run(), name="withdrawal-worker"),
        asyncio.create_task(backups.run(), name="backup-worker"),
    ]
    try:
        await bot.delete_webhook(drop_pending_updates=False)
        await dispatcher.start_polling(
            bot,
            users=users,
            settings=settings,
            catalog=catalog,
            orders=orders,
            balance=balance_service,
            payment_checker=payment_checker,
            withdrawals=withdrawals,
            backups=backups,
            broadcasts=broadcasts,
            cooldowns=cooldowns,
            allowed_updates=dispatcher.resolve_used_update_types(),
        )
    finally:
        logger.info("Stopping workers")
        payment_checker.stop()
        outbox.stop()
        withdrawal_worker.stop()
        backups.stop()
        for task in background:
            task.cancel()
        await asyncio.gather(*background, return_exceptions=True)
        await dispatcher.storage.close()
        await bot.session.close()
        await crypto.close()
        await db.close()
        logger.info("Telegram Crypto Shop stopped")


async def self_check() -> None:
    with tempfile.TemporaryDirectory(prefix="telegram_crypto_shop_check_") as temporary:
        db = Database(Path(temporary) / "bot.db")
        await db.connect()
        users = UserService(db, 1)
        await users.ensure_superadmin()
        settings = SettingsService(db)
        assert await db.integrity_check() == "ok"
        assert not await db.fetchall("PRAGMA foreign_key_check")
        assert await settings.get("shop_name")
        tables = await db.fetchall(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
        assert len(tables) >= 15
        dispatcher = Dispatcher(storage=MemoryStorage())
        dispatcher.include_routers(
            user.router,
            shop.router,
            payments.router,
            balance.router,
            referrals.router,
            admin.router,
            admin_catalog.router,
            admin_finance.router,
            admin_system.router,
            errors.router,
        )
        assert dispatcher.resolve_used_update_types()
        await dispatcher.storage.close()
        await db.close()
    print("SELF-CHECK OK: imports, routers, migrations, SQLite integrity, defaults")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Telegram Crypto Shop")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Run local startup diagnostics without Telegram or Crypto Pay",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.check:
        asyncio.run(self_check())
        return 0
    try:
        config = Config.from_env(require_tokens=True)
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}")
        return 2
    try:
        asyncio.run(run_bot(config))
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
