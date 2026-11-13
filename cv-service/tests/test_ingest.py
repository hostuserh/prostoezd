"""Тесты A1: конфиг, разбор плейлиста, детект заглушки, хеши.

Сетевых вызовов нет — всё на зафиксированных фикстурах, снятых с живых камер
13.11.2026.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from cvsvc.config import ConfigError, load_config
from cvsvc.ingest.hls import (
    HASH_SIDE,
    SLATE_HASH_MAX_DISTANCE,
    average_hash,
    hamming,
    mean_luma,
    parse_playlist,
    _jpeg_dimensions,
    _masked_gray,
)

BASE = "https://stream.vladlink.ru/embed/acc/cam/index.m3u8"

# Реальный ответ с корректным Referer: https://vladlink.ru/
LIVE_PLAYLIST = """\
#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:10
#EXT-X-MEDIA-SEQUENCE:1792998809
#EXT-X-DISCONTINUITY-SEQUENCE:4
#EXTINF:5.940,
https://stream.vladlink.ru/embed/acc/cam/segments/aHR0cDovL2hsczcudmxhZGxpbmsucnUv
#EXTINF:5.940,
https://stream.vladlink.ru/embed/acc/cam/segments/aHR0cDovL2hsczcudmxhZGxpbmsucnUx
"""

# Реальный ответ БЕЗ Referer: HTTP 200, валидное видео, но это заглушка
# «Трансляция заблокирована для данного сайта».
SLATE_PLAYLIST = """\
#EXTM3U
#EXT-X-VERSION:3
#EXT-X-KEY:METHOD=NONE
#EXT-X-TARGETDURATION:10
#EXT-X-MEDIA-SEQUENCE:1792998794
#EXT-X-DISCONTINUITY-SEQUENCE:4
#EXT-X-DISCONTINUITY
#EXTINF:4.0,
https://stream.vladlink.ru/static/slate/site.ts?seq=1792998794
#EXT-X-DISCONTINUITY
#EXTINF:4.0,
https://stream.vladlink.ru/static/slate/site.ts?seq=1792998795
"""


# --------------------------------------------------------------------------- #
# Плейлист
# --------------------------------------------------------------------------- #


def test_parse_live_playlist():
    pl = parse_playlist(LIVE_PLAYLIST, BASE)
    assert len(pl.segments) == 2
    assert pl.media_sequence == 1792998809
    assert pl.segments[0].duration == pytest.approx(5.94)
    assert not pl.is_slate
    assert not any(s.is_slate for s in pl.segments)


def test_parse_slate_playlist_is_detected():
    """Главный капкан проекта: HTTP 200 + валидное видео, но это заглушка."""
    pl = parse_playlist(SLATE_PLAYLIST, BASE)
    assert len(pl.segments) == 2
    assert pl.is_slate
    assert all(s.is_slate for s in pl.segments)


def test_relative_segment_uris_resolved():
    pl = parse_playlist("#EXTM3U\n#EXTINF:4.0,\nsegments/abc\n", BASE)
    assert pl.segments[0].uri == "https://stream.vladlink.ru/embed/acc/cam/segments/abc"


def test_segment_durations_vary_between_cameras():
    """Длительность плавает 4.0–7.98 с — фиксированную каденцию закладывать нельзя."""
    pl = parse_playlist("#EXTM3U\n#EXTINF:7.980,\na\n#EXTINF:4.0,\nb\n", BASE)
    assert [s.duration for s in pl.segments] == [pytest.approx(7.98), pytest.approx(4.0)]


def test_empty_playlist_has_no_segments():
    assert parse_playlist("#EXTM3U\n#EXT-X-VERSION:3\n", BASE).segments == []
    # пустой плейлист не считается заглушкой — это другая ошибка
    assert not parse_playlist("#EXTM3U\n", BASE).is_slate


# --------------------------------------------------------------------------- #
# Хеши и детект заглушки по содержимому
# --------------------------------------------------------------------------- #


def test_average_hash_distinguishes_images():
    black = bytes(HASH_SIDE * HASH_SIDE)
    noisy = bytes((i * 37) % 256 for i in range(HASH_SIDE * HASH_SIDE))
    assert hamming(average_hash(black), average_hash(noisy)) > SLATE_HASH_MAX_DISTANCE


def test_average_hash_is_stable_for_same_image():
    g = bytes((i * 11) % 256 for i in range(HASH_SIDE * HASH_SIDE))
    assert average_hash(g) == average_hash(g)


def test_real_slate_reference_is_near_black():
    """refs/slate.jpg снят с живого потока без Referer. Заглушка почти чёрная."""
    ref = Path(__file__).resolve().parents[1] / "refs" / "slate.ahash"
    if not ref.exists():
        pytest.skip("refs/slate.ahash не сгенерирован (python -m cvsvc.ingest.make_slate_ref)")
    h = int(ref.read_text().strip(), 16)
    # У почти чёрного кадра с текстом светлых пикселей мало.
    assert 0 < h.bit_count() < HASH_SIDE * HASH_SIDE // 2


def test_mean_luma():
    assert mean_luma(bytes([0] * 16)) == 0.0
    assert mean_luma(bytes([255] * 16)) == 255.0
    assert mean_luma(b"") == 0.0


# --------------------------------------------------------------------------- #
# Маскирование оверлеев — детект зависшей картинки
# --------------------------------------------------------------------------- #


def _camera(tmp_path: Path, overlay: str) -> object:
    cfg = tmp_path / "c.yml"
    cfg.write_text(
        textwrap.dedent(
            f"""
            version: 1
            defaults:
              headers:
                Referer: "https://vladlink.ru/"
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


