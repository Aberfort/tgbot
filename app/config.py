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
    source_channel: Union[int, str] = Field(..., alias="SOURCE_CHANNEL")

    # Bot клієнт (публікація та модерація)
    bot_token: SecretStr = Field(..., alias="BOT_TOKEN")
    bot_session_name: str = Field(default="sessions/bot", alias="BOT_SESSION_NAME")
    target_channel: Union[int, str] = Field(..., alias="TARGET_CHANNEL")
    admin_user_id: int = Field(..., alias="ADMIN_USER_ID")

    # Налаштування LLM (Gemini API; підтримується також ANTHROPIC_API_KEY як фолбек)
    gemini_api_key: Optional[SecretStr] = Field(default=None, alias="GEMINI_API_KEY")
    anthropic_api_key: Optional[SecretStr] = Field(default=None, alias="ANTHROPIC_API_KEY")
    llm_model: str = Field(default="gemini-2.5-flash", alias="LLM_MODEL")
    prompt_file: Path = Field(default=Path("prompts/rewrite.txt"), alias="PROMPT_FILE")

    # Модерація та фільтри
    moderation: bool = Field(default=True, alias="MODERATION")
    min_length: int = Field(default=80, alias="MIN_LENGTH", ge=1)
    stop_words_raw: str = Field(default="", alias="STOP_WORDS")
    max_media_size_mb: int = Field(default=50, alias="MAX_MEDIA_SIZE_MB", ge=1, le=2000)

    # Оформлення та ліміти
    footer_text: str = Field(default="", alias="FOOTER_TEXT")
    publish_delay_sec: int = Field(default=30, alias="PUBLISH_DELAY_SEC", ge=0)
    max_posts_per_hour: int = Field(default=10, alias="MAX_POSTS_PER_HOUR", ge=1)
    quiet_hours: str = Field(default="", alias="QUIET_HOURS")

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
        """Створює необхідні директорії та обмежує права доступу до файлів сесій (0700 / 0600)."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.media_tmp_dir.mkdir(parents=True, exist_ok=True)
        self.log_file.parent.mkdir(parents=True, exist_ok=True)

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
