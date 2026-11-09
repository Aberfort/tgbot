"""Модуль моніторингу джерела (Telethon userbot) з автопідпискою, realtime-подіями та catch-up polling."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional, Sequence

from telethon import TelegramClient, events
from telethon.tl.custom.message import Message
from telethon.tl.functions.channels import JoinChannelRequest

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
        self._poll_task: Optional[asyncio.Task[None]] = None
        self._stopping = False

    async def initialize(self) -> None:
        """Перевіряє доступність джерела та гарантує підписку (Join) userbot на канал."""
        try:
            entity = await self.user_client.get_entity(self.settings.source_channel)
            logger.info("Канал-джерело знайдено: %s (id=%s)", getattr(entity, "title", self.settings.source_channel), entity.id)
            # В Telegram MTProto оновлення приходять лише для каналів, на які акаунт підписаний!
            try:
                await self.user_client(JoinChannelRequest(entity))
                logger.info("Userbot успішно приєднано (joined) до каналу: %s", self.settings.source_channel)
            except Exception as join_err:
                logger.debug("Статус підписки на канал (можливо вже підписані): %s", join_err)
        except Exception as exc:
            logger.error("Не вдалося ініціалізувати канал-джерело %s: %s", self.settings.source_channel, exc)

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
        # 3. Обробник редагованих повідомлень (коли автор додає підпис через 5-30 секунд)
        self.user_client.add_event_handler(
            self._on_message_edited,
            events.MessageEdited(chats=[self.settings.source_channel]),
        )
        logger.info("Слухач джерела підписано на події каналу (включно з редагуванням): %s", self.settings.source_channel)

        # Запускаємо фоновий catch-up polling (кожні 45 секунд) для гарантованого перехоплення,
        # навіть якщо Telegram скинув сокет або ПК засинав.
        if self._poll_task is None or self._poll_task.done():
            self._stopping = False
            self._poll_task = asyncio.create_task(self._catchup_poll_loop(), name="source_catchup_poll")
            logger.info("Фоновий catch-up polling запущено (інтервал 45 сек).")

    async def stop(self) -> None:
        """Зупиняє фоновий polling під час graceful shutdown."""
        self._stopping = True
        if self._poll_task is not None and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass

    async def _catchup_poll_loop(self) -> None:
        """Періодично перевіряє останні повідомлення каналу для 100% надійності."""
        # Невелика пауза при старті, щоб завершилась ініціалізація
        await asyncio.sleep(5)
        while not self._stopping:
            try:
                await self._poll_latest_messages()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("Помилка під час catch-up polling: %s", exc)

            try:
                await asyncio.sleep(45)
            except asyncio.CancelledError:
                break

    async def _poll_latest_messages(self) -> None:
        """Запитує останні 25 повідомлень із каналу та обробляє нові."""
        try:
            messages = await self.user_client.get_messages(self.settings.source_channel, limit=25)
        except Exception as exc:
            logger.debug("Не вдалося отримати останні повідомлення: %s", exc)
            return

        if not messages:
            return

        # Групуємо повідомлення за grouped_id (альбоми)
        albums: dict[int, list[Message]] = {}
        singles: list[Message] = []

        for msg in reversed(messages):
            if msg.grouped_id:
                albums.setdefault(msg.grouped_id, []).append(msg)
            else:
                singles.append(msg)

        # Обробляємо спочатку альбоми, потім одиночні
        for grouped_id, album_msgs in albums.items():
            primary = album_msgs[0]
            text = "\n\n".join((m.raw_text or "").strip() for m in album_msgs if (m.raw_text or "").strip()).strip()
            thash = compute_text_hash(text)
            if not await self.storage.is_duplicate(primary.id, thash, grouped_id=grouped_id):
                logger.info("Catch-up poll виявив новий альбом grouped_id=%s (msg id=%d)", grouped_id, primary.id)
                await self._process_messages(album_msgs, grouped_id=grouped_id)

        for s_msg in singles:
            stext = (s_msg.raw_text or "").strip()
            thash = compute_text_hash(stext)
            if not await self.storage.is_duplicate(s_msg.id, thash, grouped_id=None):
                logger.info("Catch-up poll виявив новий одиночний пост msg id=%d", s_msg.id)
                await self._process_messages([s_msg], grouped_id=None)

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

    async def _on_message_edited(self, event: events.MessageEdited.Event) -> None:
        """Обробляє редагування повідомлення (коли автор додає підпис через 5-30 секунд)."""
        msg: Message = event.message
        text = (msg.raw_text or "").strip()
        if not text:
            return

        source_msg_id = int(msg.id)
        existing = await self.storage.get_post_by_source_id(source_msg_id)
        if existing and existing.status != PostStatus.SKIPPED:
            return  # Пост уже в конвеєрі обробки або опублікований

        logger.info(
            "Повідомлення source_msg_id=%d відредаговано з новим текстом (%d симв). Запускаємо обробку...",
            source_msg_id,
            len(text),
        )
        if msg.grouped_id:
            try:
                latest = await self.user_client.get_messages(self.settings.source_channel, limit=15)
                album_msgs = [m for m in latest if m.grouped_id == msg.grouped_id] or [msg]
                await self._process_messages(album_msgs, grouped_id=msg.grouped_id)
            except Exception as exc:
                logger.error("Помилка обробки відредагованого альбому grouped_id=%s: %s", msg.grouped_id, exc)
        else:
            await self._process_messages([msg], grouped_id=None)

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

        has_media = any(getattr(m, "media", None) is not None for m in messages)
        # Якщо в пості є медіа (фото/відео), але текст порожній:
        # Автори каналу часто публікують медіа, а підпис додають через кілька секунд!
        if not combined_text and has_media:
            logger.info(
                "Пост source_msg_id=%d має медіа, але текст поки відсутній. Очікуємо 3 сек на підпис...",
                source_msg_id,
            )
            await asyncio.sleep(3)
            try:
                if grouped_id:
                    latest = await self.user_client.get_messages(self.settings.source_channel, limit=15)
                    matched = [m for m in latest if m.grouped_id == grouped_id]
                    if matched:
                        messages = matched
                else:
                    refreshed = await self.user_client.get_messages(self.settings.source_channel, ids=source_msg_id)
                    if refreshed:
                        messages = [refreshed]
                combined_text = "\n\n".join(
                    (m.raw_text or "").strip() for m in messages if (m.raw_text or "").strip()
                ).strip()
                if combined_text:
                    logger.info(
                        "Для поста source_msg_id=%d успішно підтягнуто доданий текст (%d симв): %s...",
                        source_msg_id,
                        len(combined_text),
                        combined_text[:40],
                    )
            except Exception as e:
                logger.debug("Помилка оновлення тексту для source_msg_id=%d: %s", source_msg_id, e)

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

        # Завантаження медіафайлів (фото / відео) у тимчасову теку з перевіркою ліміту розміру
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
        """Завантажує фото та відео з повідомлень джерела, контролюючи максимальний розмір файлу."""
        post_dir: Path = self.settings.media_tmp_dir / f"post_{post_id}"
        downloaded: list[str] = []
        max_bytes = self.settings.max_media_size_mb * 1024 * 1024

        for idx, msg in enumerate(messages, start=1):
            if msg.photo is None and msg.video is None and msg.document is None:
                continue

            file_size = getattr(msg.file, "size", 0) or 0
            if file_size > max_bytes:
                logger.warning(
                    "Медіафайл у повідомленні id=%d перевищує ліміт (%d МБ > %d МБ). Пропускаємо файл.",
                    msg.id,
                    file_size // (1024 * 1024),
                    self.settings.max_media_size_mb,
                )
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