def test_masked_gray_zeroes_overlay_region(tmp_path):
    cam = _camera(tmp_path, "[[0, 0, 620, 130]]")
    gray = bytes([200] * (HASH_SIDE * HASH_SIDE))
    masked = _masked_gray(gray, cam)
    assert masked[0] == 0  # верхний левый угол — под таймкодом
    assert masked[-1] == 200  # нижний правый — нетронут


def test_ticking_timestamp_does_not_hide_frozen_picture(tmp_path):
    """⚠️ Ключевой баг, который легко пропустить.

    Таймкод в углу кадра тикает каждую секунду даже когда картинка замерла.
    Хеш по ПОЛНОМУ кадру поэтому всегда разный, и зависание не детектится.
    Хеш по кадру БЕЗ оверлеев обязан совпасть.
    """
    cam = _camera(tmp_path, "[[0, 0, 620, 130]]")
    frame_a = bytearray([128] * (HASH_SIDE * HASH_SIDE))
    frame_b = bytearray(frame_a)
    frame_b[0] = 255  # изменился только таймкод
    frame_b[1] = 250

    assert average_hash(bytes(frame_a)) != average_hash(bytes(frame_b))
    assert average_hash(_masked_gray(bytes(frame_a), cam)) == average_hash(
        _masked_gray(bytes(frame_b), cam)
    )


# --------------------------------------------------------------------------- #
# Размеры JPEG — ловим смену разрешения потока
# --------------------------------------------------------------------------- #


def test_jpeg_dimensions_from_real_frames():
    refs = Path(__file__).resolve().parents[1] / "refs"
    for name, expected in (("CAM-001.jpg", (1920, 1080)), ("CAM-003.jpg", (2592, 1944))):
        p = refs / name
        if not p.exists():
            pytest.skip(f"{p} не снят")
        assert _jpeg_dimensions(p.read_bytes()) == expected


def test_jpeg_dimensions_on_garbage():
    assert _jpeg_dimensions(b"\xff\xd8not a jpeg at all") is None


# --------------------------------------------------------------------------- #
# Валидация конфига
# --------------------------------------------------------------------------- #


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "c.yml"
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return p


def test_real_config_loads():
    root = Path(__file__).resolve().parents[1]
    cfg = load_config(root / "config" / "cameras.yml", require_rois=False)
    assert {c.id for c in cfg} == {"CAM-001", "CAM-002", "CAM-003"}
    assert cfg.camera("CAM-003").frame_size == (2592, 1944)
    # Referer обязателен на всех камерах — иначе молча приедет заглушка.
    assert all(c.headers.get("Referer") == "https://vladlink.ru/" for c in cfg)


