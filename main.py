"""Точка входу застосунку: ініціалізація логування, клієнтів Telethon, БД та життєвого циклу."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from telethon import TelegramClient

from app.config import Settings
from app.filters import PostFilter
from app.listener import SourceListener
from app.moderation import ModerationManager
from app.publisher import Publisher
from app.rewriter import Rewriter
from app.storage import Storage


def setup_logging(log_file: Path, log_level: str) -> None:
    """Налаштовує логування у консоль та файл із ротацією (RotatingFileHandler)."""
    level = getattr(logging, log_level.upper(), logging.INFO)
    log_format = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    date_format = "%Y-%m-%d %H:%M:%S"

    formatter = logging.Formatter(fmt=log_format, datefmt=date_format)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)

    file_handler = RotatingFileHandler(
        filename=log_file,
        maxBytes=5 * 1024 * 1024,  # 5 МБ на файл
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    root_logger.handlers.clear()
    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)

    # Зменшуємо деталізацію службових логів Telethon та httpx
    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


async def run() -> None:
    """Основна асинхронна корутина запуску та graceful shutdown бота."""
    settings = Settings()
    settings.ensure_directories()
    setup_logging(settings.log_file, settings.log_level)

    logger = logging.getLogger("main")
    logger.info("Ініціалізація Telegram-рерайт бота...")

    storage = Storage(settings.db_path)
    await storage.connect()

    api_hash_str = settings.api_hash.get_secret_value()
    bot_token_str = settings.bot_token.get_secret_value()

    # Клієнт №1: MTProto Userbot (читання чужого публічного каналу)
    user_client = TelegramClient(
        session=settings.session_name,
        api_id=settings.api_id,
        api_hash=api_hash_str,
        device_model="Desktop",
        system_version="macOS 14.5",
        app_version="5.4.1 x64",
        lang_code="uk",
        auto_reconnect=True,
        connection_retries=None,  # Нескінченні спроби перепідключення при обриві мережі
        retry_delay=5,
    )

    # Клієнт №2: Bot API через MTProto (публікація в наш канал та модерація)
    bot_client = TelegramClient(
        session=settings.bot_session_name,
        api_id=settings.api_id,
        api_hash=api_hash_str,
        auto_reconnect=True,
        connection_retries=None,
        retry_delay=5,
    )

    rewriter = Rewriter(
        api_key=settings.effective_llm_api_key,
        model_name=settings.llm_model,
        prompt_file=settings.prompt_file,
        footer_text=settings.footer_text,
    )
    post_filter = PostFilter(
        min_length=settings.min_length,
        stop_words=settings.stop_words,
    )
    publisher = Publisher(
        bot_client=bot_client,
        storage=storage,
        settings=settings,
    )
    moderation = ModerationManager(
        bot_client=bot_client,
        storage=storage,
        publisher=publisher,
        rewriter=rewriter,
        admin_user_id=settings.admin_user_id,
    )
    listener = SourceListener(
        user_client=user_client,
        settings=settings,
        storage=storage,
        post_filter=post_filter,
        rewriter=rewriter,
        publisher=publisher,
        moderation=moderation,
    )

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _signal_handler() -> None:
        logger.info("Отримано сигнал завершення (SIGINT/SIGTERM). Зупиняємо сервіси...")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            pass

    try:
        logger.info("Запуск Userbot-клієнта...")
        await user_client.start()
        me_user = await user_client.get_me()
        logger.info("Userbot авторизовано як: %s (id=%d)", me_user.first_name, me_user.id)

        logger.info("Запуск Bot-клієнта...")
        await bot_client.start(bot_token=bot_token_str)
        me_bot = await bot_client.get_me()
        logger.info("Бот авторизовано як: @%s (id=%d)", me_bot.username, me_bot.id)

        # Захищаємо створені файли сесій правами 0600
        settings.ensure_directories()

        # Кешуємо entity джерела та цільового каналу при старті для перевірки доступу
        source_entity = await user_client.get_entity(settings.source_channel)
        logger.info(
            "Канал-джерело знайдено: %s",
            getattr(source_entity, "title", settings.source_channel),
        )
        target_entity = await bot_client.get_entity(settings.target_channel)
        logger.info(
            "Цільовий канал знайдено: %s",
            getattr(target_entity, "title", settings.target_channel),
        )

        # Реєстрація обробників та запуск черги публікації
        moderation.register_handlers()
        listener.register_handlers()
        await publisher.start()

        logger.info(
            "Бот повністю готовий до роботи! Режим модерації: %s",
            "УВІМКНЕНО" if settings.moderation else "ВИМКНЕНО (автопублікація)",
        )

        # Очікуємо сигналу зупинки або відключення клієнтів
        await stop_event.wait()

    finally:
        logger.info("Виконується graceful shutdown...")
        await publisher.stop()
        if user_client.is_connected():
            await user_client.disconnect()
        if bot_client.is_connected():
            await bot_client.disconnect()
        await storage.close()
        logger.info("Роботу бота коректно завершено.")


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
