import asyncio
import json
import signal
import sys
import os
import time
import traceback

from loguru import logger
from localization import load_language, t

from account_manager import AccountManager
from discord_webhook import DiscordWebhook
from config_validator import validate_config
import web_server

# тест памяти
ENABLE_MEMORY_MONITOR = False 

log_memory_usage = None
if ENABLE_MEMORY_MONITOR:
    try:
        from memory_monitor import log_memory_usage
    except ImportError:
        logger.warning("⚠️ memory_monitor.py не найден, хотя ENABLE_MEMORY_MONITOR=True")

telegram_bot = None
discord_hook = None
account_manager = None

async def main():
    global account_manager, telegram_bot, discord_hook

    # 1. Конфиг
    try:
        with open("config.json", "r", encoding="utf-8") as f:
            config = json.load(f)

        logger.remove()
        log_level = "DEBUG" if config.get("Debug", False) else "INFO"
        logger.add(sys.stderr, level=log_level)

        log_file = config.get("Log_file", "miner.log")
        if log_file:
            retention = config.get("Log_retention_days", 7)
            logger.add(
                log_file,
                level="DEBUG",
                rotation="10 MB",
                retention=f"{retention} days",
                encoding="utf-8",
            )
            logger.info(f"📝 Файловый лог: {log_file}")

        logger.info(f"🔧 Log level: {log_level}")

        load_language(config.get("Language", "en"))

        warnings, fatal = validate_config(config)
        for w in warnings:
            logger.warning(f"⚙️ Конфиг: {w}")
        if fatal:
            logger.critical(f"⛔ Конфиг невалиден: {fatal}")
            sys.exit(0)

    except Exception as e:
        logger.add(sys.stderr, level="INFO")
        logger.critical(f"Ошибка загрузки конфига: {e}")
        return

    # 2. Account Manager
    account_manager = AccountManager(config)
    all_streamers = account_manager.get_all_streamers_flat()
    logger.info(f"📋 Всего уникальных стримеров: {len(all_streamers)}")

    # 3. Discord Webhook
    discord_hook = DiscordWebhook(config)
    if discord_hook.enabled:
        account_manager.set_discord(discord_hook)
        logger.info("🟣 Discord webhook enabled")

    # 4. Telegram
    if config.get("Telegram", {}).get("enabled", False):
        try:
            from tg_bot.bot import TelegramBot
            telegram_bot = TelegramBot(config)
            telegram_bot.set_account_manager(account_manager)
            account_manager.set_telegram(telegram_bot)
            await telegram_bot.start()
        except Exception as e:
            logger.error(f"Telegram не запустился: {e}")

    # 5. Web Dashboard
    web_cfg = config.get("WebDashboard", {})
    if web_cfg.get("enabled", False):
        port = web_cfg.get("port", 5000)
        try:
            web_server.start_server(account_manager, port)
            logger.info(f"🌍 Web Dashboard: http://localhost:{port}")
        except Exception as e:
            logger.error(f"Web Dashboard не запустился: {e}")

    # 6. Отправка startup уведомлений
    if discord_hook.enabled:
        discord_hook.send_startup(
            account_manager.get_all_status()
        )

    if log_memory_usage:
        asyncio.create_task(log_memory_usage(interval=60))
        logger.info("📊 Memory Monitor: ЗАПУЩЕН")

    # 7. Сторож токенов
    watch_hours = config.get("Token_check_hours", 6)
    try:
        watch_hours = float(watch_hours or 0)
    except (TypeError, ValueError):
        watch_hours = 0
    if watch_hours > 0:
        asyncio.create_task(
            account_manager.token_watchdog(watch_hours)
        )
    else:
        logger.info("🔑 Сторож токенов выключен")

    # 8. Запуск всех аккаунтов
    await account_manager.start_all()


if __name__ == "__main__":
    # SIGTERM (docker stop / systemctl stop) -> чистый выход без рестарта
    try:
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    except Exception:
        pass

    while True:
        try:
            logger.info("🚀 Запуск Miner...")
            asyncio.run(main())
        except KeyboardInterrupt:
            logger.info("👋 Остановлено пользователем")
            if discord_hook and discord_hook.enabled:
                discord_hook.send_restart("User stopped (Ctrl+C)")
            sys.exit(0)
        except SystemExit as e:
            if e.code in (0, None):
                logger.info("👋 Чистый выход (код 0)")
                break
            logger.info(f"🔄 Перезапуск по SystemExit({e.code})...")
            if discord_hook and discord_hook.enabled:
                discord_hook.send_restart(f"SystemExit({e.code})")
        except Exception as e:
            logger.critical(f"🔥 Критическая ошибка: {e}")
            traceback.print_exc()
            if discord_hook and discord_hook.enabled:
                discord_hook.send_error("System", "main.py", str(e))

        logger.info("🔄 Перезапуск через 5 секунд...")
        time.sleep(5)
