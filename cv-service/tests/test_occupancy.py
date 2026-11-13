"""Тесты A4: валидация кадра и стабилизация статусов.

Проверяем ровно те сценарии, из-за которых парковочный сервис теряет доверие:
мигающие места, «свободно» по устаревшим данным, занятость от проезжающей
машины и зависшая камера, которую никто не заметил.
"""

from __future__ import annotations

import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from cvsvc.config import load_config
from cvsvc.frame.validate import (
    FROZEN_STREAK_THRESHOLD,
    CameraFrameState,
    FrameQuality,
    validate_frame,
)
from cvsvc.ingest.hls import HASH_SIDE, Frame, average_hash, _masked_gray
from cvsvc.occupancy.stabilize import (
    MAX_FOOTPRINT_DRIFT_PX,
    Observation,
    SpotStatus,
    Stabilizer,
    UnknownReason,
    zone_counts,
)

TZ = timezone(timedelta(hours=10), "VLAT")
T0 = datetime(2026, 11, 13, 13, 0, 0, tzinfo=TZ)


# --------------------------------------------------------------------------- #
# Валидация кадра
# --------------------------------------------------------------------------- #


def _camera(tmp_path: Path, overlay: str = "[[0, 0, 620, 130]]"):
    cfg = tmp_path / "c.yml"
    cfg.write_text(
        textwrap.dedent(
            f"""
            version: 1
            defaults:
              headers: {{Referer: "https://vladlink.ru/"}}
            cameras:
              - id: CAM-T
                playlist_url: "https://example.com/i.m3u8"
                frame_size: [1920, 1080]
                overlay_exclude: {overlay}
                spots: []
                zones: []
            """
        ),
        encoding="utf-8",
    )
    return load_config(cfg, require_rois=False).camera("CAM-T")


def _frame(gray: bytes, cam, at: datetime = T0) -> Frame:
    return Frame(
        camera_id=cam.id,
        path=Path("/dev/null"),
        captured_at=at,
        width=cam.width,
        height=cam.height,
        ahash=average_hash(gray),
        content_hash=average_hash(_masked_gray(gray, cam)),
        size_bytes=1000,
        gray=gray,
    )


def _daylight_gray(seed: int = 0) -> bytes:
    """Яркая картинка с нормальным разбросом яркости."""
    return bytes((120 + (i * 7 + seed * 13) % 110) for i in range(HASH_SIDE * HASH_SIDE))


def test_good_daylight_frame_is_ok(tmp_path):
    cam = _camera(tmp_path)
    v = validate_frame(_frame(_daylight_gray(), cam), CameraFrameState())
    assert v.quality is FrameQuality.OK
    assert v.usable and v.confidence_scale == 1.0


def test_night_frame_is_not_usable(tmp_path):
    """Ночью честно уходим в unknown, а не выдумываем статусы."""
    cam = _camera(tmp_path)
    dark = bytes((i * 3) % 30 for i in range(HASH_SIDE * HASH_SIDE))
    v = validate_frame(_frame(dark, cam), CameraFrameState())
    assert v.quality is FrameQuality.NIGHT
    assert not v.usable and v.confidence_scale == 0.0


def test_fog_frame_is_low_contrast(tmp_path):
    """Туман, снег или залепленный объектив: яркость нормальная, разброса нет."""
    cam = _camera(tmp_path)
    flat = bytes(150 + (i % 3) for i in range(HASH_SIDE * HASH_SIDE))
    v = validate_frame(_frame(flat, cam), CameraFrameState())
    assert v.quality is FrameQuality.LOW_CONTRAST
    assert not v.usable


def test_dusk_lowers_confidence_but_stays_usable(tmp_path):
    cam = _camera(tmp_path)
    dusk = bytes((40 + (i * 5) % 45) for i in range(HASH_SIDE * HASH_SIDE))
    v = validate_frame(_frame(dusk, cam), CameraFrameState())
    assert v.quality is FrameQuality.DUSK
    assert v.usable and 0.0 < v.confidence_scale < 1.0


