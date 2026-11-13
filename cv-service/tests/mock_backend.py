"""FastAPI-мок backend ParkVision для тестов слоя публикации.

Реализует ровно тот контракт, который описан в
``cv-service/docs/CV_BACKEND_CONTRACT.md``:

* ``POST /api/v1/internal/occupancy`` — приём снапшота, Bearer-авторизация,
  валидация по ``cvsvc.publish.schemas.OccupancyPayload``, дедуп по ``frame_id``,
  частичный приём с перечислением отклонённых ``spot_id``;
* ``GET /health``;
* служебные ручки ``/__mock__/*`` — управление поведением и чтение принятых
  снапшотов из тестов.

Мок умеет по флагу изображать сбои: 500, зависание (таймаут), 401 и частичный
приём. Это же приложение можно поднять руками для ручной отладки CV-сервиса::

    uvicorn tests.mock_backend:app --port 8000
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterator, Literal

import httpx
import uvicorn
from fastapi import APIRouter, FastAPI, Header, Request, Response
from fastapi.responses import JSONResponse

from cvsvc.publish.schemas import (
    HealthResponse,
    OccupancyPayload,
    OccupancyResponse,
    RejectedSpot,
    now_vvo,
)

DEFAULT_TOKEN = "test-cv-token"

FailMode = Literal["ok", "500", "timeout", "401", "partial"]


@dataclass
class MockState:
    """Изменяемое состояние мока. Делится между запросами и тестом."""

    token: str = DEFAULT_TOKEN
    mode: FailMode = "ok"
    #: сколько ближайших запросов попадает под `mode`; -1 — бесконечно
    remaining: int = -1
    #: задержка ответа в режиме "timeout"
    delay_s: float = 2.0
    #: если задано, spot_id вне множества отклоняются (режим "partial")
    known_spot_ids: set[str] | None = None
    #: принятые снапшоты в порядке приёма (сырой JSON как пришёл)
    snapshots: list[dict[str, Any]] = field(default_factory=list)
    #: frame_id уже принятых снапшотов — дедупликация ретраев
    seen_frame_ids: set[str] = field(default_factory=set)
    #: счётчики для ассертов в тестах
    request_count: int = 0
    duplicate_count: int = 0
    unauthorized_count: int = 0
    invalid_count: int = 0
    #: был ли хоть раз прислан корректный Bearer (сам токен не сохраняем)
    saw_bearer: bool = False

    def take_mode(self) -> FailMode:
        """Вернуть режим для текущего запроса, списав одно «срабатывание»."""
        if self.mode == "ok":
            return "ok"
        if self.remaining < 0:
            return self.mode
        if self.remaining == 0:
            return "ok"
        self.remaining -= 1
        mode = self.mode
        if self.remaining == 0:
            self.mode = "ok"
        return mode

    def reset(self) -> None:
        self.token = DEFAULT_TOKEN
        self.mode = "ok"
        self.remaining = -1
        self.delay_s = 2.0
        self.known_spot_ids = None
        self.snapshots.clear()
        self.seen_frame_ids.clear()
        self.request_count = 0
        self.duplicate_count = 0
        self.unauthorized_count = 0
        self.invalid_count = 0
        self.saw_bearer = False


def create_app(state: MockState | None = None) -> FastAPI:
    """Собрать приложение-мок. ``app.state.mock`` — объект :class:`MockState`."""
    mock = state or MockState()
    app = FastAPI(title="ParkVision backend (mock)", version="1.0.0")
    app.state.mock = mock

    api = APIRouter(prefix="/api/v1")

    @api.post("/internal/occupancy")
    async def internal_occupancy(  # noqa: C901 - ветвление по режимам сбоя
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> Response:
        mock.request_count += 1
        mode = mock.take_mode()

        if mode == "timeout":
            # Висим дольше клиентского таймаута — клиент отвалится сам.
            await asyncio.sleep(mock.delay_s)
            return JSONResponse({"status": "late"}, status_code=200)

        if mode == "500":
            return JSONResponse(
                {"detail": "simulated backend failure"}, status_code=500
            )

        expected = f"Bearer {mock.token}"
        if mode == "401" or authorization != expected:
            mock.unauthorized_count += 1
            return JSONResponse(
                {"detail": "invalid or missing CV_SERVICE_TOKEN"}, status_code=401
            )
        mock.saw_bearer = True

        raw = await request.body()
        try:
            payload = OccupancyPayload.model_validate_json(raw)
        except ValueError as exc:
            mock.invalid_count += 1
            return JSONResponse(
                {"detail": "payload does not match schema", "errors": str(exc)[:2000]},
                status_code=422,
            )

        # --- дедупликация ретраев по frame_id --------------------------------
        if payload.frame_id and payload.frame_id in mock.seen_frame_ids:
            mock.duplicate_count += 1
            return JSONResponse(
                OccupancyResponse(
                    status="accepted",
                    processed=0,
                    timestamp=now_vvo(),
                    duplicate=True,
                ).model_dump(mode="json"),
                status_code=200,
            )

        # --- частичный приём -------------------------------------------------
        rejected: list[RejectedSpot] = []
        accepted = 0
        for det in payload.detections:
            if (
                mock.known_spot_ids is not None
                and det.spot_id not in mock.known_spot_ids
            ):
                rejected.append(
                    RejectedSpot(spot_id=det.spot_id, reason="unknown_spot_id")
                )
            else:
                accepted += 1
        accepted += len(payload.zone_counts)

        mock.snapshots.append(payload.model_dump(mode="json"))
        if payload.frame_id:
            mock.seen_frame_ids.add(payload.frame_id)

        return JSONResponse(
            OccupancyResponse(
                status="accepted" if not rejected else "partially_accepted",
                processed=accepted,
                timestamp=now_vvo(),
                rejected=rejected,
            ).model_dump(mode="json"),
            status_code=200,
        )

    app.include_router(api)

    @app.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return HealthResponse(
            status="healthy",
            version="1.0.0",
            timestamp=now_vvo(),
            database="connected",
            cv_service="active",
        )

    # ------------------------------------------------------------------ #
    # служебные ручки для тестов
    # ------------------------------------------------------------------ #

    @app.post("/__mock__/config")
    async def configure(cfg: dict[str, Any]) -> dict[str, Any]:
        if "mode" in cfg:
            mock.mode = cfg["mode"]
        if "remaining" in cfg:
            mock.remaining = int(cfg["remaining"])
        if "delay_s" in cfg:
            mock.delay_s = float(cfg["delay_s"])
        if "known_spot_ids" in cfg:
            ids = cfg["known_spot_ids"]
            mock.known_spot_ids = None if ids is None else set(ids)
        if "token" in cfg:
            mock.token = str(cfg["token"])
        return {"mode": mock.mode, "remaining": mock.remaining}

    @app.post("/__mock__/reset")
    async def reset() -> dict[str, str]:
        mock.reset()
        return {"status": "reset"}

    @app.get("/__mock__/snapshots")
    async def snapshots() -> dict[str, Any]:
        return {"count": len(mock.snapshots), "snapshots": mock.snapshots}

    @app.get("/__mock__/stats")
    async def stats() -> dict[str, Any]:
        return {
            "requests": mock.request_count,
            "accepted": len(mock.snapshots),
            "duplicates": mock.duplicate_count,
            "unauthorized": mock.unauthorized_count,
            "invalid": mock.invalid_count,
            "mode": mock.mode,
            "saw_bearer": mock.saw_bearer,
        }

    return app


app = create_app()


# --------------------------------------------------------------------------- #
# Запуск в потоке — используется pytest-фикстурой
# --------------------------------------------------------------------------- #


class MockServer:
    """uvicorn-сервер мока в фоновом потоке (нужен реальный сокет для таймаутов)."""

    def __init__(self, application: FastAPI, host: str = "127.0.0.1", port: int = 0):
        self.app = application
        config = uvicorn.Config(
            application, host=host, port=port, log_level="error", access_log=False
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self.host = host
        self.port = port

    @property
    def state(self) -> MockState:
        return self.app.state.mock  # type: ignore[no-any-return]

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self, timeout: float = 15.0) -> None:
        self._thread.start()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            servers = getattr(self._server, "servers", None)
            if self._server.started and servers:
                self.port = servers[0].sockets[0].getsockname()[1]
                return
            time.sleep(0.02)
        raise RuntimeError("mock backend не поднялся вовремя")

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=10)


@contextlib.contextmanager
def running_mock_backend(port: int = 0) -> Iterator[MockServer]:
    """Контекст-менеджер: поднять мок, отдать его, остановить."""
    server = MockServer(create_app(), port=port)
    server.start()
    try:
        yield server
    finally:
        server.stop()


def wait_until_healthy(base_url: str, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(f"{base_url}/health", timeout=1.0)
            if resp.status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(0.05)
    return False
