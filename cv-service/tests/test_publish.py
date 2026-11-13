"""Тесты слоя публикации CV -> backend.

Запуск::

    cd cv-service && python -m pytest tests/test_publish.py -v

Время в ретраях замокано (``FakeClock``), поэтому тесты не ждут бэкофф
по-настоящему. Реальный сетевой таймаут проверяется отдельно, с маленьким
значением таймаута.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

import httpx
import pytest
from pydantic import ValidationError

from cvsvc.publish.buffer import SnapshotBuffer
from cvsvc.publish.client import BackendClient, PublishOutcome
from cvsvc.publish.schemas import (
    VLADIVOSTOK,
    Camera,
    CameraStatus,
    Detection,
    OccupancyHistory,
    OccupancyPayload,
    ParkingLot,
    ParkingSpot,
    SpotStatus,
    ZoneCount,
)
from tests.mock_backend import DEFAULT_TOKEN, running_mock_backend

# --------------------------------------------------------------------------- #
# Инфраструктура
# --------------------------------------------------------------------------- #


class FakeClock:
    """Монотонные часы, которые двигает только ``sleep`` — бэкофф без ожидания."""

    def __init__(self, start: float = 1_000.0) -> None:
        self.t = start
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds


@pytest.fixture(scope="module")
def server():
    with running_mock_backend() as srv:
        yield srv


@pytest.fixture(autouse=True)
def _reset(server):
    server.state.reset()
    yield
    server.state.reset()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def make_client(server, tmp_path, clock, **kwargs) -> BackendClient:
    buffer = kwargs.pop("buffer", None) or SnapshotBuffer(
        path=str(tmp_path / "buffer.db"),
        max_items=kwargs.pop("buffer_max_items", 2000),
    )
    kwargs.pop("buffer_max_items", None)
    params = {
        "backend_url": server.base_url,
        "token": DEFAULT_TOKEN,
        "timeout_s": 2.0,
        "max_attempts": 3,
        "buffer": buffer,
        "sleep": clock.sleep,
        "monotonic": clock.monotonic,
        "max_total_seconds": 60.0,
        "backoff_base_s": 0.5,
    }
    params.update(kwargs)
    return BackendClient(**params)


FRAME_TS = datetime(2026, 11, 13, 10, 30, 0, tzinfo=VLADIVOSTOK)


def payload(
    camera_id: str = "CAM-001",
    ts: datetime | None = None,
    detections: list[Detection] | None = None,
    **kwargs,
) -> OccupancyPayload:
    return OccupancyPayload(
        camera_id=camera_id,
        timestamp=ts or FRAME_TS,
        detections=detections
        if detections is not None
        else [
            Detection(spot_id="S-014", status=SpotStatus.FREE, confidence=0.97),
            Detection(spot_id="S-015", status=SpotStatus.OCCUPIED, confidence=0.99),
        ],
        **kwargs,
    )


def accepted(server) -> list[dict]:
    resp = httpx.get(f"{server.base_url}/__mock__/snapshots", timeout=5.0)
    resp.raise_for_status()
    return resp.json()["snapshots"]


def stats(server) -> dict:
    return httpx.get(f"{server.base_url}/__mock__/stats", timeout=5.0).json()


def configure(server, **cfg) -> None:
    httpx.post(f"{server.base_url}/__mock__/config", json=cfg, timeout=5.0).raise_for_status()


# --------------------------------------------------------------------------- #
# 1. Схемы: зеркала CLAUDE.md
# --------------------------------------------------------------------------- #


CLAUDE_MD_PAYLOAD = {
    "camera_id": "CAM-001",
    "timestamp": "2026-11-13T10:30:00+10:00",
    "detections": [
        {"spot_id": "S-014", "status": "free", "confidence": 0.97},
        {"spot_id": "S-015", "status": "occupied", "confidence": 0.99},
    ],
}

EXTENDED_PAYLOAD = {
    "camera_id": "CAM-003",
    "timestamp": "2026-11-13T10:30:00+10:00",
    "frame_id": "CAM-003-1794539345",
    "camera_status": "active",
    "detections": [{"spot_id": "S-301", "status": "free", "confidence": 0.97}],
    "zone_counts": [
        {
            "zone_id": "Z-301",
            "capacity": 42,
            "free": 7,
            "occupied": 33,
            "unknown": 2,
            "confidence": 0.88,
        }
    ],
}


def test_claude_md_example_payload_is_valid():
    """Payload из примера CLAUDE.md проходит валидацию без изменений."""
    p = OccupancyPayload.model_validate(CLAUDE_MD_PAYLOAD)
    assert p.camera_id == "CAM-001"
    assert p.timestamp.utcoffset() == timedelta(hours=10)
    assert [d.spot_id for d in p.detections] == ["S-014", "S-015"]
    assert p.detections[0].status is SpotStatus.FREE
    # расширения не обязательны
    assert p.frame_id is None
    assert p.camera_status is None
    assert p.zone_counts == []


def test_extended_payload_is_valid():
    """Согласованное расширение (frame_id / camera_status / zone_counts)."""
    p = OccupancyPayload.model_validate(EXTENDED_PAYLOAD)
    assert p.frame_id == "CAM-003-1794539345"
    assert p.camera_status is CameraStatus.ACTIVE
    assert p.zone_counts[0].capacity == 42
    assert p.zone_counts[0].free + p.zone_counts[0].occupied + p.zone_counts[
        0
    ].unknown == 42
    # round-trip через JSON не теряет ничего
    assert OccupancyPayload.model_validate_json(p.model_dump_json()) == p


def test_detection_status_unknown_supported():
    p = OccupancyPayload.model_validate(
        {
            "camera_id": "CAM-002",
            "timestamp": "2026-11-13T10:30:00+10:00",
            "detections": [
                {"spot_id": "S-200", "status": "unknown", "confidence": 0.31}
            ],
        }
    )
    assert p.detections[0].status is SpotStatus.UNKNOWN


def test_naive_timestamp_rejected():
    with pytest.raises(ValidationError, match="timezone-aware"):
        OccupancyPayload.model_validate(
            {
                "camera_id": "CAM-001",
                "timestamp": "2026-11-13T10:30:00",
                "detections": [
                    {"spot_id": "S-014", "status": "free", "confidence": 0.9}
                ],
            }
        )


def test_confidence_out_of_range_rejected():
    with pytest.raises(ValidationError):
        Detection(spot_id="S-1", status=SpotStatus.FREE, confidence=1.4)


def test_zone_counts_must_sum_to_capacity():
    with pytest.raises(ValidationError, match="capacity"):
        ZoneCount(zone_id="Z-301", capacity=42, free=7, occupied=33, unknown=0)


def test_empty_payload_rejected():
    with pytest.raises(ValidationError, match="payload пуст"):
        OccupancyPayload(camera_id="CAM-001", timestamp=FRAME_TS, detections=[])


def test_parking_spot_requires_roi_frame_size():
    base = {
        "id": "S-014",
        "parking_lot_id": "P-001",
        "camera_id": "CAM-001",
        "latitude": 43.115141,
        "longitude": 131.885341,
        "approach_lat": 43.1152,
        "approach_lon": 131.8854,
        "roi_polygon": [[100, 100], [200, 100], [200, 200], [100, 200]],
        "spot_type": "standard",
        "status": "free",
        "confidence": 0.97,
        "updated_at": "2026-11-13T10:30:00+10:00",
    }
    with pytest.raises(ValidationError, match="roi_frame_size"):
        ParkingSpot.model_validate(base)

    spot = ParkingSpot.model_validate({**base, "roi_frame_size": [1920, 1080]})
    assert spot.roi_frame_size == (1920, 1080)

    # ROI, размеченный на 2592x1944, не влезает в кадр 1920x1080
    with pytest.raises(ValidationError, match="вне кадра"):
        ParkingSpot.model_validate(
            {
                **base,
                "roi_frame_size": [1920, 1080],
                "roi_polygon": [[2500, 1900], [2550, 1900], [2550, 1930]],
            }
        )


def test_other_spec_models_mirror_claude_md():
    lot = ParkingLot.model_validate(
        {
            "id": "P-001",
            "name": "Парковка у ТЦ Clover House",
            "latitude": 43.115141,
            "longitude": 131.885341,
            "capacity": 25,
            "parking_type": "free",
            "access_rules": "круглосуточно",
            "entrance_lat": 43.1152,
            "entrance_lon": 131.8854,
        }
    )
    assert lot.has_discrete_spots is True  # EXT-поле по умолчанию обратносовместимо

    cam = Camera.model_validate(
        {
            "id": "CAM-001",
            "parking_lot_id": "P-001",
            "source_reference": "https://cam.example/stream.m3u8",
            "status": "active",
            "last_frame_at": "2026-11-13T10:30:15+10:00",
        }
    )
    assert cam.status is CameraStatus.ACTIVE

    hist = OccupancyHistory.model_validate(
        {
            "id": 1234,
            "spot_id": "S-014",
            "status": "occupied",
            "confidence": 0.95,
            "observed_at": "2026-11-13T10:29:45+10:00",
        }
    )
    assert hist.status is SpotStatus.OCCUPIED


# --------------------------------------------------------------------------- #
# 2. Счастливый путь
# --------------------------------------------------------------------------- #


def test_valid_payload_accepted(server, tmp_path, clock):
    client = make_client(server, tmp_path, clock)
    result = client.publish(payload())

    assert result.outcome is PublishOutcome.SENT
    assert result.ok and result.attempts == 1 and result.status_code == 200
    assert result.response is not None and result.response.processed == 2
    assert client.buffer.count() == 0

    got = accepted(server)
    assert len(got) == 1
    assert got[0]["camera_id"] == "CAM-001"
    assert got[0]["frame_id"] == "CAM-001-" + str(int(FRAME_TS.timestamp()))
    assert [d["spot_id"] for d in got[0]["detections"]] == ["S-014", "S-015"]
    client.close()


def test_claude_md_payload_round_trip_through_client(server, tmp_path, clock):
    """Ровно тот payload, что в спеке, доезжает до backend и принимается."""
    client = make_client(server, tmp_path, clock)
    result = client.publish(OccupancyPayload.model_validate(CLAUDE_MD_PAYLOAD))
    assert result.outcome is PublishOutcome.SENT
    assert stats(server)["invalid"] == 0
    client.close()


def test_zone_snapshot_accepted(server, tmp_path, clock):
    client = make_client(server, tmp_path, clock)
    result = client.publish(OccupancyPayload.model_validate(EXTENDED_PAYLOAD))
    assert result.outcome is PublishOutcome.SENT
    got = accepted(server)[0]
    assert got["zone_counts"][0]["zone_id"] == "Z-301"
    assert got["camera_status"] == "active"
    client.close()


def test_duplicate_frame_id_deduplicated(server, tmp_path, clock):
    """Ретрай того же кадра backend обязан распознать по frame_id."""
    client = make_client(server, tmp_path, clock)
    first = client.publish(payload())
    second = client.publish(payload())

    assert first.outcome is PublishOutcome.SENT
    assert second.outcome is PublishOutcome.DUPLICATE
    assert len(accepted(server)) == 1
    assert stats(server)["duplicates"] == 1
    client.close()


def test_low_confidence_downgraded_to_unknown(server, tmp_path, clock):
    """CONFIDENCE_THRESHOLD: ниже порога статус понижается до unknown."""
    client = make_client(server, tmp_path, clock, confidence_threshold=0.7)
    client.publish(
        payload(
            detections=[
                Detection(spot_id="S-014", status=SpotStatus.FREE, confidence=0.42),
                Detection(spot_id="S-015", status=SpotStatus.OCCUPIED, confidence=0.95),
            ]
        )
    )
    dets = {d["spot_id"]: d for d in accepted(server)[0]["detections"]}
    assert dets["S-014"]["status"] == "unknown"
    assert dets["S-014"]["confidence"] == pytest.approx(0.42)
    assert dets["S-015"]["status"] == "occupied"
    client.close()


def test_health_endpoint(server, tmp_path, clock):
    client = make_client(server, tmp_path, clock)
    health = client.health()
    assert health is not None
    assert health.status == "healthy" and health.database == "connected"
    client.close()


# --------------------------------------------------------------------------- #
# 3. Ошибки: 401 / 500 / таймаут / частичный приём
# --------------------------------------------------------------------------- #


def test_401_is_not_retried(server, tmp_path, clock):
    configure(server, mode="401", remaining=-1)
    client = make_client(server, tmp_path, clock)
    result = client.publish(payload())

    assert result.outcome is PublishOutcome.DROPPED
    assert result.attempts == 1, "4xx не должен ретраиться"
    assert result.status_code == 401
    assert stats(server)["requests"] == 1
    assert client.buffer.count() == 0
    assert client.buffer.dlq_count() == 1
    assert clock.slept == [], "бэкоффа при 4xx быть не должно"
    client.close()


def test_wrong_token_is_not_retried(server, tmp_path, clock):
    client = make_client(server, tmp_path, clock, token="wrong-token")
    result = client.publish(payload())
    assert result.outcome is PublishOutcome.DROPPED
    assert result.status_code == 401
    assert stats(server)["requests"] == 1
    client.close()


def test_500_is_retried_then_buffered(server, tmp_path, clock):
    configure(server, mode="500", remaining=-1)
    client = make_client(server, tmp_path, clock)
    result = client.publish(payload())

    assert result.outcome is PublishOutcome.BUFFERED
    assert result.attempts == 3, "3 попытки по умолчанию"
    assert stats(server)["requests"] == 3
    assert len(clock.slept) == 2, "между попытками — бэкофф"
    assert clock.slept[1] > clock.slept[0], "бэкофф растёт экспоненциально"
    assert client.buffer.count() == 1
    assert client.buffer.dlq_count() == 0
    client.close()


def test_500_then_recovery_within_same_publish(server, tmp_path, clock):
    """Два 500 подряд, третья попытка успешна — снапшот не попадает в буфер."""
    configure(server, mode="500", remaining=2)
    client = make_client(server, tmp_path, clock)
    result = client.publish(payload())

    assert result.outcome is PublishOutcome.SENT
    assert result.attempts == 3
    assert client.buffer.count() == 0
    assert len(accepted(server)) == 1
    client.close()


def test_timeout_is_retried_then_buffered(server, tmp_path, clock):
    configure(server, mode="timeout", remaining=-1, delay_s=1.5)
    client = make_client(server, tmp_path, clock, timeout_s=0.3)
    result = client.publish(payload())

    assert result.outcome is PublishOutcome.BUFFERED
    assert result.attempts == 3
    assert result.error is not None and "timeout" in result.error
    assert client.buffer.count() == 1
    client.close()


def test_backoff_has_jitter(server, tmp_path, clock):
    """Два клиента с разным rng не синхронизируют ретраи."""
    import random

    configure(server, mode="500", remaining=-1)
    c1 = make_client(server, tmp_path / "a", clock, rng=random.Random(1))
    clock2 = FakeClock()
    c2 = make_client(server, tmp_path / "b", clock2, rng=random.Random(2))
    c1.publish(payload())
    c2.publish(payload())
    assert clock.slept != clock2.slept
    c1.close()
    c2.close()


def test_partial_accept_reports_rejected_spots(server, tmp_path, clock):
    configure(server, known_spot_ids=["S-014"])
    client = make_client(server, tmp_path, clock)
    result = client.publish(payload())

    assert result.outcome is PublishOutcome.PARTIAL
    assert result.ok, "частичный приём — это всё-таки доставка"
    assert result.rejected_spot_ids == ["S-015"]
    assert result.response is not None and result.response.processed == 1
    assert client.buffer.count() == 0, "частичный приём не повод ретраить"
    client.close()


def test_backend_absent_does_not_raise(tmp_path, clock):
    """Backend не поднят вообще: publish не бросает, снапшот в буфере."""
    buffer = SnapshotBuffer(path=str(tmp_path / "buffer.db"))
    client = BackendClient(
        backend_url="http://127.0.0.1:9",  # порт discard, соединение не встанет
        token=DEFAULT_TOKEN,
        timeout_s=0.5,
        max_attempts=2,
        buffer=buffer,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        max_total_seconds=30.0,
    )
    result = client.publish(payload())
    assert result.outcome is PublishOutcome.BUFFERED
    assert client.buffer.count() == 1
    assert client.health() is None
    client.close()


def test_invalid_payload_rejected_by_mock(server, tmp_path, clock):
    """Мок действительно валидирует тело по схеме (а не глотает что угодно)."""
    resp = httpx.post(
        f"{server.base_url}/api/v1/internal/occupancy",
        headers={"Authorization": f"Bearer {DEFAULT_TOKEN}"},
        json={"camera_id": "CAM-001", "detections": []},
        timeout=5.0,
    )
    assert resp.status_code == 422
    assert stats(server)["invalid"] == 1


# --------------------------------------------------------------------------- #
# 4. Буфер и дозаливка
# --------------------------------------------------------------------------- #


def _publish_outage(client, server, minutes: int = 5, interval_s: int = 15):
    """Backend лежит `minutes` минут, кадры идут раз в `interval_s` секунд."""
    configure(server, mode="500", remaining=-1)
    count = minutes * 60 // interval_s
    expected = []
    for i in range(count):
        ts = FRAME_TS + timedelta(seconds=i * interval_s)
        expected.append(ts.isoformat())
        result = client.publish(payload(ts=ts))
        assert result.outcome is PublishOutcome.BUFFERED
    return expected


def test_outage_then_backfill_in_timestamp_order(server, tmp_path, clock):
    """Backend лежит 5 «минут» → после подъёма всё дозалито в порядке timestamp."""
    client = make_client(server, tmp_path, clock)
    expected = _publish_outage(client, server, minutes=5, interval_s=15)

    assert client.buffer.count() == 20
    assert len(accepted(server)) == 0

    configure(server, mode="ok", remaining=-1)
    flushed = client.flush()

    assert flushed == 20
    assert client.buffer.count() == 0
    got = [s["timestamp"] for s in accepted(server)]
    assert got == expected, "порядок дозаливки должен быть по времени кадра"
    assert got == sorted(got)
    client.close()


def test_backfill_preserves_order_when_mixed_with_live_publish(
    server, tmp_path, clock
):
    """Пока буфер не пуст, свежий снапшот тоже идёт через буфер — порядок цел."""
    client = make_client(server, tmp_path, clock)
    expected = _publish_outage(client, server, minutes=1, interval_s=15)

    configure(server, mode="ok", remaining=-1)
    live_ts = FRAME_TS + timedelta(seconds=60)
    expected.append(live_ts.isoformat())
    result = client.publish(payload(ts=live_ts))

    assert result.outcome is PublishOutcome.SENT
    assert result.flushed == len(expected) == 5
    assert client.buffer.count() == 0
    assert [s["timestamp"] for s in accepted(server)] == expected
    client.close()


def test_buffer_is_bounded_and_evicts_oldest(server, tmp_path, clock):
    client = make_client(server, tmp_path, clock, buffer_max_items=5)
    _publish_outage(client, server, minutes=5, interval_s=15)

    assert client.buffer.count() == 5, "буфер не должен расти бесконечно"

    configure(server, mode="ok", remaining=-1)
    client.flush()
    got = [s["timestamp"] for s in accepted(server)]
    newest = [
        (FRAME_TS + timedelta(seconds=i * 15)).isoformat() for i in range(15, 20)
    ]
    assert got == newest, "вытесняться должны самые старые снапшоты"
    client.close()


def test_buffer_survives_process_restart(server, tmp_path, clock):
    """Буфер на SQLite переживает перезапуск CV-сервиса."""
    path = str(tmp_path / "buffer.db")
    configure(server, mode="500", remaining=-1)
    first = make_client(server, tmp_path, clock, buffer=SnapshotBuffer(path=path))
    first.publish(payload())
    assert first.buffer.count() == 1
    first.close()

    configure(server, mode="ok", remaining=-1)
    second = make_client(server, tmp_path, clock, buffer=SnapshotBuffer(path=path))
    assert second.buffer.count() == 1
    assert second.flush() == 1
    assert len(accepted(server)) == 1
    second.close()


def test_poison_snapshot_does_not_block_backfill(server, tmp_path, clock):
    """4xx при дозаливке убирает запись в DLQ, остальная очередь едет дальше."""
    client = make_client(server, tmp_path, clock)
    buffered = _publish_outage(client, server, minutes=1, interval_s=15)
    assert client.buffer.count() == len(buffered) == 4

    # первый же снапшот отвергается навсегда, остальные принимаются
    configure(server, mode="401", remaining=1)
    flushed = client.flush()

    assert flushed == 3
    assert client.buffer.count() == 0
    assert client.buffer.dlq_count() == 1
    client.close()


# --------------------------------------------------------------------------- #
# 5. Логи
# --------------------------------------------------------------------------- #


def test_token_never_leaks_into_logs(server, tmp_path, clock, caplog):
    secret = "super-secret-cv-token-42"
    configure(server, token=secret)
    with caplog.at_level(logging.DEBUG, logger="cvsvc.publish"):
        client = make_client(server, tmp_path, clock, token=secret)
        client.publish(payload())
        configure(server, mode="500", remaining=-1)
        client.publish(payload(ts=FRAME_TS + timedelta(seconds=15)))
        client.close()

    blob = "\n".join(
        r.getMessage() for r in caplog.records if r.name == "cvsvc.publish"
    )
    assert blob, "логи должны писаться"
    assert secret not in blob
    assert "<len=" in blob, "токен логируется только в маскированном виде"


def test_logs_are_json_lines(server, tmp_path, clock, caplog):
    with caplog.at_level(logging.DEBUG, logger="cvsvc.publish"):
        client = make_client(server, tmp_path, clock)
        client.publish(payload())
        client.close()

    events = []
    for record in caplog.records:
        if record.name != "cvsvc.publish":
            continue
        parsed = json.loads(record.getMessage())  # бросит, если не json-строка
        events.append(parsed["event"])
    assert "client_init" in events
    assert "publish_ok" in events
