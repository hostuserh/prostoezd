"""Синхронный клиент доставки снапшотов занятости в backend ParkVision.

Поведение (см. ``cv-service/docs/CV_BACKEND_CONTRACT.md``):

* ``POST {BACKEND_URL}/api/v1/internal/occupancy``,
  заголовок ``Authorization: Bearer {CV_SERVICE_TOKEN}``;
* каждый цикл уходит ПОЛНЫЙ снапшот камеры, а не дельта;
* таймаут 5 с, 3 попытки с экспоненциальным бэкоффом и джиттером
  на 5xx / 429 / сетевых ошибках;
* 4xx (кроме 429) не ретраится — снапшот уходит в DLQ;
* при недоступности backend снапшоты копятся в SQLite-буфере и дозаливаются
  строго в порядке ``timestamp`` кадра;
* суммарное время публикации ограничено бюджетом (по умолчанию FRAME_INTERVAL),
  чтобы доставка не задерживала синхронный CV-цикл;
* логи — json-строки, токен в них не попадает никогда.

Пример использования в CV-цикле::

    client = BackendClient()                      # читает env
    for camera in cameras:                        # камеры обрабатываются подряд
        payload = build_payload(camera, frame)
        result = client.publish(payload)
        if not result.ok:
            log.warning("snapshot buffered: %s", result.error)
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable

import httpx

from cvsvc.publish.buffer import SnapshotBuffer
from cvsvc.publish.schemas import (
    HealthResponse,
    OccupancyPayload,
    OccupancyResponse,
)

__all__ = [
    "BackendClient",
    "PublishOutcome",
    "PublishResult",
    "build_payload",
    "env_bool",
    "env_float",
    "env_int",
]

logger = logging.getLogger("cvsvc.publish")

DEFAULT_TIMEOUT_S = 5.0
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BUFFER_MAX = 2000
DEFAULT_BACKOFF_BASE_S = 0.5
DEFAULT_BACKOFF_CAP_S = 8.0
API_PREFIX = "/api/v1"
OCCUPANCY_PATH = "/internal/occupancy"


# --------------------------------------------------------------------------- #
# env helpers
# --------------------------------------------------------------------------- #


def env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning('{"event":"bad_env","name":"%s","fallback":%s}', name, default)
        return default


def env_int(name: str, default: int) -> int:
    return int(env_float(name, float(default)))


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _mask(secret: str | None) -> str:
    """Безопасное представление токена для логов: длина + хвост из 4 символов."""
    if not secret:
        return "<empty>"
    if len(secret) <= 4:
        return f"<len={len(secret)}>"
    return f"<len={len(secret)}:…{secret[-4:]}>"


# --------------------------------------------------------------------------- #
# Результат публикации
# --------------------------------------------------------------------------- #


class PublishOutcome(str, Enum):
    SENT = "sent"  # backend принял снапшот
    PARTIAL = "partial"  # принял, но часть spot_id отклонил
    DUPLICATE = "duplicate"  # backend распознал повтор по frame_id
    BUFFERED = "buffered"  # не доставлен, лежит в буфере, будет дозалит
    DROPPED = "dropped"  # неретраибельная ошибка (4xx), ушёл в DLQ


@dataclass(slots=True)
class PublishResult:
    outcome: PublishOutcome
    attempts: int = 0
    status_code: int | None = None
    response: OccupancyResponse | None = None
    rejected_spot_ids: list[str] = field(default_factory=list)
    error: str | None = None
    buffer_size: int = 0
    flushed: int = 0

    @property
    def ok(self) -> bool:
        """Снапшот доставлен (целиком, частично или признан дублем)."""
        return self.outcome in (
            PublishOutcome.SENT,
            PublishOutcome.PARTIAL,
            PublishOutcome.DUPLICATE,
        )


@dataclass(slots=True)
class _Attempt:
    ok: bool
    attempts: int
    status_code: int | None = None
    body: str | None = None
    retriable: bool = False
    error: str | None = None
    budget_exhausted: bool = False


# --------------------------------------------------------------------------- #
# Клиент
# --------------------------------------------------------------------------- #


class BackendClient:
    """Синхронный клиент ``POST /internal/occupancy`` с буфером и ретраями."""

    def __init__(
        self,
        backend_url: str | None = None,
        token: str | None = None,
        *,
        timeout_s: float | None = None,
        max_attempts: int | None = None,
        buffer_path: str | None = None,
        buffer_max_items: int | None = None,
        max_total_seconds: float | None = None,
        confidence_threshold: float | None = None,
        enforce_confidence_threshold: bool = True,
        flush_batch: int = 50,
        backoff_base_s: float = DEFAULT_BACKOFF_BASE_S,
        backoff_cap_s: float = DEFAULT_BACKOFF_CAP_S,
        http_client: httpx.Client | None = None,
        buffer: SnapshotBuffer | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        rng: random.Random | None = None,
    ) -> None:
        # --- конфигурация строго по CLAUDE.md -----------------------------
        self.backend_url = (
            backend_url or os.getenv("BACKEND_URL") or "http://backend:8000"
        ).rstrip("/")
        self._token = token if token is not None else os.getenv("CV_SERVICE_TOKEN", "")
        self.frame_interval_s = env_float("FRAME_INTERVAL", 15.0)
        self.confidence_threshold = (
            confidence_threshold
            if confidence_threshold is not None
            else env_float("CONFIDENCE_THRESHOLD", 0.7)
        )
        self.enforce_confidence_threshold = enforce_confidence_threshold

        self.timeout_s = (
            timeout_s if timeout_s is not None else env_float("CV_HTTP_TIMEOUT", DEFAULT_TIMEOUT_S)
        )
        self.max_attempts = max(
            1,
            max_attempts
            if max_attempts is not None
            else env_int("CV_PUBLISH_ATTEMPTS", DEFAULT_MAX_ATTEMPTS),
        )
        # Публикация не должна съедать больше одного кадрового интервала.
        self.max_total_seconds = (
            max_total_seconds
            if max_total_seconds is not None
            else env_float("CV_PUBLISH_BUDGET", self.frame_interval_s)
        )
        self.backoff_base_s = backoff_base_s
        self.backoff_cap_s = backoff_cap_s
        self.flush_batch = flush_batch

        self._sleep = sleep
        self._monotonic = monotonic
        self._rng = rng or random.Random()

        self.buffer = buffer or SnapshotBuffer(
            path=buffer_path or os.getenv("CV_BUFFER_PATH", "buffer.db"),
            max_items=buffer_max_items
            if buffer_max_items is not None
            else env_int("CV_BUFFER_MAX", DEFAULT_BUFFER_MAX),
        )
        self._owns_buffer = buffer is None

        self._client = http_client or httpx.Client(
            timeout=httpx.Timeout(
                self.timeout_s,
                connect=min(2.0, self.timeout_s),
            )
        )
        self._owns_client = http_client is None

        self._log(
            "client_init",
            backend_url=self.backend_url,
            token=_mask(self._token),
            timeout_s=self.timeout_s,
            max_attempts=self.max_attempts,
            budget_s=self.max_total_seconds,
            buffer_max=self.buffer.max_items,
            buffered=self.buffer.count(),
            confidence_threshold=self.confidence_threshold,
        )

    # -- логи --------------------------------------------------------------- #

    @staticmethod
    def _log(event: str, level: int = logging.INFO, **fields: Any) -> None:
        record = {"event": event, **fields}
        try:
            line = json.dumps(record, ensure_ascii=False, default=str)
        except (TypeError, ValueError):  # pragma: no cover - страховка
            line = json.dumps({"event": event, "repr": repr(fields)[:500]})
        logger.log(level, line)

    # -- публичный интерфейс ------------------------------------------------ #

    @property
    def occupancy_url(self) -> str:
        return f"{self.backend_url}{API_PREFIX}{OCCUPANCY_PATH}"

    def publish(self, payload: OccupancyPayload) -> PublishResult:
        """Отправить полный снапшот камеры. Никогда не бросает сетевых исключений.

        Если в буфере уже что-то лежит, новый снапшот сначала кладётся в буфер,
        а затем буфер дозаливается целиком — так гарантируется порядок по
        ``timestamp`` кадра.
        """
        prepared = payload.with_frame_id()
        if self.enforce_confidence_threshold:
            prepared = prepared.apply_confidence_threshold(self.confidence_threshold)
        body = prepared.model_dump_json()
        deadline = self._monotonic() + self.max_total_seconds

        if self.buffer.count() > 0:
            row_id = self.buffer.append(prepared, body)
            flushed = self.flush(deadline=deadline)
            delivered = not self.buffer.exists(row_id)
            outcome = PublishOutcome.SENT if delivered else PublishOutcome.BUFFERED
            self._log(
                "publish_via_buffer",
                **prepared.summary(),
                outcome=outcome.value,
                flushed=flushed,
                buffered=self.buffer.count(),
            )
            return PublishResult(
                outcome=outcome,
                flushed=flushed,
                buffer_size=self.buffer.count(),
                error=None if delivered else "backend unavailable, snapshot buffered",
            )

        attempt = self._send(body, deadline, context=prepared.summary())
        return self._finalize(prepared, body, attempt)

    def flush(self, deadline: float | None = None, limit: int | None = None) -> int:
        """Дозалить буфер в порядке ``timestamp``. Возвращает число доставленных.

        Останавливается на первой неудаче — порядок важнее пропускной
        способности: backend не должен увидеть более свежий снапшот раньше
        более старого.
        """
        if deadline is None:
            deadline = self._monotonic() + self.max_total_seconds
        sent = 0
        batch_limit = limit if limit is not None else self.flush_batch
        while sent < batch_limit:
            if self._monotonic() >= deadline:
                self._log("flush_budget_exhausted", sent=sent, buffered=self.buffer.count())
                break
            rows = self.buffer.peek(limit=1)
            if not rows:
                break
            row = rows[0]
            attempt = self._send(
                row.body,
                deadline,
                context={
                    "camera_id": row.camera_id,
                    "frame_id": row.frame_id,
                    "source": "buffer",
                },
            )
            if attempt.ok:
                self.buffer.drop(row.row_id)
                sent += 1
                continue
            if attempt.retriable:
                self.buffer.bump_attempts(row.row_id)
                self._log(
                    "flush_stopped",
                    level=logging.WARNING,
                    reason=attempt.error,
                    status_code=attempt.status_code,
                    sent=sent,
                    buffered=self.buffer.count(),
                )
                break
            # 4xx — повторять бессмысленно, убираем из очереди в DLQ,
            # иначе одна битая запись навсегда заблокирует дозаливку.
            self.buffer.move_to_dlq(
                row.row_id, attempt.status_code, attempt.error or "rejected"
            )
            self._log(
                "flush_dlq",
                level=logging.ERROR,
                camera_id=row.camera_id,
                frame_id=row.frame_id,
                status_code=attempt.status_code,
                error=attempt.error,
            )
        if sent:
            self._log("flush_done", sent=sent, buffered=self.buffer.count())
        return sent

    def health(self) -> HealthResponse | None:
        """``GET /health``. Возвращает None, если backend недоступен/ответ кривой."""
        for url in (f"{self.backend_url}/health", f"{self.backend_url}{API_PREFIX}/health"):
            try:
                resp = self._client.get(url, headers=self._headers())
            except httpx.HTTPError as exc:
                self._log(
                    "health_error",
                    level=logging.WARNING,
                    url=url,
                    error=type(exc).__name__,
                )
                continue
            if resp.status_code == 404:
                continue
            if resp.status_code >= 400:
                self._log("health_bad_status", level=logging.WARNING, status_code=resp.status_code)
                return None
            try:
                return HealthResponse.model_validate_json(resp.text)
            except ValueError as exc:
                self._log("health_bad_body", level=logging.WARNING, error=str(exc)[:300])
                return None
        return None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()
        if self._owns_buffer:
            self.buffer.close()

    def __enter__(self) -> BackendClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- внутреннее --------------------------------------------------------- #

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "parkvision-cv/1.0",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _backoff(self, attempt_no: int) -> float:
        """Экспоненциальный бэкофф с «половинным» джиттером."""
        raw = min(self.backoff_cap_s, self.backoff_base_s * (2 ** (attempt_no - 1)))
        return raw / 2.0 + self._rng.uniform(0.0, raw / 2.0)

    def _send(
        self, body: str, deadline: float, context: dict[str, Any]
    ) -> _Attempt:
        last: _Attempt = _Attempt(ok=False, attempts=0, error="not attempted")
        for attempt_no in range(1, self.max_attempts + 1):
            if self._monotonic() >= deadline and attempt_no > 1:
                last.budget_exhausted = True
                self._log("publish_budget_exhausted", level=logging.WARNING, **context)
                return last
            started = self._monotonic()
            try:
                resp = self._client.post(
                    self.occupancy_url,
                    content=body.encode("utf-8"),
                    headers=self._headers(),
                )
            except httpx.TimeoutException as exc:
                last = _Attempt(
                    ok=False,
                    attempts=attempt_no,
                    retriable=True,
                    error=f"timeout: {type(exc).__name__}",
                )
            except httpx.HTTPError as exc:
                last = _Attempt(
                    ok=False,
                    attempts=attempt_no,
                    retriable=True,
                    error=f"network: {type(exc).__name__}",
                )
            else:
                latency_ms = int((self._monotonic() - started) * 1000)
                code = resp.status_code
                if 200 <= code < 300:
                    self._log(
                        "publish_ok",
                        **context,
                        attempt=attempt_no,
                        status_code=code,
                        latency_ms=latency_ms,
                    )
                    return _Attempt(
                        ok=True, attempts=attempt_no, status_code=code, body=resp.text
                    )
                retriable = code >= 500 or code == 429
                last = _Attempt(
                    ok=False,
                    attempts=attempt_no,
                    status_code=code,
                    body=resp.text,
                    retriable=retriable,
                    error=f"http {code}: {resp.text[:200]}",
                )
                if not retriable:
                    self._log(
                        "publish_rejected",
                        level=logging.ERROR,
                        **context,
                        attempt=attempt_no,
                        status_code=code,
                        error=last.error,
                    )
                    return last

            self._log(
                "publish_retry" if attempt_no < self.max_attempts else "publish_failed",
                level=logging.WARNING,
                **context,
                attempt=attempt_no,
                status_code=last.status_code,
                error=last.error,
            )
            if attempt_no >= self.max_attempts:
                break
            delay = self._backoff(attempt_no)
            if self._monotonic() + delay >= deadline:
                last.budget_exhausted = True
                self._log("publish_budget_exhausted", level=logging.WARNING, **context)
                break
            self._sleep(delay)
        return last

    def _finalize(
        self, payload: OccupancyPayload, body: str, attempt: _Attempt
    ) -> PublishResult:
        if attempt.ok:
            response: OccupancyResponse | None = None
            if attempt.body:
                try:
                    response = OccupancyResponse.model_validate_json(attempt.body)
                except ValueError as exc:
                    # Backend ответил 2xx, но телом, которого мы не понимаем:
                    # снапшот всё равно доставлен, просто логируем расхождение.
                    self._log(
                        "response_schema_mismatch",
                        level=logging.WARNING,
                        error=str(exc)[:300],
                        body=(attempt.body or "")[:300],
                    )
            rejected = [r.spot_id for r in response.rejected] if response else []
            if response and response.duplicate:
                outcome = PublishOutcome.DUPLICATE
            elif rejected:
                outcome = PublishOutcome.PARTIAL
                self._log(
                    "publish_partial",
                    level=logging.WARNING,
                    camera_id=payload.camera_id,
                    frame_id=payload.frame_id,
                    rejected=rejected,
                )
            else:
                outcome = PublishOutcome.SENT
            return PublishResult(
                outcome=outcome,
                attempts=attempt.attempts,
                status_code=attempt.status_code,
                response=response,
                rejected_spot_ids=rejected,
                buffer_size=self.buffer.count(),
            )

        if attempt.retriable or attempt.budget_exhausted:
            self.buffer.append(payload, body)
            size = self.buffer.count()
            self._log(
                "snapshot_buffered",
                level=logging.WARNING,
                **payload.summary(),
                attempts=attempt.attempts,
                status_code=attempt.status_code,
                error=attempt.error,
                buffered=size,
                evicted=self.buffer.last_evicted,
            )
            return PublishResult(
                outcome=PublishOutcome.BUFFERED,
                attempts=attempt.attempts,
                status_code=attempt.status_code,
                error=attempt.error,
                buffer_size=size,
            )

        self.buffer.append_dlq(
            payload, body, attempt.status_code, attempt.error or "rejected"
        )
        self._log(
            "snapshot_dropped",
            level=logging.ERROR,
            **payload.summary(),
            attempts=attempt.attempts,
            status_code=attempt.status_code,
            error=attempt.error,
            dlq=self.buffer.dlq_count(),
        )
        return PublishResult(
            outcome=PublishOutcome.DROPPED,
            attempts=attempt.attempts,
            status_code=attempt.status_code,
            error=attempt.error,
            buffer_size=self.buffer.count(),
        )


def build_payload(
    camera_id: str,
    frame_time: datetime,
    detections: list[dict[str, Any]] | None = None,
    zone_counts: list[dict[str, Any]] | None = None,
    camera_status: str | None = None,
) -> OccupancyPayload:
    """Удобный конструктор payload из «сырых» словарей CV-пайплайна."""
    return OccupancyPayload.model_validate(
        {
            "camera_id": camera_id,
            "timestamp": frame_time,
            "detections": detections or [],
            "zone_counts": zone_counts or [],
            "camera_status": camera_status,
        }
    ).with_frame_id()