def test_frozen_stream_detected_despite_ticking_timestamp(tmp_path):
    """⚠️ Ключевой сценарий.

    Таймкод в углу тикает каждую секунду даже когда картинка замерла.
    Если бы детект зависания шёл по полному кадру, он бы не сработал НИКОГДА.
    """
    cam = _camera(tmp_path)
    state = CameraFrameState()
    base = bytearray(_daylight_gray())

    verdicts = []
    for i in range(FROZEN_STREAK_THRESHOLD + 1):
        g = bytearray(base)
        g[0] = (40 + i * 60) % 256  # меняется ТОЛЬКО таймкод
        g[1] = (90 + i * 50) % 256
        verdicts.append(validate_frame(_frame(bytes(g), cam), state))

    assert verdicts[0].quality is FrameQuality.OK
    assert verdicts[-1].quality is FrameQuality.FROZEN
    assert not verdicts[-1].usable


def test_changing_picture_resets_frozen_streak(tmp_path):
    cam = _camera(tmp_path)
    state = CameraFrameState()
    for _ in range(FROZEN_STREAK_THRESHOLD + 1):
        validate_frame(_frame(_daylight_gray(1), cam), state)
    assert state.frozen_streak >= FROZEN_STREAK_THRESHOLD

    v = validate_frame(_frame(_daylight_gray(42), cam), state)
    assert state.frozen_streak == 0
    assert v.quality is FrameQuality.OK


# --------------------------------------------------------------------------- #
# Гистерезис
# --------------------------------------------------------------------------- #


def _obs(status: SpotStatus, at: datetime, conf: float = 0.95, fp=(100.0, 200.0)):
    return Observation(
        status=status, confidence=conf, at=at,
        footprint=fp if status is SpotStatus.OCCUPIED else None,
    )


def _feed(stab: Stabilizer, spot: str, statuses, start: datetime, step_s: int = 15, **kw):
    v = None
    for i, s in enumerate(statuses):
        v = stab.observe(spot, _obs(s, start + timedelta(seconds=i * step_s), **kw))
    return v


def test_status_commits_after_three_of_five():
    stab = Stabilizer()
    v = _feed(stab, "S-1", [SpotStatus.FREE] * 2, T0)
    assert v.status is SpotStatus.UNKNOWN  # двух голосов мало

    v = _feed(
        stab, "S-1", [SpotStatus.FREE], T0 + timedelta(seconds=30)
    )
    assert v.status is SpotStatus.FREE
    assert v.changed
    assert v.confidence > 0.9


def test_single_contrary_observation_does_not_flip_status():
    """Одиночный выброс не должен мигать местом."""
    stab = Stabilizer()
    _feed(stab, "S-1", [SpotStatus.FREE] * 3, T0)
    v = stab.observe("S-1", _obs(SpotStatus.OCCUPIED, T0 + timedelta(seconds=45)))
    assert v.status is SpotStatus.FREE
    assert not v.changed


def test_sustained_change_does_flip_status():
    stab = Stabilizer()
    _feed(stab, "S-1", [SpotStatus.FREE] * 3, T0)
    v = _feed(
        stab, "S-1", [SpotStatus.OCCUPIED] * 3, T0 + timedelta(seconds=45)
    )
    assert v.status is SpotStatus.OCCUPIED
    assert v.changed


def test_passing_car_does_not_occupy_the_spot():
    """Проезжающая машина пересекает полигон — её след уезжает между циклами."""
    stab = Stabilizer()
    _feed(stab, "S-1", [SpotStatus.FREE] * 3, T0)

    t = T0 + timedelta(seconds=45)
    last = None
    for i in range(3):
        drift = (i + 1) * (MAX_FOOTPRINT_DRIFT_PX * 3)
        last = stab.observe(
            "S-1",
            Observation(
                status=SpotStatus.OCCUPIED,
                confidence=0.95,
                at=t + timedelta(seconds=i * 15),
                footprint=(100.0 + drift, 200.0),
            ),
        )
    assert last.status is SpotStatus.FREE  # транзит не засчитан


def test_parked_car_with_small_jitter_does_occupy():
    """Припаркованная машина чуть дрожит из-за шума детектора — это не транзит."""
    stab = Stabilizer()
    t = T0
    last = None
    for i in range(3):
        last = stab.observe(
            "S-1",
            Observation(
                status=SpotStatus.OCCUPIED,
                confidence=0.95,
                at=t + timedelta(seconds=i * 15),
                footprint=(100.0 + i * 2.0, 200.0 - i * 1.5),
            ),
        )
    assert last.status is SpotStatus.OCCUPIED


