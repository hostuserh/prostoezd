"""SQLite-буфер снапшотов занятости на время недоступности backend.

Зачем SQLite, а не список в памяти: целевой сервер слабый (4 ГБ RAM / 4 ядра),
а CV-сервис перезапускается чаще, чем хотелось бы. SQLite переживает рестарт,
не держит снапшоты в RAM и не тянет внешних зависимостей (модуль stdlib).

Инварианты:

* дозаливка идёт строго в порядке ``timestamp`` кадра (ASC), при равенстве —
  в порядке попадания в буфер (``id`` ASC);
* глубина буфера ограничена (``max_items``), при переполнении вытесняются
  САМЫЕ СТАРЫЕ снапшоты — свежие статусы ценнее протухших;
* payload хранится как JSON-строка ровно в том виде, в каком уйдёт в сеть,
  поэтому дозаливка байт-в-байт повторяет исходную отправку (важно для
  дедупликации по ``frame_id`` на стороне backend).
"""

from __future__ import annotations

import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import timezone
from pathlib import Path

from cvsvc.publish.schemas import OccupancyPayload

__all__ = ["BufferedSnapshot", "SnapshotBuffer"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    sort_ts    TEXT NOT NULL,   -- timestamp кадра в UTC ISO-8601, для ORDER BY
    camera_id  TEXT NOT NULL,
    frame_id   TEXT,
    body       TEXT NOT NULL,   -- JSON payload как есть
    attempts   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_snapshots_order ON snapshots (sort_ts, id);

CREATE TABLE IF NOT EXISTS dlq (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    sort_ts     TEXT NOT NULL,
    camera_id   TEXT NOT NULL,
    frame_id    TEXT,
    body        TEXT NOT NULL,
    status_code INTEGER,
    reason      TEXT
);
"""


@dataclass(slots=True)
class BufferedSnapshot:
    """Строка буфера, готовая к повторной отправке."""

    row_id: int
    camera_id: str
    frame_id: str | None
    body: str
    attempts: int


class SnapshotBuffer:
    """Персистентная FIFO-очередь снапшотов с упорядочиванием по времени кадра."""

    def __init__(
        self,
        path: str | os.PathLike[str] = "buffer.db",
        max_items: int = 2000,
        dlq_max_items: int = 500,
    ) -> None:
        if max_items < 1:
            raise ValueError("max_items должен быть >= 1")
        self.path = str(path)
        self.max_items = max_items
        self.dlq_max_items = dlq_max_items
        self.last_evicted = 0
        self._lock = threading.Lock()
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # WAL: устойчивее к обрыву процесса, меньше fsync-ов на слабом диске.
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # -- запись ------------------------------------------------------------ #

    def append(self, payload: OccupancyPayload, body: str | None = None) -> int:
        """Положить снапшот в буфер. Возвращает ``row_id`` вставленной записи.

        Число вытесненных по переполнению записей доступно в ``last_evicted``.
        """
        body = body if body is not None else payload.model_dump_json(exclude_none=False)
        sort_ts = payload.timestamp.astimezone(timezone.utc).isoformat()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO snapshots (sort_ts, camera_id, frame_id, body) "
                "VALUES (?, ?, ?, ?)",
                (sort_ts, payload.camera_id, payload.frame_id, body),
            )
            row_id = int(cur.lastrowid or 0)
            self.last_evicted = self._evict_locked()
            self._conn.commit()
        return row_id

    def exists(self, row_id: int) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM snapshots WHERE id = ?", (row_id,)
            ).fetchone()
        return row is not None

    def append_dlq(
        self,
        payload: OccupancyPayload,
        body: str,
        status_code: int | None,
        reason: str,
    ) -> None:
        """Отправить неретраибельный снапшот (4xx) в dead-letter-очередь."""
        sort_ts = payload.timestamp.astimezone(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                "INSERT INTO dlq (sort_ts, camera_id, frame_id, body, status_code, "
                "reason) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    sort_ts,
                    payload.camera_id,
                    payload.frame_id,
                    body,
                    status_code,
                    reason,
                ),
            )
            self._conn.execute(
                "DELETE FROM dlq WHERE id NOT IN "
                "(SELECT id FROM dlq ORDER BY id DESC LIMIT ?)",
                (self.dlq_max_items,),
            )
            self._conn.commit()

    def _evict_locked(self) -> int:
        cur = self._conn.execute(
            "DELETE FROM snapshots WHERE id NOT IN ("
            "  SELECT id FROM snapshots ORDER BY sort_ts DESC, id DESC LIMIT ?"
            ")",
            (self.max_items,),
        )
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

    # -- чтение ------------------------------------------------------------ #

    def peek(self, limit: int = 50) -> list[BufferedSnapshot]:
        """Самые старые (по времени кадра) снапшоты, не удаляя их."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, camera_id, frame_id, body, attempts FROM snapshots "
                "ORDER BY sort_ts ASC, id ASC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            BufferedSnapshot(
                row_id=r["id"],
                camera_id=r["camera_id"],
                frame_id=r["frame_id"],
                body=r["body"],
                attempts=r["attempts"],
            )
            for r in rows
        ]

    def drop(self, row_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM snapshots WHERE id = ?", (row_id,))
            self._conn.commit()

    def bump_attempts(self, row_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE snapshots SET attempts = attempts + 1 WHERE id = ?", (row_id,)
            )
            self._conn.commit()

    def move_to_dlq(self, row_id: int, status_code: int | None, reason: str) -> None:
        """Перенести строку из буфера в DLQ (неретраибельная ошибка при дозаливке)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT sort_ts, camera_id, frame_id, body FROM snapshots "
                "WHERE id = ?",
                (row_id,),
            ).fetchone()
            if row is not None:
                self._conn.execute(
                    "INSERT INTO dlq (sort_ts, camera_id, frame_id, body, "
                    "status_code, reason) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        row["sort_ts"],
                        row["camera_id"],
                        row["frame_id"],
                        row["body"],
                        status_code,
                        reason,
                    ),
                )
                self._conn.execute("DELETE FROM snapshots WHERE id = ?", (row_id,))
                self._conn.commit()

    def count(self) -> int:
        with self._lock:
            return int(
                self._conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
            )

    def dlq_count(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM dlq").fetchone()[0])

    def clear(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM snapshots")
            self._conn.execute("DELETE FROM dlq")
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> SnapshotBuffer:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
