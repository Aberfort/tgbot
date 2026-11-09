"""Безпечна авторизація Telethon Userbot через QR-код (без витоку токенів на зовнішні сервіси)."""

from __future__ import annotations

import asyncio
import getpass
import os
from pathlib import Path

import qrcode
from telethon import TelegramClient, errors

from app.config import Settings


async def main() -> None:
    settings = Settings()
    settings.ensure_directories()

    print("=" * 65)
    print(" АВТОРИЗАЦІЯ TELETHON ЧЕРЕЗ QR-КОД (БЕЗ SMS)")
    print("=" * 65)

    client = TelegramClient(
        session=settings.session_name,
        api_id=settings.api_id,
        api_hash=settings.api_hash.get_secret_value(),
        device_model="Desktop",
        system_version="macOS 14.5",
        app_version="5.4.1 x64",
        lang_code="uk",
    )

    await client.connect()
    try:
        if await client.is_user_authorized():
            me = await client.get_me()
            print(f"✅ Акаунт уже авторизовано як: {me.first_name} (@{me.username or 'немає'})")
            return

        print("\nГенеруємо токен та локальний QR-код для входу...")
        qr_login = await client.qr_login()

        print("\n" + "=" * 65)
        print("ЯК АВТОРИЗУВАТИСЯ:")
        print("1. Відкрийте Telegram на своєму смартфоні.")
        print("2. Перейдіть у: Налаштування -> Пристрої -> Підключити пристрій")
        print("   (Settings -> Devices -> Link Desktop Device)")
        print("3. Наведіть камеру на QR-код нижче:")
        print("=" * 65 + "\n")

        # Локальна генерація QR-коду в терміналі (без сторонніх веб-сервісів!)
        qr = qrcode.QRCode()
        qr.add_data(qr_login.url)
        qr.print_ascii(invert=True)

        print("Очікуємо підтвердження зі смартфона (120 сек)...")

        try:
            user = await qr_login.wait(timeout=120)
        except errors.SessionPasswordNeededError:
            print("\nПотрібен пароль двофакторної автентифікації (2FA).")
            password = getpass.getpass("Введіть ваш хмарний пароль Telegram: ")
            user = await client.sign_in(password=password)
        except asyncio.TimeoutError:
            print("\nЧас очікування минув (120 сек). Спробуйте запустити скрипт ще раз.")
            return

        session_file = Path(f"{settings.session_name}.session").resolve()
        if session_file.exists():
            try:
                os.chmod(session_file, 0o600)
            except OSError:
                pass

        print("\n" + "=" * 65)
        print(f" УСПІХ! Авторизовано як: {user.first_name} (@{user.username or 'немає'}) [ID: {user.id}]")
        print(f" Сесію успішно збережено (права 0600): {session_file}")
        print("=" * 65)
        print("Тепер можна запускати основний проєкт: docker compose up -d")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
