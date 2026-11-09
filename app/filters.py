"""Модуль фільтрації вхідних постів та обчислення хешу для дедуплікації."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Sequence

from telethon.tl.custom.message import Message


@dataclass(frozen=True)
class FilterResult:
    """Результат перевірки поста фільтрами."""

    passed: bool
    reason: str = ""


def compute_text_hash(text: str) -> str:
    """Обчислює нормалізований SHA-256 хеш тексту для надійної дедуплікації."""
    normalized = re.sub(r"\s+", " ", text.strip().lower())
    if not normalized:
        return ""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


class PostFilter:
    """Перевіряє повідомлення з каналу-джерела на репости та стоп-слова."""

    def __init__(self, stop_words: Sequence[str] = (), min_length: int = 0) -> None:
        self.min_length = min_length
        self.stop_words = [w.lower().strip() for w in stop_words if w.strip()]

    def check(self, messages: Sequence[Message], combined_text: str) -> FilterResult:
        """Виконує комплексну перевірку одного повідомлення або альбому повідомлень."""
        if not messages:
            return FilterResult(passed=False, reason="Порожній список повідомлень")

        # 1. Перевірка на репост (forward з іншого каналу/користувача)
        for msg in messages:
            if msg.fwd_from is not None:
                return FilterResult(passed=False, reason="Пост є репостом (fwd_from)")
            if msg.via_bot_id is not None:
                return FilterResult(passed=False, reason="Пост опубліковано через inline-бота")

        # 2. Перевірка наявності тексту
        clean_text = combined_text.strip()
        if not clean_text:
            return FilterResult(passed=False, reason="Пост не містить тексту")

        # 3. Перевірка на рекламні стоп-слова
        lowered = clean_text.lower()
        for word in self.stop_words:
            if word in lowered:
                return FilterResult(passed=False, reason=f"Знайдено стоп-слово: '{word}'")

        return FilterResult(passed=True, reason="OK")
