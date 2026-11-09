"""Утиліта для експорту сесії в Base64 для швидкого деплою на Railway/хмари."""

import base64
import gzip
import sys
from pathlib import Path


def export_session() -> None:
    session_file = Path("sessions/userbot.session")
    if not session_file.exists():
        print("❌ Файл sessions/userbot.session не знайдено!")
        print("Спочатку пройдіть авторизацію через: docker compose run --rm tg-rewriter-bot python login.py")
        sys.exit(1)

    raw_data = session_file.read_bytes()
    compressed = gzip.compress(raw_data)
    encoded = base64.b64encode(compressed).decode("utf-8")

    print("=" * 65)
    print("РЯДОК ДЛЯ RAILWAY ENVIRONMENT VARIABLE (USERBOT_SESSION_B64)")
    print("=" * 65)
    print(encoded)
    print("=" * 65)
    print("📋 Скопіюйте цей довгий рядок і вставте у Railway:")
    print("Змінна: USERBOT_SESSION_B64")
    print(f"Розмір: ~{len(encoded) // 1024} КБ (стиснутий gzip + base64)")
    print("При старті на Railway бот автоматично розпакує вашу сесію!")
    print("=" * 65)


if __name__ == "__main__":
    export_session()

