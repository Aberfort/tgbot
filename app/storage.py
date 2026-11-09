"""Модуль асинхронного сховища даних на базі SQLite (aiosqlite)."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

import aiosqlite

logger = logging.getLogger(__name__)


class PostStatus(str, Enum):
    """Статуси життєвого циклу поста."""

    NEW = "new"
    REWRITTEN = "rewritten"
    PENDING_REVIEW = "pending_review"
    QUEUED = "queued"
    PUBLISHED = "published"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class PostRecord:
    """Представлення запису поста з таблиці posts."""

    id: int
    source_msg_id: int
    grouped_id: Optional[int]
    status: PostStatus
    original_text: str
    rewritten_text: Optional[str]
    text_hash: str
    media_paths: list[str]
    created_at: str
    published_at: Optional[str]


class Storage:
    """Асинхронний менеджер SQLite для збереження постів та контролю дедуплікації й лімітів."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._conn: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        """Відкриває з'єднання з базою даних та ініціалізує схему таблиці posts."""
        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL;")
        await self._conn.execute("PRAGMA synchronous=NORMAL;")
        await self._init_schema()
        logger.info("Підключено до SQLite БД: %s", self.db_path)

    async def close(self) -> None:
        """Коректно закриває з'єднання з базою даних."""
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
            logger.info("З'єднання з SQLite закрито.")

    @property
    def conn(self) -> aiosqlite.Connection:
        """Повертає активне з'єднання або підіймає помилку, якщо БД не ініціалізована."""
        if self._conn is None:
            raise RuntimeError("Сховище не підключено. Спочатку викличте await storage.connect().")
        return self._conn

    async def _init_schema(self) -> None:
        """Створює таблицю posts та індекси, якщо вони ще не існують."""
        await self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_msg_id INTEGER NOT NULL UNIQUE,
                grouped_id INTEGER,
                status TEXT NOT NULL,
                original_text TEXT NOT NULL,
                rewritten_text TEXT,
                hash TEXT NOT NULL,
                media_paths TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                published_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_posts_hash ON posts(hash);
            CREATE INDEX IF NOT EXISTS idx_posts_grouped_id ON posts(grouped_id);
            CREATE INDEX IF NOT EXISTS idx_posts_status ON posts(status);
            CREATE INDEX IF NOT EXISTS idx_posts_published_at ON posts(published_at);
            """
        )
        await self.conn.commit()

    async def clear_stale_queued_posts(self, max_age_minutes: int = 15) -> int:
        """Скасовує застарілі пости в черзі (QUEUED/REWRITTEN), щоб старі пости не публікувались із запізненням."""
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)).isoformat()
        cursor = await self.conn.execute(
            """
            UPDATE posts
            SET status = ?
            WHERE status IN (?, ?) AND (created_at < ? OR source_msg_id < 276349)
            """,
            (
                PostStatus.SKIPPED.value,
                PostStatus.QUEUED.value,
                PostStatus.REWRITTEN.value,
                cutoff,
            ),
        )
        await self.conn.commit()
        return cursor.rowcount

    async def is_duplicate(
        self,
        source_msg_id: int,
        text_hash: str,
        grouped_id: Optional[int] = None,
    ) -> bool:
        """Перевіряє, чи оброблявся вже пост за source_msg_id, grouped_id або хешем тексту."""
        query = "SELECT status, hash FROM posts WHERE source_msg_id = ?"
        params: list = [source_msg_id]
        if grouped_id is not None:
            query += " OR grouped_id = ?"
            params.append(grouped_id)
        if text_hash and not text_hash.startswith("skipped_"):
            query += " OR (hash = ? AND hash != '')"
            params.append(text_hash)
        query += " LIMIT 1"

        cursor = await self.conn.execute(query, tuple(params))
        row = await cursor.fetchone()
        if row is None:
            return False

        status, existing_hash = row[0], row[1]
        # Якщо пост раніше було пропущено (SKIPPED), а тепер для нього є валідний текст:
        # це не дублікат — старий запис видаляється, щоб пост пішов на повноцінну обробку!
        if status == PostStatus.SKIPPED.value and text_hash and not text_hash.startswith("skipped_"):
            await self.conn.execute(
                "DELETE FROM posts WHERE source_msg_id = ?",
                (source_msg_id,),
            )
            if grouped_id is not None:
                await self.conn.execute(
                    "DELETE FROM posts WHERE grouped_id = ?",
                    (grouped_id,),
                )
            await self.conn.commit()
            return False

        return True

    async def create_post(
        self,
        source_msg_id: int,
        original_text: str,
        text_hash: str,
        status: PostStatus = PostStatus.NEW,
        grouped_id: Optional[int] = None,
        media_paths: Optional[list[str]] = None,
    ) -> int:
        """Створює або оновлює запис у таблиці posts і повертає його внутрішній ID."""
        now_iso = datetime.now(timezone.utc).isoformat()
        media_json = json.dumps(media_paths or [], ensure_ascii=False)
        cursor = await self.conn.execute(
            """
            INSERT OR REPLACE INTO posts (
                source_msg_id, grouped_id, status, original_text,
                rewritten_text, hash, media_paths, created_at, published_at
            )
            VALUES (?, ?, ?, ?, NULL, ?, ?, ?, NULL)
            """,
            (
                source_msg_id,
                grouped_id,
                status.value,
                original_text,
                text_hash,
                media_json,
                now_iso,
            ),
        )
        await self.conn.commit()
        return int(cursor.lastrowid)

    async def update_rewritten(
        self,
        post_id: int,
        rewritten_text: str,
        status: PostStatus = PostStatus.REWRITTEN,
    ) -> None:
        """Оновлює переписаний текст та статус поста."""
        await self.conn.execute(
            "UPDATE posts SET rewritten_text = ?, status = ? WHERE id = ?",
            (rewritten_text, status.value, post_id),
        )
        await self.conn.commit()

    async def update_media_paths(self, post_id: int, media_paths: list[str]) -> None:
        """Оновлює список шляхів до завантажених медіафайлів поста."""
        await self.conn.execute(
            "UPDATE posts SET media_paths = ? WHERE id = ?",
            (json.dumps(media_paths, ensure_ascii=False), post_id),
        )
        await self.conn.commit()

    async def update_status(
        self,
        post_id: int,
        status: PostStatus,
        published: bool = False,
    ) -> None:
        """Оновлює статус поста та (опційно) час фактичної публікації."""
        if published:
            now_iso = datetime.now(timezone.utc).isoformat()
            await self.conn.execute(
                "UPDATE posts SET status = ?, published_at = ? WHERE id = ?",
                (status.value, now_iso, post_id),
            )
        else:
            await self.conn.execute(
                "UPDATE posts SET status = ? WHERE id = ?",
                (status.value, post_id),
            )
        await self.conn.commit()

    async def get_post(self, post_id: int) -> Optional[PostRecord]:
        """Отримує запис поста за його первинним ключем id."""
        cursor = await self.conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,))
        row = await cursor.fetchone()
        if row is None:
            return None
        return self._row_to_record(row)

    async def get_post_by_source_id(self, source_msg_id: int) -> Optional[PostRecord]:
        """Отримує запис поста за його source_msg_id."""
        cursor = await self.conn.execute("SELECT * FROM posts WHERE source_msg_id = ?", (source_msg_id,))
        row = await cursor.fetchone()
        if row is None:
            return None
        return self._row_to_record(row)

    async def get_published_count_last_hour(self) -> int:
        """Повертає кількість опублікованих постів за останні 60 хвилин."""
        one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        cursor = await self.conn.execute(
            """
            SELECT COUNT(*) AS cnt
            FROM posts
            WHERE status = ? AND published_at IS NOT NULL AND published_at >= ?
            """,
            (PostStatus.PUBLISHED.value, one_hour_ago),
        )
        row = await cursor.fetchone()
        return int(row["cnt"]) if row else 0

    async def get_queued_post_ids(self, include_rewritten: bool = False) -> list[int]:
        """Повертає список ID постів, які очікували публікації до перезапуску бота."""
        statuses = [PostStatus.QUEUED.value]
        if include_rewritten:
            statuses.append(PostStatus.REWRITTEN.value)
        placeholders = ",".join("?" for _ in statuses)
        cursor = await self.conn.execute(
            f"SELECT id FROM posts WHERE status IN ({placeholders}) ORDER BY id ASC",
            tuple(statuses),
        )
        rows = await cursor.fetchall()
        return [int(r["id"]) for r in rows]

    async def get_active_post_ids(self) -> set[int]:
        """Повертає множину ID активних постів, чиї медіафайли ще потрібні."""
        active_statuses = (
            PostStatus.NEW.value,
            PostStatus.REWRITTEN.value,
            PostStatus.PENDING_REVIEW.value,
            PostStatus.QUEUED.value,
        )
        cursor = await self.conn.execute(
            "SELECT id FROM posts WHERE status IN (?, ?, ?, ?)",
            active_statuses,
        )
        rows = await cursor.fetchall()
        return {int(r["id"]) for r in rows}

    @staticmethod
    def _row_to_record(row: aiosqlite.Row) -> PostRecord:
        """Конвертує рядок SQLite у датаклас PostRecord."""
        try:
            media_paths = json.loads(row["media_paths"]) if row["media_paths"] else []
        except json.JSONDecodeError:
            media_paths = []
        try:
            status = PostStatus(row["status"])
        except ValueError:
            status = PostStatus.NEW
        return PostRecord(
            id=int(row["id"]),
            source_msg_id=int(row["source_msg_id"]),
            grouped_id=int(row["grouped_id"]) if row["grouped_id"] is not None else None,
            status=status,
            original_text=str(row["original_text"]),
            rewritten_text=str(row["rewritten_text"]) if row["rewritten_text"] is not None else None,
            text_hash=str(row["hash"]),
            media_paths=media_paths,
            created_at=str(row["created_at"]),
            published_at=str(row["published_at"]) if row["published_at"] is not None else None,
        )