def test_low_confidence_becomes_unknown_never_free():
    """Неуверенный детектор — это «не знаю», а не «свободно»."""
    stab = Stabilizer()
    v = _feed(stab, "S-1", [SpotStatus.FREE] * 5, T0, conf=0.3)
    assert v.status is SpotStatus.UNKNOWN
    assert v.reason is UnknownReason.NO_CONSENSUS


# --------------------------------------------------------------------------- #
# Свежесть — требование спеки
# --------------------------------------------------------------------------- #


def test_stale_data_never_reported_as_free():
    """CLAUDE.md: устаревшие статусы НЕ показывать как free."""
    stab = Stabilizer()
    v = _feed(stab, "S-1", [SpotStatus.FREE] * 3, T0)
    assert v.status is SpotStatus.FREE

    later = stab.refresh_staleness(T0 + timedelta(seconds=300))
    assert len(later) == 1
    assert later[0].status is SpotStatus.UNKNOWN
    assert later[0].reason is UnknownReason.STALE
    assert later[0].changed


def test_refresh_keeps_fresh_status():
    stab = Stabilizer()
    _feed(stab, "S-1", [SpotStatus.OCCUPIED] * 3, T0)
    v = stab.refresh_staleness(T0 + timedelta(seconds=40))[0]
    assert v.status is SpotStatus.OCCUPIED


def test_force_unknown_clears_history():
    """После заглушки старые голоса не должны мгновенно воскрешать статус."""
    stab = Stabilizer()
    _feed(stab, "S-1", [SpotStatus.FREE] * 3, T0)

    t = T0 + timedelta(seconds=60)
    v = stab.force_unknown(["S-1"], UnknownReason.SLATE, t)[0]
    assert v.status is SpotStatus.UNKNOWN
    assert v.reason is UnknownReason.SLATE
    assert v.changed

    # Одного наблюдения после восстановления недостаточно для возврата в free.
    back = stab.observe("S-1", _obs(SpotStatus.FREE, t + timedelta(seconds=15)))
    assert back.status is SpotStatus.UNKNOWN


@pytest.mark.parametrize(
    "reason",
    [
        UnknownReason.SLATE,
        UnknownReason.FROZEN,
        UnknownReason.NIGHT,
        UnknownReason.CAMERA_DRIFT,
        UnknownReason.CAMERA_ERROR,
    ],
)
def test_every_unknown_carries_a_reason(reason):
    """«Просто unknown» без причины невозможно отлаживать."""
    stab = Stabilizer()
    v = stab.force_unknown(["S-1"], reason, T0)[0]
    assert v.reason is reason


# --------------------------------------------------------------------------- #
# Сериализация состояния
# --------------------------------------------------------------------------- #


def test_state_survives_restart():
    """Рестарт не должен сбрасывать гистерезис — иначе места замигают заново."""
    stab = Stabilizer()
    _feed(stab, "S-1", [SpotStatus.OCCUPIED] * 3, T0)
    dumped = stab.to_dict()

    restored = Stabilizer()
    restored.load_dict(dumped)
    v = restored.refresh_staleness(T0 + timedelta(seconds=40))[0]
    assert v.status is SpotStatus.OCCUPIED
    assert v.confidence > 0.9


# --------------------------------------------------------------------------- #
# Счётчики зон
# --------------------------------------------------------------------------- #


def test_zone_counts_sum_to_capacity():
    assert zone_counts(42, 33, 2) == (7, 33, 2)
    assert sum(zone_counts(42, 33, 2)) == 42


def test_zone_counts_clamp_impossible_occupancy():
    """occupied > capacity — ошибка конфига; наружу отрицательных чисел не пускаем."""
    free, occupied, unknown = zone_counts(10, 15)
    assert (free, occupied, unknown) == (0, 10, 0)
    assert free >= 0


def test_zone_counts_all_unknown():
    assert zone_counts(20, 0, 20) == (0, 0, 20)
