"""Модуль модерації постів через приватні повідомлення адміністратору з inline-кнопками."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from telethon import Button, TelegramClient, events

from app.publisher import Publisher
from app.rewriter import Rewriter
from app.storage import PostRecord, PostStatus, Storage

logger = logging.getLogger(__name__)


class ModerationManager:
    """Обробляє відправку чернеток адміну та натискання кнопок Опублікувати / Редагувати / Відхилити."""

    def __init__(
        self,
        bot_client: TelegramClient,
        storage: Storage,
        publisher: Publisher,
        rewriter: Rewriter,
        admin_user_id: int,
    ) -> None:
        self.bot_client = bot_client
        self.storage = storage
        self.publisher = publisher
        self.rewriter = rewriter
        self.admin_user_id = admin_user_id
        # Словник {admin_user_id: post_id} для стану очікування відредагованого тексту
        self._awaiting_edit: dict[int, int] = {}

    @staticmethod
    def _build_keyboard(post_id: int) -> list[list[Button]]:
        """Формує inline-клавіатуру для чернетки поста."""
        return [
            [
                Button.inline("✅ Опублікувати", data=f"pub:{post_id}".encode("utf-8")),
                Button.inline("✏️ Редагувати", data=f"edit:{post_id}".encode("utf-8")),
                Button.inline("❌ Відхилити", data=f"rej:{post_id}".encode("utf-8")),
            ]
        ]

    def register_handlers(self) -> None:
        """Реєструє обробники CallbackQuery та текстових повідомлень від адміністратора."""
        self.bot_client.add_event_handler(
            self._on_callback_query,
            events.CallbackQuery(chats=[self.admin_user_id]),
        )
        self.bot_client.add_event_handler(
            self._on_admin_message,
            events.NewMessage(chats=[self.admin_user_id], incoming=True),
        )
        logger.info("Обробники модерації зареєстровано для admin_user_id=%d", self.admin_user_id)

    async def send_for_review(self, post: PostRecord) -> None:
        """Надсилає чернетку поста адміністратору в особисті повідомлення."""
        await self.storage.update_status(post.id, PostStatus.PENDING_REVIEW)
        text = post.rewritten_text or post.original_text
        existing_media = [p for p in post.media_paths if Path(p).exists()]

        header = f"📝 <b>Чернетка поста #{post.id}</b> (source ID: <code>{post.source_msg_id}</code>)\n\n"
        keyboard = self._build_keyboard(post.id)

        try:
            if existing_media:
                # Якщо це альбом або одне фото/відео, спочатку надсилаємо прев'ю медіафайлів,
                # а другим повідомленням — текст з inline-кнопками (оскільки Telegram не дозволяє
                # прикріплювати inline-клавіатуру безпосередньо до медіа-альбомів).
                if len(existing_media) > 1:
                    await self.bot_client.send_file(
                        entity=self.admin_user_id,
                        file=existing_media,
                        caption=f"📎 Медіа-альбом до чернетки #{post.id} ({len(existing_media)} файлів)",
                    )
                    await self.bot_client.send_message(
                        entity=self.admin_user_id,
                        message=header + text,
                        parse_mode="html",
                        buttons=keyboard,
                        link_preview=False,
                    )
                else:
                    caption_text = header + text
                    if len(caption_text) <= 1024:
                        await self.bot_client.send_file(
                            entity=self.admin_user_id,
                            file=existing_media[0],
                            caption=caption_text,
                            parse_mode="html",
                            buttons=keyboard,
                        )
                    else:
                        await self.bot_client.send_file(
                            entity=self.admin_user_id,
                            file=existing_media[0],
                            caption=f"📎 Медіафайл до чернетки #{post.id}",
                        )
                        await self.bot_client.send_message(
                            entity=self.admin_user_id,
                            message=caption_text,
                            parse_mode="html",
                            buttons=keyboard,
                            link_preview=False,
                        )
            else:
                await self.bot_client.send_message(
                    entity=self.admin_user_id,
                    message=header + text,
                    parse_mode="html",
                    buttons=keyboard,
                    link_preview=False,
                )

            logger.info("Чернетку поста id=%d надіслано на модерацію адміну.", post.id)
        except Exception as exc:
            logger.exception("Не вдалося надіслати чернетку id=%d адміну: %s", post.id, exc)

    async def _on_callback_query(self, event: events.CallbackQuery.Event) -> None:
        """Обробляє натискання inline-кнопок адміністратором."""
        if event.sender_id != self.admin_user_id:
            await event.answer("Доступ заборонено.", alert=True)
            return

        data_str = event.data.decode("utf-8", errors="ignore")
        if ":" not in data_str:
            return

        action, post_id_str = data_str.split(":", maxsplit=1)
        if not post_id_str.isdigit():
            return
        post_id = int(post_id_str)

        post: Optional[PostRecord] = await self.storage.get_post(post_id)
        if post is None:
            await event.answer("Пост не знайдено в базі даних.", alert=True)
            return

        if action == "pub":
            self._awaiting_edit.pop(self.admin_user_id, None)
            if post.status == PostStatus.PUBLISHED:
                await event.answer("Цей пост уже опубліковано!", alert=True)
                return

            await self.publisher.enqueue(post.id)
            await event.answer("Додано в чергу на публікацію!")
            await event.edit(
                f"✅ <b>Пост #{post.id} схвалено та додано в чергу публікації.</b>\n\n"
                f"{post.rewritten_text or post.original_text}",
                parse_mode="html",
                buttons=None,
            )

        elif action == "edit":
            self._awaiting_edit[self.admin_user_id] = post.id
            await event.answer("Надішліть новий текст поста у відповідь.")
            await self.bot_client.send_message(
                entity=self.admin_user_id,
                message=(
                    f"✏️ <b>Режим редагування поста #{post.id}</b>\n"
                    f"Надішліть новий текст повідомленням (підтримується HTML-розмітка: "
                    f"<code>&lt;b&gt;</code>, <code>&lt;i&gt;</code>, <code>&lt;a href='...'&gt;</code>). "
                    f"Футер каналу буде додано автоматично, якщо його ще немає в тексті.\n"
                    f"Щоб скасувати редагування, натисніть кнопку нижче."
                ),
                parse_mode="html",
                buttons=[[Button.inline("Скасувати редагування", data=f"cancel_edit:{post.id}".encode("utf-8"))]],
            )

        elif action == "cancel_edit":
            self._awaiting_edit.pop(self.admin_user_id, None)
            await event.answer("Редагування скасовано.")
            await event.delete()

        elif action == "rej":
            self._awaiting_edit.pop(self.admin_user_id, None)
            await self.storage.update_status(post.id, PostStatus.SKIPPED)
            self.publisher.cleanup_media(post.media_paths)
            await event.answer("Пост відхилено.")
            await event.edit(
                f"❌ <b>Пост #{post.id} відхилено модератором.</b>",
                parse_mode="html",
                buttons=None,
            )
            logger.info("Пост id=%d відхилено адміністратором.", post.id)

    async def _on_admin_message(self, event: events.NewMessage.Event) -> None:
        """Приймає новий текст від адміністратора, якщо активний режим редагування."""
        if event.sender_id != self.admin_user_id:
            return

        # Ігноруємо власні або службові команди
        raw_text = (event.raw_text or "").strip()
        if not raw_text or raw_text.startswith("/"):
            return

        post_id = self._awaiting_edit.get(self.admin_user_id)
        if post_id is None:
            return

        post = await self.storage.get_post(post_id)
        if post is None:
            self._awaiting_edit.pop(self.admin_user_id, None)
            await event.reply("⚠️ Пост не знайдено в БД.")
            return

        # Отримуємо текст з HTML-форматуванням (якщо адмін форматував текст засобами клієнта Telegram)
        new_text = (event.message.text or raw_text).strip()
        has_media = len(post.media_paths) > 0
        if self.rewriter.footer_text and self.rewriter.footer_text not in new_text:
            formatted_text = self.rewriter.format_with_footer(new_text, has_media=has_media)
        else:
            formatted_text = new_text

        await self.storage.update_rewritten(
            post_id=post.id,
            rewritten_text=formatted_text,
            status=PostStatus.PENDING_REVIEW,
        )
        self._awaiting_edit.pop(self.admin_user_id, None)

        await self.bot_client.send_message(
            entity=self.admin_user_id,
            message=(
                f"✅ <b>Текст поста #{post.id} оновлено!</b>\n\n"
                f"{formatted_text}"
            ),
            parse_mode="html",
            buttons=self._build_keyboard(post.id),
            link_preview=False,
        )
        logger.info("Адміністратор оновив текст поста id=%d.", post.id)