def test_missing_referer_is_an_error(tmp_path):
    p = _write(
        tmp_path,
        """
        version: 1
        cameras:
          - id: CAM-T
            playlist_url: "https://example.com/i.m3u8"
            frame_size: [1920, 1080]
            spots: []
            zones: []
        """,
    )
    with pytest.raises(ConfigError) as ei:
        load_config(p, require_rois=False)
    assert any("Referer" in msg for msg in ei.value.problems)


def test_tiny_spot_is_rejected(tmp_path):
    """Квад меньше 28 px — детектор машину не увидит, такое место брать нельзя."""
    p = _write(
        tmp_path,
        """
        version: 1
        defaults:
          headers: {Referer: "https://vladlink.ru/"}
        cameras:
          - id: CAM-T
            playlist_url: "https://example.com/i.m3u8"
            frame_size: [1920, 1080]
            spots:
              - id: S-T
                quad: [[100,100],[115,100],[115,115],[100,115]]
                lat: 43.17
                lon: 131.98
                approach: {lat: 43.17, lon: 131.98}
        """,
    )
    with pytest.raises(ConfigError) as ei:
        load_config(p)
    assert any("детектор машину не увидит" in m for m in ei.value.problems)


def test_placeholder_coords_from_claude_md_are_flagged(tmp_path):
    """43.115141/131.885341 — пример Clover House из спеки, не наши камеры."""
    p = _write(
        tmp_path,
        """
        version: 1
        defaults:
          headers: {Referer: "https://vladlink.ru/"}
        cameras:
          - id: CAM-T
            playlist_url: "https://example.com/i.m3u8"
            frame_size: [1920, 1080]
            zones:
              - id: Z-T
                polygon: [[0,0],[100,0],[100,100]]
                capacity: 10
                lat: 55.75
                lon: 37.61
                entrance: {lat: 55.75, lon: 37.61}
        """,
    )
    with pytest.raises(ConfigError) as ei:
        load_config(p)
    assert any("вне Владивостока" in m for m in ei.value.problems)


def test_duplicate_spot_ids_rejected(tmp_path):
    p = _write(
        tmp_path,
        """
        version: 1
        defaults:
          headers: {Referer: "https://vladlink.ru/"}
        cameras:
          - id: CAM-A
            playlist_url: "https://example.com/a.m3u8"
            frame_size: [1920, 1080]
            spots:
              - id: S-1
                quad: [[10,10],[120,10],[120,70],[10,70]]
                lat: 43.17
                lon: 131.98
                approach: {lat: 43.17, lon: 131.98}
          - id: CAM-B
            playlist_url: "https://example.com/b.m3u8"
            frame_size: [1920, 1080]
            spots:
              - id: S-1
                quad: [[10,10],[120,10],[120,70],[10,70]]
                lat: 43.17
                lon: 131.98
                approach: {lat: 43.17, lon: 131.98}
        """,
    )
    with pytest.raises(ConfigError) as ei:
        load_config(p)
    assert any("неуникальные id мест" in m for m in ei.value.problems)


def test_roi_outside_frame_rejected(tmp_path):
    p = _write(
        tmp_path,
        """
        version: 1
        defaults:
          headers: {Referer: "https://vladlink.ru/"}
        cameras:
          - id: CAM-T
            playlist_url: "https://example.com/i.m3u8"
            frame_size: [1920, 1080]
            spots:
              - id: S-1
                quad: [[10,10],[2600,10],[2600,900],[10,900]]
                lat: 43.17
                lon: 131.98
                approach: {lat: 43.17, lon: 131.98}
        """,
    )
    with pytest.raises(ConfigError) as ei:
        load_config(p)
    assert any("вне кадра" in m for m in ei.value.problems)


def test_all_problems_reported_at_once(tmp_path):
    """Валидатор обязан отдавать весь список, а не первую ошибку."""
    p = _write(
        tmp_path,
        """
        version: 1
        cameras:
          - id: CAM-T
            playlist_url: "https://example.com/i.m3u8"
            frame_size: [1920, 1080]
            zones:
              - id: Z-T
                polygon: [[0,0],[100,0],[100,100]]
                capacity: -5
                lat: 55.0
                lon: 37.0
                entrance: {lat: 55.0, lon: 37.0}
        """,
    )
    with pytest.raises(ConfigError) as ei:
        load_config(p)
    assert len(ei.value.problems) >= 3  # Referer + capacity + координаты
