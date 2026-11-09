"""Модуль моніторингу джерела (Telethon userbot) для одиночних повідомлень та медіа-альбомів."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional, Sequence

from telethon import TelegramClient, events
from telethon.tl.custom.message import Message

from app.config import Settings
from app.filters import PostFilter, compute_text_hash
from app.moderation import ModerationManager
from app.publisher import Publisher
from app.rewriter import Rewriter
from app.storage import PostStatus, Storage

logger = logging.getLogger(__name__)


class SourceListener:
    """Підписується на нові пости та альбоми в каналі-джерелі та запускає конвеєр обробки."""

    def __init__(
        self,
        user_client: TelegramClient,
        settings: Settings,
        storage: Storage,
        post_filter: PostFilter,
        rewriter: Rewriter,
        publisher: Publisher,
        moderation: ModerationManager,
    ) -> None:
        self.user_client = user_client
        self.settings = settings
        self.storage = storage
        self.post_filter = post_filter
        self.rewriter = rewriter
        self.publisher = publisher
        self.moderation = moderation
        self._processing_lock = asyncio.Lock()

    def register_handlers(self) -> None:
        """Реєструє обробники events.Album та events.NewMessage для каналу-джерела."""
        # 1. Обробник медіа-альбомів (grouped_id IS NOT NULL)
        self.user_client.add_event_handler(
            self._on_album,
            events.Album(chats=[self.settings.source_channel]),
        )
        # 2. Обробник одиночних повідомлень (grouped_id IS NULL, щоб не дублювати альбоми)
        self.user_client.add_event_handler(
            self._on_single_message,
            events.NewMessage(
                chats=[self.settings.source_channel],
                func=lambda e: e.grouped_id is None,
            ),
        )
        logger.info("Слухач джерела підписано на канал: %s", self.settings.source_channel)

    async def _on_album(self, event: events.Album.Event) -> None:
        """Обробляє медіа-альбом (кілька фото/відео з однаковим grouped_id) як один пост."""
        messages: list[Message] = list(event.messages)
        if not messages:
            return
        grouped_id = messages[0].grouped_id
        await self._process_messages(messages, grouped_id=grouped_id)

    async def _on_single_message(self, event: events.NewMessage.Event) -> None:
        """Обробляє одиночний пост без альбому."""
        await self._process_messages([event.message], grouped_id=None)

    async def _process_messages(
        self,
        messages: Sequence[Message],
        grouped_id: Optional[int],
    ) -> None:
        """Повний конвеєр обробки нового поста: дедуплікація -> фільтри -> медіа -> LLM -> модерація/публікація."""
        if not messages:
            return

        primary_msg = messages[0]
        source_msg_id = int(primary_msg.id)

        # Збираємо текст із повідомлень (в альбомі підпис зазвичай лише у першому елементі)
        combined_text = "\n\n".join(
            (m.raw_text or "").strip() for m in messages if (m.raw_text or "").strip()
        ).strip()
        text_hash = compute_text_hash(combined_text)

        async with self._processing_lock:
            if await self.storage.is_duplicate(
                source_msg_id=source_msg_id,
                text_hash=text_hash,
                grouped_id=grouped_id,
            ):
                logger.info(
                    "Пост source_msg_id=%d (grouped_id=%s) вже оброблено (дублікат). Пропускаємо.",
                    source_msg_id,
                    grouped_id,
                )
                return

            # Перевірка фільтрами (репост, порожній текст, мін. довжина, стоп-слова)
            filter_res = self.post_filter.check(messages=messages, combined_text=combined_text)
            if not filter_res.passed:
                logger.info(
                    "Пост source_msg_id=%d пропущено фільтром: %s",
                    source_msg_id,
                    filter_res.reason,
                )
                await self.storage.create_post(
                    source_msg_id=source_msg_id,
                    original_text=combined_text,
                    text_hash=text_hash or f"skipped_{source_msg_id}",
                    status=PostStatus.SKIPPED,
                    grouped_id=grouped_id,
                )
                return

            # Реєструємо новий пост у БД зі статусом NEW
            post_id = await self.storage.create_post(
                source_msg_id=source_msg_id,
                original_text=combined_text,
                text_hash=text_hash,
                status=PostStatus.NEW,
                grouped_id=grouped_id,
            )

        logger.info(
            "Новий пост id=%d (source_msg_id=%d, grouped_id=%s) прийнято в обробку.",
            post_id,
            source_msg_id,
            grouped_id,
        )

        # Завантаження медіафайлів (фото / відео) у тимчасову теку
        media_paths = await self._download_media_files(post_id=post_id, messages=messages)
        if media_paths:
            await self.storage.update_media_paths(post_id=post_id, media_paths=media_paths)

        # Рерайт тексту через LLM
        try:
            rewritten_text = await self.rewriter.rewrite(
                original_text=combined_text,
                has_media=len(media_paths) > 0,
            )
            await self.storage.update_rewritten(
                post_id=post_id,
                rewritten_text=rewritten_text,
                status=PostStatus.REWRITTEN,
            )
        except Exception as exc:
            logger.error("Помилка рерайту поста id=%d: %s", post_id, exc)
            await self.storage.update_status(post_id=post_id, status=PostStatus.FAILED)
            self.publisher.cleanup_media(media_paths)
            return

        post_record = await self.storage.get_post(post_id)
        if post_record is None:
            return

        # Відправка на модерацію або в чергу автоматичної публікації
        if self.settings.moderation:
            await self.moderation.send_for_review(post_record)
        else:
            await self.publisher.enqueue(post_record.id)

    async def _download_media_files(
        self,
        post_id: int,
        messages: Sequence[Message],
    ) -> list[str]:
        """Завантажує фото та відео з повідомлень джерела у тимчасову директорію поста."""
        post_dir: Path = self.settings.media_tmp_dir / f"post_{post_id}"
        downloaded: list[str] = []

        for idx, msg in enumerate(messages, start=1):
            # Завантажуємо лише фото та відео/документи, ігноруючи прев'ю веб-сторінок (MessageMediaWebPage)
            if msg.photo is None and msg.video is None and msg.document is None:
                continue

            post_dir.mkdir(parents=True, exist_ok=True)
            target_prefix = post_dir / f"media_{idx}"
            try:
                file_path = await self.user_client.download_media(
                    message=msg,
                    file=str(target_prefix),
                )
                if file_path:
                    downloaded.append(str(Path(file_path).resolve()))
                    logger.debug("Завантажено медіафайл для поста id=%d: %s", post_id, file_path)
            except Exception as exc:
                logger.warning(
                    "Не вдалося завантажити медіа з повідомлення id=%d (пост id=%d): %s",
                    msg.id,
                    post_id,
                    exc,
                )

        return downloaded
