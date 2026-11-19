"""Модуль автоматичного SMM-просування, SEO-оптимізації каналу, вечірніх опитувань та TikTok/Reels генератора."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import random
import re
import shutil
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from google.genai import types as genai_types
from telethon import Button, TelegramClient, events
from telethon.tl import functions, types

from app.config import Settings
from app.rewriter import Rewriter
from app.storage import Storage
from app.video_maker import (
    build_overlay_png,
    render_vertical_from_photos,
    render_vertical_from_video,
)

logger = logging.getLogger(__name__)

SEO_CHANNEL_TITLE = "BTS Daily Hub 💜 | БТС Новости K-POP"
SEO_CHANNEL_BIO = (
    "💜 Главный хаб новостей BTS и K-POP!\n"
    "⚡️ Эксклюзивы, переводы, фото, рекорды и обновления мемберов каждый день.\n"
    "✨ Тэхён • Чонгук • Чимин • Джин • Намджун • Юнги • Джей-Хоуп"
)
RECOMMENDED_REACTIONS = ["💜", "🔥", "😍", "😭", "🤯", "👑", "👍"]

FIRST_COMMENT_TEMPLATES = [
    "💜 Что думаете, АРМИ? Делитесь эмоциями в комментариях! 👇\nПодписывайтесь на наш канал: @btsdailyhub ✨",
    "🔥 Как вам эта новость? Пишите своё мнение в комментариях! 👇\nГлавные новости BTS каждый день — @btsdailyhub 💜",
    "✨ АРМИ, ждём ваши реакции в комментариях! 👇💜\nНе забудьте подписаться на @btsdailyhub, чтобы не пропустить обновления!",
    "😍 Делитесь впечатлениями в комментариях! 👇\nЕщё больше эксклюзивов о BTS в нашем канале @btsdailyhub 💜",
]


class SMMManager:
    """Автоматизує SMM-упаковку каналу, інтерактивні опитування, перший коментар та генерацію TikTok-сценаріїв."""

    def __init__(
        self,
        user_client: TelegramClient,
        bot_client: TelegramClient,
        settings: Settings,
        storage: Storage,
        rewriter: Rewriter,
    ) -> None:
        self.user_client = user_client
        self.bot_client = bot_client
        self.settings = settings
        self.storage = storage
        self.rewriter = rewriter
        self._poll_task: Optional[asyncio.Task[None]] = None
        self._stopping = False
        self._last_poll_date: Optional[str] = None
        self._linked_discussion_id: Optional[int] = None
        self._comment_lock = asyncio.Lock()
        self._commented_grouped_ids: set[int] = set()
        self._commented_msg_ids: set[int] = set()
        self._video_lock = asyncio.Lock()
        self._rendered_video_keys: set[str] = set()

    @staticmethod
    def _smm_menu_keyboard() -> list[list[Button]]:
        """Клавіатура SMM-панелі для адміністратора."""
        return [
            [
                Button.inline("🎥 Змонтувати TikTok-відео", data=b"smm:video"),
            ],
            [
                Button.inline("🎬 Сценарій TikTok / Reels", data=b"smm:tiktok"),
                Button.inline("📊 Запустити опитування", data=b"smm:poll"),
            ],
            [
                Button.inline("📈 Статистика бота", data=b"smm:stats"),
            ],
        ]

    async def start(self) -> None:
        """Виконує SEO-оптимізацію профілю каналу, реєструє команди адміна та запускає планувальник опитувань."""
        await self.optimize_channel_profile()
        self._register_handlers()

        if self.settings.daily_poll_enabled and (self._poll_task is None or self._poll_task.done()):
            self._stopping = False
            self._poll_task = asyncio.create_task(self._daily_poll_loop(), name="smm_daily_poll")
            logger.info(
                "Планувальник вечірніх опитувань запущено (час: %02d:00 UTC).",
                self.settings.daily_poll_hour_utc,
            )

    async def stop(self) -> None:
        """Зупиняє фоновий планувальник опитувань."""
        self._stopping = True
        if self._poll_task is not None and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass

    async def optimize_channel_profile(self) -> None:
        """Автоматично налаштовує SEO-назву, опис (Bio), реакції та перевіряє групу обговорень цільового каналу."""
        try:
            channel_entity = await self.user_client.get_entity(self.settings.target_channel)
            full_channel = await self.user_client(
                functions.channels.GetFullChannelRequest(channel=channel_entity)
            )
            current_title = getattr(channel_entity, "title", "") or ""
            current_about = getattr(full_channel.full_chat, "about", "") or ""
            linked_chat_id = getattr(full_channel.full_chat, "linked_chat_id", None)

            if linked_chat_id:
                self._linked_discussion_id = int(linked_chat_id)
                logger.info("Виявлено прив'язану групу обговорень (коментарі): id=%d", self._linked_discussion_id)

            # 1. SEO-оптимізація назви каналу для глобального пошуку Telegram
            if "БТС" not in current_title.upper() and "K-POP" not in current_title.upper():
                try:
                    await self.user_client(
                        functions.channels.EditTitleRequest(
                            channel=channel_entity,
                            title=SEO_CHANNEL_TITLE,
                        )
                    )
                    logger.info("Назву каналу оновлено для Telegram SEO: %s", SEO_CHANNEL_TITLE)
                except Exception as title_err:
                    logger.debug("Не вдалося оновити назву каналу: %s", title_err)

            # 2. SEO-оптимізація опису (Bio) каналу
            if not current_about.strip() or ("Тэхён" not in current_about and "Чонгук" not in current_about):
                try:
                    await self.user_client(
                        functions.messages.EditChatAboutRequest(
                            peer=channel_entity,
                            about=SEO_CHANNEL_BIO,
                        )
                    )
                    logger.info("Опис (Bio) каналу оновлено з SEO-ключами учасників BTS.")
                except Exception as bio_err:
                    logger.debug("Не вдалося оновити Bio каналу: %s", bio_err)

            # 3. Увімкнення тематичних реакцій (💜, 🔥, 😍, 😭, 🤯, 👑, 👍)
            try:
                reaction_objs = [types.ReactionEmoji(emoticon=emo) for emo in RECOMMENDED_REACTIONS]
                await self.user_client(
                    functions.messages.SetChatAvailableReactionsRequest(
                        peer=channel_entity,
                        available_reactions=types.ChatReactionsSome(reactions=reaction_objs),
                    )
                )
                logger.info("Тематичні реакції каналу налаштовано: %s", " ".join(RECOMMENDED_REACTIONS))
            except Exception as react_err:
                logger.debug("Не вдалося оновити список реакцій каналу: %s", react_err)

        except Exception as exc:
            logger.warning("Пропущено авто-оптимізацію профілю каналу: %s", exc)

    def _register_handlers(self) -> None:
        """Реєструє команди SMM-панелі для адміна та авто-перший коментар у групі обговорень."""
        self.bot_client.add_event_handler(
            self._on_admin_command,
            events.NewMessage(chats=[self.settings.admin_user_id], incoming=True),
        )
        self.bot_client.add_event_handler(
            self._on_smm_callback,
            events.CallbackQuery(chats=[self.settings.admin_user_id]),
        )

        # Якщо до каналу прив'язана група коментарів, підписуємося на авто-переслані пости
        if self._linked_discussion_id:
            self.user_client.add_event_handler(
                self._on_discussion_forwarded_post,
                events.NewMessage(chats=[self._linked_discussion_id]),
            )
            logger.info("Активовано авто-перший коментар у групі обговорень id=%d", self._linked_discussion_id)

    async def _on_discussion_forwarded_post(self, event: events.NewMessage.Event) -> None:
        """Автоматично залишає перший залучаючий коментар під кожним новим постом у групі обговорень (1 коментар на весь альбом)."""
        msg = event.message
        if not msg or not getattr(msg, "fwd_from", None):
            return
        # Перевіряємо, що це автоматичний репост із каналу в групу обговорень
        if not getattr(msg.fwd_from, "saved_from_peer", None) and not getattr(msg, "post", False):
            return

        grouped_id = getattr(msg, "grouped_id", None)
        async with self._comment_lock:
            if msg.id in self._commented_msg_ids:
                return
            if grouped_id is not None:
                if grouped_id in self._commented_grouped_ids:
                    logger.debug(
                        "Пропускаємо повторний коментар для медіа-альбому grouped_id=%s (msg_id=%d).",
                        grouped_id,
                        msg.id,
                    )
                    return
                self._commented_grouped_ids.add(int(grouped_id))
            self._commented_msg_ids.add(int(msg.id))

            # Обмежуємо розмір кешу в пам'яті
            if len(self._commented_grouped_ids) > 500:
                self._commented_grouped_ids.clear()
            if len(self._commented_msg_ids) > 500:
                self._commented_msg_ids.clear()

        try:
            await asyncio.sleep(3)
            comment_text = random.choice(FIRST_COMMENT_TEMPLATES)
            await self.user_client.send_message(
                entity=event.chat_id,
                message=comment_text,
                reply_to=msg.id,
            )
            logger.info(
                "Додано авто-перший коментар у групі обговорень до msg_id=%d (grouped_id=%s).",
                msg.id,
                grouped_id,
            )
        except Exception as exc:
            logger.debug("Не вдалося залишити перший коментар: %s", exc)

    async def _on_admin_command(self, event: events.NewMessage.Event) -> None:
        """Обробляє команди /start, /smm, /video, /tiktok, /poll, /stats від адміністратора."""
        if event.sender_id != self.settings.admin_user_id:
            return
        raw = (event.raw_text or "").strip().lower()
        if not raw.startswith("/"):
            return

        cmd = raw.split()[0]
        if cmd in ("/start", "/smm", "/help"):
            await self._send_smm_dashboard()
        elif cmd == "/video":
            await self._handle_render_tiktok_video()
        elif cmd == "/tiktok":
            await self._handle_generate_tiktok()
        elif cmd == "/poll":
            await self._handle_trigger_poll()
        elif cmd == "/stats":
            await self._handle_send_stats()

    async def _on_smm_callback(self, event: events.CallbackQuery.Event) -> None:
        """Обробляє натискання inline-кнопок SMM-панелі."""
        if event.sender_id != self.settings.admin_user_id:
            return
        data_str = event.data.decode("utf-8", errors="ignore")
        if not data_str.startswith("smm:"):
            return

        action = data_str.split(":", maxsplit=1)[1]
        if action == "video":
            await event.answer("Запускаю авто-монтаж вертикального відео...")
            await self._handle_render_tiktok_video()
        elif action == "tiktok":
            await event.answer("Генерую вірусний сценарій для TikTok / Reels...")
            await self._handle_generate_tiktok()
        elif action == "poll":
            await event.answer("Створюю та публікую інтерактивне опитування в канал...")
            await self._handle_trigger_poll()
        elif action == "stats":
            await event.answer("Збираю статистику...")
            await self._handle_send_stats()

    async def _send_smm_dashboard(self) -> None:
        """Надсилає адміну головне меню SMM-помічника."""
        text = (
            "🚀 <b>SMM-Панель управління BTS Daily Hub 💜</b>\n\n"
            "Автоматичне просування активне:\n"
            "• <b>SEO-назва та опис:</b> оптимізовано під пошук Telegram\n"
            "• <b>Вірусний підпис:</b> клікабельне посилання в кожному пості\n"
            "• <b>Вечірній інтерактив:</b> щодня о 19:00 (за Києвом)\n"
            "• <b>Ліміти:</b> до 4 постів/год, інтервал 5 хв, TTL 45 хв\n\n"
            "Оберіть дію нижче або використовуйте команди "
            "<code>/video</code>, <code>/tiktok</code>, <code>/poll</code>, <code>/stats</code>:"
        )
        await self.bot_client.send_message(
            entity=self.settings.admin_user_id,
            message=text,
            parse_mode="html",
            buttons=self._smm_menu_keyboard(),
        )

    async def _handle_render_tiktok_video(self) -> None:
        """Бере свіжий пост із медіа в каналі, генерує хук через Gemini, монтує 9:16 .mp4 і надсилає адміну."""
        if self._video_lock.locked():
            await self.bot_client.send_message(
                entity=self.settings.admin_user_id,
                message="⏳ Вже монтується попереднє відео. Зачекайте кілька секунд...",
            )
            return

        async with self._video_lock:
            status_msg = await self.bot_client.send_message(
                entity=self.settings.admin_user_id,
                message="🎥 <b>Підбираю свіжий пост із медіа та монтую TikTok-відео (9:16)...</b>\nЗачекайте ~10–25 сек.",
                parse_mode="html",
            )

            work_dir = self.settings.media_tmp_dir / f"tiktok_{int(datetime.now(timezone.utc).timestamp())}"
            work_dir.mkdir(parents=True, exist_ok=True)
            try:
                messages = await self.user_client.get_messages(self.settings.target_channel, limit=25)
                if not messages:
                    await self.bot_client.send_message(
                        entity=self.settings.admin_user_id,
                        message="⚠️ У цільовому каналі поки немає постів для монтажу.",
                    )
                    return

                # Групуємо повідомлення у пости (зберігаючи порядок від найновіших до старіших)
                grouped_order: list[str] = []
                grouped_map: dict[str, list] = {}
                for m in messages:
                    if not m or (not getattr(m, "photo", None) and not getattr(m, "video", None)):
                        continue
                    key = f"g:{m.grouped_id}" if getattr(m, "grouped_id", None) else f"m:{m.id}"
                    if key not in grouped_map:
                        grouped_order.append(key)
                        grouped_map[key] = []
                    grouped_map[key].append(m)

                if not grouped_order:
                    await self.bot_client.send_message(
                        entity=self.settings.admin_user_id,
                        message="⚠️ Серед останніх постів каналу не знайдено фото або відео для монтажу.",
                    )
                    return

                # Обираємо найсвіжіший пост, для якого ще не рендерили відео (щоб повторний /video брав наступний)
                chosen_key = next((k for k in grouped_order if k not in self._rendered_video_keys), None)
                if chosen_key is None:
                    self._rendered_video_keys.clear()
                    chosen_key = grouped_order[0]
                self._rendered_video_keys.add(chosen_key)

                post_msgs = sorted(grouped_map[chosen_key], key=lambda m: m.id)
                post_text = "\n".join(
                    (m.raw_text or "").strip() for m in post_msgs if (m.raw_text or "").strip()
                ).strip()
                # Прибираємо наш власний підпис каналу з контексту для LLM
                clean_post_text = re.sub(r"BTS Daily Hub\s*💜?", "", post_text).strip()

                # Завантажуємо медіафайли (пріоритет: відео, інакше до 4 фото)
                video_msg = next((m for m in post_msgs if getattr(m, "video", None)), None)
                downloaded_video: Optional[Path] = None
                downloaded_photos: list[Path] = []

                if video_msg is not None:
                    file_size = getattr(video_msg.file, "size", 0) or 0
                    if file_size <= self.settings.max_media_size_mb * 1024 * 1024:
                        dl = await self.user_client.download_media(video_msg, file=str(work_dir / "source_video.mp4"))
                        if dl and Path(dl).exists():
                            downloaded_video = Path(dl)

                if downloaded_video is None:
                    photo_msgs = [m for m in post_msgs if getattr(m, "photo", None)][:4]
                    for idx, pm in enumerate(photo_msgs):
                        dl = await self.user_client.download_media(pm, file=str(work_dir / f"photo_{idx}.jpg"))
                        if dl and Path(dl).exists():
                            downloaded_photos.append(Path(dl))

                if downloaded_video is None and not downloaded_photos:
                    await self.bot_client.send_message(
                        entity=self.settings.admin_user_id,
                        message="⚠️ Не вдалося завантажити медіафайли поста (можливо, відео перевищує ліміт розміру).",
                    )
                    return

                # Генеруємо через Gemini хук на екран + готовий опис для TikTok/Reels російською
                channel_handle = (
                    f"@{str(self.settings.target_channel).lstrip('@')}"
                    if not isinstance(self.settings.target_channel, int)
                    else "@btsdailyhub"
                )
                hook, sub_cta, tiktok_caption = await self._generate_tiktok_video_texts(
                    clean_post_text,
                    channel_handle=channel_handle,
                )

                # Створюємо оверлей та монтуємо 9:16 .mp4
                overlay_path = work_dir / "overlay.png"
                await asyncio.to_thread(
                    build_overlay_png,
                    overlay_path,
                    hook,
                    sub_cta,
                    channel_handle,
                )

                output_mp4 = work_dir / "tiktok_ready.mp4"
                if downloaded_video is not None:
                    await render_vertical_from_video(
                        video_path=downloaded_video,
                        overlay_path=overlay_path,
                        output_path=output_mp4,
                        max_duration=15,
                    )
                else:
                    await render_vertical_from_photos(
                        photo_paths=downloaded_photos,
                        overlay_path=overlay_path,
                        output_path=output_mp4,
                        work_dir=work_dir,
                    )

                if not output_mp4.exists():
                    raise RuntimeError("Підсумковий файл відео не створено.")

                # Відправляємо готове відео та опис для копіювання
                await self.bot_client.send_file(
                    entity=self.settings.admin_user_id,
                    file=str(output_mp4),
                    caption=f"✅ <b>Готове вертикальне відео (9:16) для TikTok / Reels / Shorts!</b>\nЗаголовок на відео: <i>{html.escape(hook)}</i>",
                    parse_mode="html",
                    supports_streaming=True,
                )
                await self.bot_client.send_message(
                    entity=self.settings.admin_user_id,
                    message=(
                        "📝 <b>Готовий опис для TikTok / Reels (натисніть, щоб скопіювати):</b>\n\n"
                        f"<code>{html.escape(tiktok_caption)}</code>\n\n"
                        "💡 <i>Порада: збережіть відео на телефон, додайте трендовий звук BTS прямо в TikTok і публікуйте!</i>"
                    ),
                    parse_mode="html",
                    buttons=self._smm_menu_keyboard(),
                )
                try:
                    await status_msg.delete()
                except Exception:
                    pass
                logger.info("Змонтовано та надіслано адміну TikTok-відео для поста %s.", chosen_key)

            except Exception as exc:
                logger.exception("Помилка авто-монтажу TikTok-відео: %s", exc)
                await self.bot_client.send_message(
                    entity=self.settings.admin_user_id,
                    message=f"⚠️ Не вдалося змонтувати відео: <code>{html.escape(str(exc))}</code>",
                    parse_mode="html",
                    buttons=self._smm_menu_keyboard(),
                )
            finally:
                shutil.rmtree(work_dir, ignore_errors=True)

    async def _generate_tiktok_video_texts(
        self,
        post_text: str,
        channel_handle: str,
    ) -> tuple[str, str, str]:
        """Генерує короткий заголовок-хук на екран, підзаголовок та опис із хештегами для TikTok російською."""
        prompt = (
            "Ты — топ-креатор вирусных TikTok/Reels видео про BTS и K-pop.\n"
            "На основе этой новости из нашего Telegram-канала сделай тексты для 10-15 секундного ролика на РУССКОМ ЯЗЫКЕ.\n\n"
            f"Новость:\n{post_text[:800] or 'Свежее обновление BTS!'}\n\n"
            "Верни СТРОГО валидный JSON-объект без Markdown-оберток:\n"
            "{\n"
            '  "hook": "Яркий короткий заголовок-хук на экран (4-8 слов, БЕЗ эмодзи, например: Тэхён снова побил рекорд в Корее!)",\n'
            '  "sub_cta": "Короткая интрига внизу экрана (до 45 символов, БЕЗ эмодзи, например: Все фото в 4K уже в нашем ТГ)",\n'
            f'  "caption": "Живое описание под видео с эмодзи, призывом перейти в ТГ {channel_handle} (ссылка в профиле / поиск в ТГ) и 8-10 релевантными хештегами (#bts #army #kpop #бтс ...)"\n'
            "}"
        )
        try:
            response = await self.rewriter.client.aio.models.generate_content(
                model=self.rewriter.model_name,
                contents=prompt,
                config=genai_types.GenerateContentConfig(temperature=0.75),
            )
            raw = (response.text or "").strip()
            raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
            data = json.loads(raw)
            hook = str(data.get("hook") or "").strip()
            sub_cta = str(data.get("sub_cta") or "").strip()
            caption = str(data.get("caption") or "").strip()
            if hook and caption:
                return hook, sub_cta or "Подробности и HD-фото уже в нашем ТГ", caption
        except Exception as err:
            logger.warning("Фолбек текстів TikTok-відео через помилку Gemini: %s", err)

        fallback_hook = "Срочное обновление BTS!"
        fallback_sub = "Все подробности и HD-фото уже в нашем ТГ"
        fallback_caption = (
            f"💜 Свежие новости BTS уже вышли! Все подробности, переводы и фото в высоком качестве "
            f"ищите в нашем Telegram-канале {channel_handle} (ссылка в шапке профиля) ✨\n\n"
            "#bts #army #kpop #бтс #тэхён #чонгук #чимин #корея #рек #fyp"
        )
        return fallback_hook, fallback_sub, fallback_caption

    async def _handle_send_stats(self) -> None:
        """Формує та відправляє статистику роботи бота адміністратору."""
        stats = await self.storage.get_stats_summary()
        pub_total = stats.get("published", 0)
        pub_24h = stats.get("published_24h", 0)
        queued = stats.get("queued", 0) + stats.get("rewritten", 0)
        expired = stats.get("expired", 0)
        skipped = stats.get("skipped", 0)

        msg = (
            "📈 <b>Статистика каналу BTS Daily Hub 💜</b>\n\n"
            f"✅ Опубліковано за 24 год: <b>{pub_24h}</b>\n"
            f"📚 Усього опубліковано: <b>{pub_total}</b>\n"
            f"⏳ Зараз у живій черзі: <b>{queued}</b> (макс. {self.settings.max_queued_posts})\n"
            f"🧹 Відсіяно старих/спаму (EXPIRED): <b>{expired}</b>\n"
            f"⏭ Пропущено фільтром (SKIPPED): <b>{skipped}</b>"
        )
        await self.bot_client.send_message(
            entity=self.settings.admin_user_id,
            message=msg,
            parse_mode="html",
            buttons=self._smm_menu_keyboard(),
        )

    async def _handle_generate_tiktok(self) -> None:
        """Генерує готовий сценарій для TikTok / Reels / Shorts на основі останніх постів каналу."""
        recent_posts = await self.storage.get_recent_published_posts(limit=3)
        if not recent_posts:
            await self.bot_client.send_message(
                entity=self.settings.admin_user_id,
                message="⚠️ Поки немає опублікованих постів для генерації TikTok-сценарію.",
            )
            return

        posts_context = "\n---\n".join(
            re.sub(r"<[^>]+>", "", (p.rewritten_text or p.original_text)) for p in recent_posts
        )
        prompt = (
            "Ты — топ-креатор вирусных роликов TikTok и Instagram Reels про BTS и K-pop.\n"
            "На основе этих свежих новостей нашего Telegram-канала выбери САМУЮ горячую новость "
            "и напиши готовый сценарий для 10-15 секундного ролика, который принесет максимум переходов в наш Telegram-канал @btsdailyhub.\n\n"
            f"Новости:\n{posts_context}\n\n"
            "Выдай ответ строго в таком формате (Telegram HTML, используй <b> и <code> для копирования):\n"
            "🎬 <b>Идея визуала (что показать на фоне):</b> ...\n\n"
            "💥 <b>Текст на экране (первые 3 секунды — кликбейт-хук):</b>\n<code>...</code>\n\n"
            "📝 <b>Описание под видео (с байтом перейти в ТГ по ссылке в шапке профиля):</b>\n<code>...</code>\n\n"
            "🏷 <b>Хештеги:</b>\n<code>#bts #army #kpop #бтс #тэхён #чонгук #чимин #корея #рек #fyp</code>"
        )
        try:
            response = await self.rewriter.client.aio.models.generate_content(
                model=self.rewriter.model_name,
                contents=prompt,
                config=genai_types.GenerateContentConfig(temperature=0.7),
            )
            script_text = (response.text or "").strip()
            await self.bot_client.send_message(
                entity=self.settings.admin_user_id,
                message=f"🔥 <b>Готовий сценарій для TikTok / Reels / Shorts:</b>\n\n{script_text}",
                parse_mode="html",
                buttons=self._smm_menu_keyboard(),
            )
        except Exception as exc:
            logger.error("Помилка генерації TikTok-сценарію: %s", exc)
            await self.bot_client.send_message(
                entity=self.settings.admin_user_id,
                message=f"⚠️ Не вдалося згенерувати сценарій: {exc}",
            )

    async def _handle_trigger_poll(self) -> None:
        """Запускає генерацію та публікацію опитування в канал за запитом адміна."""
        ok = await self.publish_interactive_poll()
        if ok:
            await self.bot_client.send_message(
                entity=self.settings.admin_user_id,
                message="✅ <b>Інтерактивне опитування успішно опубліковано в @btsdailyhub!</b>",
                parse_mode="html",
                buttons=self._smm_menu_keyboard(),
            )
        else:
            await self.bot_client.send_message(
                entity=self.settings.admin_user_id,
                message="⚠️ Не вдалося опублікувати опитування. Перевірте логи бота.",
            )

    async def _daily_poll_loop(self) -> None:
        """Щовечора у прайм-тайм публікує інтерактивне опитування в цільовий канал."""
        await asyncio.sleep(15)
        while not self._stopping:
            try:
                now_utc = datetime.now(timezone.utc)
                today_str = now_utc.strftime("%Y-%m-%d")
                if (
                    now_utc.hour == self.settings.daily_poll_hour_utc
                    and self._last_poll_date != today_str
                ):
                    self._last_poll_date = today_str
                    logger.info("Час вечірнього інтерактиву! Генеруємо та публікуємо опитування...")
                    await self.publish_interactive_poll()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("Помилка в циклі щоденного опитування: %s", exc)

            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                break

    async def publish_interactive_poll(self) -> bool:
        """Генерує через Gemini живий інтерактивний опрос для фанатів BTS і відправляє його в канал."""
        recent_posts = await self.storage.get_recent_published_posts(limit=3)
        recent_context = "\n".join(
            re.sub(r"<[^>]+>", "", (p.rewritten_text or p.original_text))[:200] for p in recent_posts
        )

        prompt = (
            "Ты — редактор фанатского Telegram-канала о BTS (BTS Daily Hub 💜).\n"
            "Придумай 1 яркий, эмоциональный вечерний опрос для АРМИ на РУССКОМ ЯЗЫКЕ, "
            "чтобы поднять вовлеченность канала.\n"
            "Темы могут быть: какой сольный релиз сейчас на репите, чей образ круче, любимая эра BTS, "
            "какое настроение сегодня с мемберами или реакция на свежие новости.\n"
            f"Свежие посты канала (для вдохновения, но необязательно):\n{recent_context}\n\n"
            "Верни СТРОГО валидный JSON-объект без Markdown-блоков:\n"
            '{"question": "Текст вопроса с эмодзи (до 200 символов)", '
            '"options": ["Вариант 1", "Вариант 2", "Вариант 3", "Вариант 4", "Вариант 5"]}\n'
            "Вариантов ответа должно быть от 4 до 6, каждый не длиннее 80 символов."
        )

        try:
            response = await self.rewriter.client.aio.models.generate_content(
                model=self.rewriter.model_name,
                contents=prompt,
                config=genai_types.GenerateContentConfig(temperature=0.8),
            )
            raw_json = (response.text or "").strip()
            raw_json = re.sub(r"^```(?:json)?|```$", "", raw_json, flags=re.MULTILINE).strip()
            data = json.loads(raw_json)
            question = str(data.get("question", "")).strip()[:255]
            options = [str(o).strip()[:95] for o in data.get("options", []) if str(o).strip()][:7]
            if not question or len(options) < 2:
                raise ValueError("Недостатньо варіантів у згенерованому опитуванні")
        except Exception as gen_err:
            logger.warning("Фолбек на базове опитування через помилку генерації (%s)", gen_err)
            question = "💜 Вечерний интерактив АРМИ! Чей сольный вайб вам ближе всего на этой неделе? ✨"
            options = [
                "Тэхён (V) — эстетика и джаз 🎷",
                "Чонгук — драйв и поп-хиты 🔥",
                "Чимин — нежность и хореография 🌙",
                "Юнги (Agust D) — мощный рэп 🖤",
                "Джин / Намджун / Джей-Хоуп ✨",
                "Слушаю групповые хиты BTS на репите! 💜",
            ]

        return await self._send_telegram_poll(question, options)

    async def _send_telegram_poll(self, question: str, options: list[str]) -> bool:
        """Відправляє нативне опитування в цільовий канал через Bot HTTP API."""
        bot_token = (
            self.settings.bot_token.get_secret_value()
            if hasattr(self.settings.bot_token, "get_secret_value")
            else str(self.settings.bot_token)
        )
        target = self.settings.target_channel
        chat_id = target if isinstance(target, int) else f"@{str(target).lstrip('@')}"

        url = f"https://api.telegram.org/bot{bot_token}/sendPoll"
        payload = json.dumps(
            {
                "chat_id": chat_id,
                "question": question,
                "options": [{"text": opt} for opt in options],
                "is_anonymous": True,
                "allows_multiple_answers": False,
            }
        ).encode("utf-8")

        def _post_sync() -> bool:
            req = urllib.request.Request(
                url,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                resp_data = json.loads(resp.read().decode("utf-8"))
                return bool(resp_data.get("ok"))

        try:
            ok = await asyncio.to_thread(_post_sync)
            if ok:
                logger.info("Інтерактивне опитування успішно опубліковано в %s: %s", chat_id, question)
            return ok
        except Exception as exc:
            logger.error("Не вдалося відправити опитування в %s: %s", chat_id, exc)
            return False

