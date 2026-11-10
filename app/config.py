"""Модуль конфігурації застосунку на базі pydantic-settings із захистом секретів."""

from __future__ import annotations

import os
from datetime import time
from pathlib import Path
from typing import Optional, Union

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Налаштування бота, що зчитуються зі змінних оточення або файлу .env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # MTProto Userbot (читання джерела)
    api_id: int = Field(..., alias="API_ID", description="Telegram API ID з my.telegram.org")
    api_hash: SecretStr = Field(..., alias="API_HASH", description="Telegram API Hash з my.telegram.org")
    session_name: str = Field(default="sessions/userbot", alias="SESSION_NAME")
    userbot_session_b64: Optional[SecretStr] = Field(default=None, alias="USERBOT_SESSION_B64")
    userbot_session_b64_1: Optional[SecretStr] = Field(default=None, alias="USERBOT_SESSION_B64_1")
    userbot_session_b64_2: Optional[SecretStr] = Field(default=None, alias="USERBOT_SESSION_B64_2")
    source_channel: Union[int, str] = Field(..., alias="SOURCE_CHANNEL")

    # Bot клієнт (публікація та модерація)
    bot_token: SecretStr = Field(..., alias="BOT_TOKEN")
    bot_session_name: str = Field(default="sessions/bot", alias="BOT_SESSION_NAME")
    target_channel: Union[int, str] = Field(..., alias="TARGET_CHANNEL")
    admin_user_id: int = Field(..., alias="ADMIN_USER_ID")

    # Налаштування LLM (Gemini API; підтримується також ANTHROPIC_API_KEY як фолбек)
    gemini_api_key: Optional[SecretStr] = Field(default=None, alias="GEMINI_API_KEY")
    anthropic_api_key: Optional[SecretStr] = Field(default=None, alias="ANTHROPIC_API_KEY")
    llm_model: str = Field(default="gemini-3.5-flash", alias="LLM_MODEL")
    prompt_file: Path = Field(default=Path("prompts/rewrite.txt"), alias="PROMPT_FILE")

    # Модерація та фільтри
    moderation: bool = Field(default=True, alias="MODERATION")
    min_length: int = Field(default=0, alias="MIN_LENGTH", ge=0)
    stop_words_raw: str = Field(default="", alias="STOP_WORDS")
    max_media_size_mb: int = Field(default=50, alias="MAX_MEDIA_SIZE_MB", ge=1, le=2000)

    # Оформлення та ліміти
    footer_text: str = Field(
        default='<a href="https://t.me/btsdailyhub">BTS Daily Hub 💜</a>',
        alias="FOOTER_TEXT",
    )
    publish_delay_sec: int = Field(default=300, alias="PUBLISH_DELAY_SEC", ge=0)
    max_posts_per_hour: int = Field(default=4, alias="MAX_POSTS_PER_HOUR", ge=1)
    max_queued_posts: int = Field(default=4, alias="MAX_QUEUED_POSTS", ge=1)
    post_ttl_minutes: int = Field(default=45, alias="POST_TTL_MINUTES", ge=5)
    quiet_hours: str = Field(default="", alias="QUIET_HOURS")
    daily_poll_enabled: bool = Field(default=True, alias="DAILY_POLL_ENABLED")
    daily_poll_hour_utc: int = Field(default=17, alias="DAILY_POLL_HOUR_UTC", ge=0, le=23)

    # Шляхи та логування
    db_path: Path = Field(default=Path("data/bot.db"), alias="DB_PATH")
    media_tmp_dir: Path = Field(default=Path("data/tmp_media"), alias="MEDIA_TMP_DIR")
    log_file: Path = Field(default=Path("logs/bot.log"), alias="LOG_FILE")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    @field_validator("source_channel", "target_channel", mode="before")
    @classmethod
    def parse_channel_identifier(cls, value: Union[int, str]) -> Union[int, str]:
        """Перетворює числовий рядок (наприклад '-100123456') на int, інакше очищує username."""
        if isinstance(value, int):
            return value
        val_str = str(value).strip()
        if val_str.startswith("-") and val_str[1:].isdigit():
            return int(val_str)
        if val_str.isdigit():
            return int(val_str)
        return val_str.lstrip("@")

    @model_validator(mode="after")
    def validate_llm_key(self) -> "Settings":
        """Перевіряє наявність API-ключа для LLM (GEMINI_API_KEY або ANTHROPIC_API_KEY)."""
        key = self.effective_llm_api_key
        if not key:
            raise ValueError(
                "Необхідно вказати непорожній GEMINI_API_KEY (або ANTHROPIC_API_KEY) у файлі .env"
            )
        return self

    @property
    def effective_llm_api_key(self) -> str:
        """Повертає активний ключ API у вигляді рядка для викликів Gemini."""
        if self.gemini_api_key and self.gemini_api_key.get_secret_value().strip():
            return self.gemini_api_key.get_secret_value().strip()
        if self.anthropic_api_key and self.anthropic_api_key.get_secret_value().strip():
            return self.anthropic_api_key.get_secret_value().strip()
        return ""

    @property
    def stop_words(self) -> list[str]:
        """Повертає список стоп-слів у нижньому регістрі."""
        if not self.stop_words_raw.strip():
            return []
        return [w.strip().lower() for w in self.stop_words_raw.split(",") if w.strip()]

    @property
    def quiet_hours_range(self) -> Optional[tuple[time, time]]:
        """Парсить рядок тихих годин формату 'HH:MM-HH:MM' у кортеж (start_time, end_time)."""
        raw = self.quiet_hours.strip()
        if not raw:
            return None
        try:
            start_str, end_str = raw.split("-", maxsplit=1)
            sh, sm = map(int, start_str.strip().split(":"))
            eh, em = map(int, end_str.strip().split(":"))
            return time(sh, sm), time(eh, em)
        except Exception as exc:
            raise ValueError(
                f"Невірний формат QUIET_HOURS='{raw}'. Очікується HH:MM-HH:MM (напр. 23:00-07:00)"
            ) from exc

    def ensure_directories(self) -> None:
        """Створює необхідні директорії, відновлює сесію з base64 (якщо вказано) та обмежує права доступу."""
        import base64

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.media_tmp_dir.mkdir(parents=True, exist_ok=True)
        self.log_file.parent.mkdir(parents=True, exist_ok=True)

        # Відновлення файлу userbot.session зі змінної USERBOT_SESSION_B64 (або частин B64_1 + B64_2)
        b64_val = ""
        if self.userbot_session_b64 and self.userbot_session_b64.get_secret_value().strip():
            b64_val = self.userbot_session_b64.get_secret_value().strip()
        elif self.userbot_session_b64_1 and self.userbot_session_b64_1.get_secret_value().strip():
            p1 = self.userbot_session_b64_1.get_secret_value().strip()
            p2 = self.userbot_session_b64_2.get_secret_value().strip() if self.userbot_session_b64_2 else ""
            b64_val = p1 + p2
        else:
            p1 = os.getenv("USERBOT_SESSION_B64_1", "").strip()
            p2 = os.getenv("USERBOT_SESSION_B64_2", "").strip()
            if p1 or p2:
                b64_val = p1 + p2

        u_sess = Path(f"{self.session_name}.session")

        def is_valid_session_file(sess_file: Path) -> bool:
            if not sess_file.exists() or sess_file.stat().st_size == 0:
                return False
            try:
                import sqlite3
                with sqlite3.connect(sess_file) as conn:
                    cur = conn.cursor()
                    cur.execute("SELECT count(*) FROM entities")
                    cnt = cur.fetchone()[0]
                    return cnt > 0
            except Exception:
                return False

        force_restore = os.getenv("FORCE_RESTORE_SESSION", "").lower() in ("1", "true", "yes")
        is_valid = is_valid_session_file(u_sess)
        print(f"[INIT] Session target path: {u_sess.resolve()}, exists={u_sess.exists()}, valid_auth={is_valid}, b64_len={len(b64_val)}, force={force_restore}")

        if b64_val and (force_restore or not is_valid):
            import gzip

            u_sess.parent.mkdir(parents=True, exist_ok=True)
            raw_bytes = base64.b64decode(b64_val)
            try:
                raw_bytes = gzip.decompress(raw_bytes)
            except Exception as e:
                print(f"[INIT] Gzip decompress note: {e}")
            u_sess.write_bytes(raw_bytes)
            try:
                os.chmod(u_sess, 0o600)
            except OSError:
                pass
            print(f"[INIT] Restored userbot session: {u_sess.resolve()} ({u_sess.stat().st_size} bytes)")

        for sess_prefix in (self.session_name, self.bot_session_name):
            sess_dir = Path(sess_prefix).parent
            sess_dir.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(sess_dir, 0o700)
            except OSError:
                pass
            sess_file = Path(f"{sess_prefix}.session")
            if sess_file.exists():
                try:
                    os.chmod(sess_file, 0o600)
                except OSError:
                    pass
