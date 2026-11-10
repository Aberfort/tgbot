"""Модуль публікації постів у цільовий канал із чергою, відновленням стану, лімітами та очищенням медіа."""

from __future__ import annotations

import asyncio
import logging
import shutil
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Optional

from telethon import TelegramClient
from telethon.errors import FloodWaitError

from app.config import Settings
from app.storage import PostRecord, PostStatus, Storage

logger = logging.getLogger(__name__)


class Publisher:
    """Керує чергою публікацій, дотриманням лімітів і відправкою постів у цільовий канал."""

    def __init__(
        self,
        bot_client: TelegramClient,
        storage: Storage,
        settings: Settings,
    ) -> None:
        self.bot_client = bot_client
        self.storage = storage
        self.settings = settings
        self._queue: asyncio.Queue[int] = asyncio.Queue()
        self._worker_task: Optional[asyncio.Task[None]] = None
        self._stopping = False

    async def start(self) -> None:
        """Відновлює невідправлені пости після рестарту, очищує залишкові медіа та запускає воркер черги."""
        await self._recover_pending_queue()
        await self._cleanup_orphaned_media_dirs()

        if self._worker_task is None or self._worker_task.done():
            self._stopping = False
            self._worker_task = asyncio.create_task(self._worker_loop(), name="publisher_worker")
            logger.info("Воркер черги публікацій запущено.")

    async def _recover_pending_queue(self) -> None:
        """Завантажує з БД пости у статусі QUEUED (та REWRITTEN при вимкненій модерації) після рестарту."""
        post_ids = await self.storage.get_queued_post_ids(
            include_rewritten=not self.settings.moderation,
        )
        for pid in post_ids:
            await self._queue.put(pid)
        if post_ids:
            logger.info("Відновлено %d постів у чергу публікації після перезапуску.", len(post_ids))

    async def _cleanup_orphaned_media_dirs(self) -> None:
        """Видаляє залишкові папки post_<id> у tmp_media, якщо відповідні пости вже завершені."""
        tmp_root = self.settings.media_tmp_dir
        if not tmp_root.exists():
            return
        active_ids = await self.storage.get_active_post_ids()
        for child in tmp_root.iterdir():
            if child.is_dir() and child.name.startswith("post_"):
                suffix = child.name.removeprefix("post_")
                if suffix.isdigit() and int(suffix) not in active_ids:
                    shutil.rmtree(child, ignore_errors=True)
                    logger.debug("Видалено залишкову директорію медіа: %s", child)

    async def stop(self) -> None:
        """Зупиняє фоновий воркер черги публікацій (graceful shutdown)."""
        self._stopping = True
        if self._worker_task is not None and not self._worker_task.done():
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        logger.info("Воркер черги публікацій зупинено.")

    async def enqueue(self, post_id: int) -> None:
        """Зберігає статус QUEUED у БД та додає ID поста до черги на публікацію."""
        # Перевіряємо розмір живої черги перед додаванням нового поста
        active_queued = await self.storage.get_queued_count()
        if active_queued >= self.settings.max_queued_posts:
            logger.warning(
                "Черга переповнена (активних у черзі: %d >= ліміт %d). Пост id=%d скасовано як застарілий (EXPIRED).",
                active_queued,
                self.settings.max_queued_posts,
                post_id,
            )
            await self.storage.update_status(post_id, PostStatus.EXPIRED)
            post = await self.storage.get_post(post_id)
            if post and post.media_paths:
                self.cleanup_media(post.media_paths)
            return

        await self.storage.update_status(post_id, PostStatus.QUEUED)
        await self._queue.put(post_id)
        logger.info(
            "Пост id=%d додано в чергу публікації (у черзі: %d, максимум: %d).",
            post_id,
            self._queue.qsize(),
            self.settings.max_queued_posts,
        )

    def _is_quiet_hour(self, now_time: time) -> bool:
        """Перевіряє, чи входить поточний локальний час у діапазон 'тихих годин'."""
        q_range = self.settings.quiet_hours_range
        if q_range is None:
            return False
        start_t, end_t = q_range
        if start_t <= end_t:
            return start_t <= now_time < end_t
        # Діапазон через північ (наприклад, 23:00-07:00)
        return now_time >= start_t or now_time < end_t

    async def _wait_for_publish_window(self) -> None:
        """Очікує завершення тихих годин та звільнення квоти MAX_POSTS_PER_HOUR."""
        while not self._stopping:
            now_local = datetime.now().time()
            if self._is_quiet_hour(now_local):
                logger.info("Зараз 'тихі години' (%s). Очікування 60 сек...", self.settings.quiet_hours)
                await asyncio.sleep(60)
                continue

            published_last_hour = await self.storage.get_published_count_last_hour()
            if published_last_hour >= self.settings.max_posts_per_hour:
                logger.info(
                    "Досягнуто ліміту %d постів/год (опубліковано %d). Очікування 60 сек...",
                    self.settings.max_posts_per_hour,
                    published_last_hour,
                )
                await asyncio.sleep(60)
                continue

            break

    async def _worker_loop(self) -> None:
        """Основний цикл обробки черги публікацій."""
        while not self._stopping:
            try:
                post_id = await self._queue.get()
            except asyncio.CancelledError:
                break

            try:
                await self._wait_for_publish_window()
                post = await self.storage.get_post(post_id)
                if post is None:
                    logger.warning("Пост id=%d не знайдено в БД перед публікацією.", post_id)
                    continue
                if post.status == PostStatus.PUBLISHED:
                    logger.info("Пост id=%d вже був опублікований раніше. Пропускаємо.", post_id)
                    continue
                if post.status == PostStatus.EXPIRED:
                    logger.info("Пост id=%d скасовано як застарілий (EXPIRED). Пропускаємо.", post_id)
                    continue

                # Перевірка TTL (Time To Live / термін придатності) поста
                try:
                    created_dt = datetime.fromisoformat(post.created_at)
                    now_utc = datetime.now(timezone.utc)
                    if created_dt.tzinfo is None:
                        created_dt = created_dt.replace(tzinfo=timezone.utc)
                    age_minutes = (now_utc - created_dt).total_seconds() / 60.0
                    if age_minutes > self.settings.post_ttl_minutes:
                        logger.warning(
                            "Пост id=%d застарів у черзі (вік: %.1f хв > TTL %d хв). Пропускаємо як EXPIRED.",
                            post_id,
                            age_minutes,
                            self.settings.post_ttl_minutes,
                        )
                        await self.storage.update_status(post.id, PostStatus.EXPIRED)
                        if post.media_paths:
                            self.cleanup_media(post.media_paths)
                        continue
                except Exception as parse_err:
                    logger.warning("Не вдалося перевірити TTL для поста id=%d: %s", post_id, parse_err)

                await self.publish_post_now(post)

                if self.settings.publish_delay_sec > 0:
                    logger.debug(
                        "Затримка між публікаціями: %d сек...",
                        self.settings.publish_delay_sec,
                    )
                    await asyncio.sleep(self.settings.publish_delay_sec)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.exception("Неочікувана помилка у воркері публікації для id=%d: %s", post_id, exc)
            finally:
                self._queue.task_done()

    async def publish_post_now(self, post: PostRecord) -> bool:
        """Відправляє пост (текст, медіа або альбом) у цільовий канал з обробкою FloodWaitError."""
        text_to_send = post.rewritten_text or post.original_text
        existing_media = [p for p in post.media_paths if Path(p).exists()]

        for attempt in range(1, 4):
            try:
                if existing_media:
                    # Повторно завантажуємо файли у цільовий канал (без forward, щоб не було мітки джерела)
                    file_arg = existing_media if len(existing_media) > 1 else existing_media[0]
                    await self.bot_client.send_file(
                        entity=self.settings.target_channel,
                        file=file_arg,
                        caption=text_to_send,
                        parse_mode="html",
                    )
                else:
                    await self.bot_client.send_message(
                        entity=self.settings.target_channel,
                        message=text_to_send,
                        parse_mode="html",
                        link_preview=False,
                    )

                await self.storage.update_status(post.id, PostStatus.PUBLISHED, published=True)
                logger.info(
                    "Пост id=%d (source_msg_id=%d) успішно опубліковано в %s.",
                    post.id,
                    post.source_msg_id,
                    self.settings.target_channel,
                )
                self.cleanup_media(post.media_paths)
                return True

            except FloodWaitError as flood_err:
                wait_sec = int(flood_err.seconds) + 2
                logger.warning(
                    "Отримано FloodWaitError при публікації поста id=%d. Сон на %d сек...",
                    post.id,
                    wait_sec,
                )
                await asyncio.sleep(wait_sec)
            except Exception as exc:
                logger.error(
                    "Помилка публікації поста id=%d (спроба %d/3): %s",
                    post.id,
                    attempt,
                    exc,
                )
                if attempt == 3:
                    await self.storage.update_status(post.id, PostStatus.FAILED)
                    self.cleanup_media(post.media_paths)
                    return False
                await asyncio.sleep(3 * attempt)

        return False

    @staticmethod
    def cleanup_media(media_paths: list[str]) -> None:
        """Видаляє тимчасові медіафайли та порожню директорію поста після публікації або відхилення."""
        parent_dirs: set[Path] = set()
        for raw_path in media_paths:
            path = Path(raw_path)
            parent_dirs.add(path.parent)
            try:
                if path.exists():
                    path.unlink()
                    logger.debug("Видалено тимчасовий файл: %s", path)
            except OSError as exc:
                logger.warning("Не вдалося видалити файл %s: %s", path, exc)

        for directory in parent_dirs:
            try:
                if directory.exists() and not any(directory.iterdir()):
                    directory.rmdir()
            except OSError:
                pass
