"""Модуль рерайту постів за допомогою Google Gemini API (google-genai)."""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

# Ліміти Telegram на довжину повідомлення та підпису до медіа
TG_TEXT_LIMIT = 4096
TG_CAPTION_LIMIT = 1024


class Rewriter:
    """Асинхронний клієнт для переписування текстів через Gemini API з retry та контролем довжини."""

    def __init__(
        self,
        api_key: str,
        model_name: str,
        prompt_file: Path,
        footer_text: str = "",
        max_retries: int = 4,
        base_delay: float = 2.0,
    ) -> None:
        self.client = genai.Client(api_key=api_key)
        self.model_name = model_name
        self.prompt_file = prompt_file
        self.footer_text = footer_text.strip()
        self.max_retries = max_retries
        self.base_delay = base_delay

    def _load_system_prompt(self) -> str:
        """Зчитує системний промпт із файлу при кожному виклику (дозволяє редагувати без рестарту)."""
        if not self.prompt_file.exists():
            raise FileNotFoundError(f"Файл системного промпту не знайдено: {self.prompt_file}")
        return self.prompt_file.read_text(encoding="utf-8").strip()

    def get_max_body_length(self, has_media: bool) -> int:
        """Обчислює максимальну допустиму довжину основного тексту з урахуванням футера."""
        limit = TG_CAPTION_LIMIT if has_media else TG_TEXT_LIMIT
        if self.footer_text:
            # 2 символи на подвійний перенос рядка '\n\n' перед футером + запас на HTML-теги
            return max(200, limit - len(self.footer_text) - 30)
        return limit - 20

    def format_with_footer(self, body_text: str, has_media: bool) -> str:
        """Додає футер до тексту та гарантує дотримання лімітів Telegram (4096 / 1024)."""
        limit = TG_CAPTION_LIMIT if has_media else TG_TEXT_LIMIT
        clean_body = body_text.strip()

        if self.footer_text:
            max_body = limit - len(self.footer_text) - 2
            if len(clean_body) > max_body:
                clean_body = self._safe_truncate(clean_body, max_body)
            return f"{clean_body}\n\n{self.footer_text}"

        if len(clean_body) > limit:
            clean_body = self._safe_truncate(clean_body, limit)
        return clean_body

    @staticmethod
    def _safe_truncate(text: str, max_len: int) -> str:
        """Обрізає текст за межею речення або слова та закриває незакриті HTML-теги."""
        if len(text) <= max_len:
            return text
        cut = text[: max_len - 1]
        last_dot = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "), cut.rfind("\n"))
        if last_dot > int(max_len * 0.6):
            cut = cut[: last_dot + 1]
        else:
            last_space = cut.rfind(" ")
            if last_space > 0:
                cut = cut[:last_space] + "…"
            else:
                cut += "…"

        # Видаляємо обірваний напіввідкритий тег наприкінці, якщо він є
        cut = re.sub(r"<[^>]*$", "", cut)
        # Закриваємо базові парні HTML-теги у зворотному порядку
        for tag in ("b", "i", "u", "s", "code", "a"):
            open_count = len(re.findall(rf"<{tag}(?:\s[^>]*)?>", cut, flags=re.IGNORECASE))
            close_count = len(re.findall(rf"</{tag}>", cut, flags=re.IGNORECASE))
            if open_count > close_count:
                cut += f"</{tag}>" * (open_count - close_count)
        return cut

    async def rewrite(self, original_text: str, has_media: bool = False) -> str:
        """Виконує рерайт тексту через Gemini API з експоненційним retry."""
        system_prompt = self._load_system_prompt()
        max_body_len = self.get_max_body_length(has_media=has_media)

        user_prompt = (
            f"Ліміт довжини готового тексту (разом із тегами): не більше {max_body_len} символів.\n\n"
            f"Оригінальний текст поста для рерайту:\n"
            f"---\n{original_text}\n---"
        )

        last_exc: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                response = await self.client.aio.models.generate_content(
                    model=self.model_name,
                    contents=user_prompt,
                    config=types.GenerateContentConfig(
                        system_instruction=system_prompt,
                        temperature=0.5,
                    ),
                )
                result_text = (response.text or "").strip()
                if not result_text:
                    raise ValueError("LLM повернула порожню відповідь")

                return self.format_with_footer(result_text, has_media=has_media)

            except Exception as exc:
                last_exc = exc
                if attempt == self.max_retries:
                    logger.error(
                        "Помилка звернення до LLM після %d спроб: %s",
                        self.max_retries,
                        exc,
                    )
                    break

                delay = self.base_delay * (2 ** (attempt - 1))
                logger.warning(
                    "Помилка LLM (спроба %d/%d): %s. Повтор через %.1f сек...",
                    attempt,
                    self.max_retries,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)

        raise RuntimeError(f"Не вдалося виконати рерайт через LLM: {last_exc}") from last_exc
