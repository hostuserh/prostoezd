"""Забор кадров из HLS stream.vladlink.ru.

Разведка, на которой построен этот модуль (проверено вживую 13.11.2026):

1. Плейлист лежит по `/embed/{account}/{camera}/index.m3u8`.

2. ⚠️ БЕЗ заголовка `Referer: https://vladlink.ru/` сервер отдаёт HTTP 200 и
   валидное h264 1920x1080 видео — но это ЗАГЛУШКА: чёрный кадр с текстом
   «Трансляция заблокирована для данного сайта» (`/static/slate/site.ts`).
   Никакой ошибки. Поэтому детект заглушки обязателен: иначе сервис будет
   честно рапортовать «все места свободны» по чёрному кадру.

3. ⚠️ Архива (DVR) нет: старые сегменты отдают 403, окно плейлиста ~60 с
   (9 сегментов). Исторических кадров взять негде — отсюда рекордер.

4. ⚠️ URI сегментов НЕ заканчиваются на `.ts` (это base64 от origin-URL),
   поэтому ffmpeg с самим плейлистом падает на `allowed_extensions`.
   Мы это обходим радикально: плейлист парсим сами, ffmpeg скармливаем
   уже скачанный сегмент.

5. Длительность сегментов плавает (4.0–7.98 с на разных камерах) —
   фиксированную каденцию не закладываем, плейлист перечитываем каждый цикл.

6. Экономия на слабом сервере: постоянный ffmpeg-декодер на 3x1080p25 съедает
   1.5–2 ядра. Вместо этого берём ТОЛЬКО последний сегмент и только первые
   ~500 КБ через HTTP Range — сервер отдаёт 206, и первого кейфрейма хватает
   на полный кадр (проверено на 2592x1944). Замер: 0.32 с CPU, ~0.5 МБ на кадр.
"""

from __future__ import annotations

import logging
import os
import random
import ssl
import struct
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from urllib.parse import urljoin

from cvsvc.config import Camera

log = logging.getLogger(__name__)

# Владивосток, UTC+10. Время кадра отдаём backend'у в этой зоне (см. CLAUDE.md).
VLADIVOSTOK_TZ = timezone(timedelta(hours=10), "VLAT")

SLATE_PATH_MARKER = "/static/slate/"
HASH_SIDE = 16  # 16x16 gray -> 256 байт -> 256-битный aHash
SLATE_HASH_MAX_DISTANCE = 24  # расстояние Хэмминга до эталона заглушки
DARK_MEAN_LUMA = 40  # ниже — кадр почти чёрный (ночь или заглушка)


class FrameErrorKind(str, Enum):
    NETWORK = "network"          # не достучались до плейлиста/сегмента
    EMPTY_PLAYLIST = "empty_playlist"
    SLATE = "slate"              # «Трансляция заблокирована» — доступ потерян
    DECODE = "decode"            # ffmpeg не смог достать кадр
    RESOLUTION = "resolution"    # поток отдал не то разрешение, что в конфиге


class FrameError(Exception):
    def __init__(self, kind: FrameErrorKind, detail: str, camera_id: str) -> None:
        self.kind = kind
        self.detail = detail
        self.camera_id = camera_id
        super().__init__(f"[{camera_id}] {kind.value}: {detail}")


@dataclass
class SegmentRef:
    uri: str
    duration: float
    is_slate: bool


@dataclass
class Playlist:
    segments: list[SegmentRef]
    media_sequence: int

    @property
    def is_slate(self) -> bool:
        """Весь плейлист — заглушка. Значит Referer не принят или доступ отозван."""
        return bool(self.segments) and all(s.is_slate for s in self.segments)


@dataclass
class Frame:
    camera_id: str
    path: Path
    captured_at: datetime
    width: int
    height: int
    ahash: int          # 256-битный average hash по кадру целиком
    content_hash: int   # то же, но БЕЗ областей оверлеев — для детекта зависания
    size_bytes: int
    gray: bytes         # 16x16 grayscale: дешёвая основа для оценок яркости/контраста

    @property
    def mean_luma(self) -> float:
        """Средняя яркость 0..255. Ночью проваливается — повод уйти в unknown."""
        return mean_luma(self.gray)

    @property
    def contrast(self) -> float:
        """Стандартное отклонение яркости. Туман, снег, залепленный объектив → близко к 0."""
        if not self.gray:
            return 0.0
        m = self.mean_luma
        return (sum((p - m) ** 2 for p in self.gray) / len(self.gray)) ** 0.5


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #


def _ssl_context() -> ssl.SSLContext:
    """Контекст TLS с рабочим хранилищем корневых сертификатов.

    Сборки Python с python.org приезжают без установленного CA-бандла, и тогда
    `urlopen` падает на CERTIFICATE_VERIFY_FAILED на совершенно валидном
    сертификате. Поэтому берём certifi, если он есть; иначе — системный
    дефолт (в Docker-образе он на месте). Переменные SSL_CERT_FILE / SSL_CERT_DIR
    по-прежнему работают и имеют приоритет.
    """
    if os.environ.get("SSL_CERT_FILE") or os.environ.get("SSL_CERT_DIR"):
        return ssl.create_default_context()
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


_SSL_CTX = _ssl_context()


def _request(
    url: str,
    headers: dict[str, str],
    timeout: float,
    byte_range: int | None = None,
) -> bytes:
    req = urllib.request.Request(url, headers=dict(headers))
    if byte_range:
        req.add_header("Range", f"bytes=0-{byte_range - 1}")
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
        return resp.read()


def _fetch_with_retries(
    url: str,
    headers: dict[str, str],
    camera_id: str,
    timeout: float = 10.0,
    byte_range: int | None = None,
    attempts: int = 3,
) -> bytes:
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return _request(url, headers, timeout, byte_range)
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
            last = exc
            if attempt < attempts - 1:
                # экспоненциальный бэкофф с джиттером
                delay = (2**attempt) * 0.5 + random.uniform(0, 0.3)
                log.warning(
                    "camera=%s url_fetch_retry attempt=%d/%d delay=%.2fs err=%s",
                    camera_id, attempt + 1, attempts, delay, exc,
                )
                time.sleep(delay)
    raise FrameError(FrameErrorKind.NETWORK, f"{url}: {last}", camera_id)


# --------------------------------------------------------------------------- #
# Плейлист
# --------------------------------------------------------------------------- #


def parse_playlist(text: str, base_url: str) -> Playlist:
    """Разбирает m3u8. Относительные URI разрешаются относительно base_url."""
    segments: list[SegmentRef] = []
    media_sequence = 0
    duration = 0.0

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            try:
                media_sequence = int(line.split(":", 1)[1])
            except ValueError:
                pass
            continue
        if line.startswith("#EXTINF:"):
            try:
                duration = float(line.split(":", 1)[1].rstrip(",").split(",")[0])
            except ValueError:
                duration = 0.0
            continue
        if line.startswith("#"):
            continue
        uri = urljoin(base_url, line)
        segments.append(
            SegmentRef(uri=uri, duration=duration, is_slate=SLATE_PATH_MARKER in uri)
        )
        duration = 0.0

    return Playlist(segments=segments, media_sequence=media_sequence)


def fetch_playlist(cam: Camera, timeout: float = 10.0) -> Playlist:
    body = _fetch_with_retries(cam.playlist_url, cam.headers, cam.id, timeout=timeout)
    pl = parse_playlist(body.decode("utf-8", errors="replace"), cam.playlist_url)
    if not pl.segments:
        raise FrameError(FrameErrorKind.EMPTY_PLAYLIST, "в плейлисте нет сегментов", cam.id)
    return pl


# --------------------------------------------------------------------------- #
# Декодирование и хеши
# --------------------------------------------------------------------------- #


def _jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    """Разбирает SOF-маркер JPEG. Нужно, чтобы поймать смену разрешения потока."""
    i = 2
    n = len(data)
    while i + 9 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB):
            height, width = struct.unpack(">HH", data[i + 5 : i + 9])
            return int(width), int(height)
        if marker == 0xD8 or 0xD0 <= marker <= 0xD9:
            i += 2
            continue
        length = int.from_bytes(data[i + 2 : i + 4], "big")
        if length <= 0:
            return None
        i += 2 + length
    return None


def average_hash(gray: bytes) -> int:
    """aHash по сырому 16x16 grayscale: бит на пиксель относительно среднего."""
    if not gray:
        return 0
    mean = sum(gray) / len(gray)
    bits = 0
    for i, px in enumerate(gray):
        if px > mean:
            bits |= 1 << i
    return bits


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def mean_luma(gray: bytes) -> float:
    return sum(gray) / len(gray) if gray else 0.0


def _masked_gray(gray: bytes, cam: Camera) -> bytes:
    """Зануляет области оверлеев в уменьшенной копии кадра.

    ⚠️ Таймкод в левом верхнем углу тикает КАЖДУЮ СЕКУНДУ даже когда картинка
    замерла. Если считать хеш по полному кадру, зависание камеры никогда не
    задетектится. Поэтому content_hash считаем по кадру без оверлеев.
    """
    if not cam.overlay_exclude:
        return gray
    buf = bytearray(gray)
    sx = HASH_SIDE / cam.width
    sy = HASH_SIDE / cam.height
    for x1, y1, x2, y2 in cam.overlay_exclude:
        for ty in range(int(y1 * sy), min(HASH_SIDE, int(y2 * sy) + 1)):
            for tx in range(int(x1 * sx), min(HASH_SIDE, int(x2 * sx) + 1)):
                buf[ty * HASH_SIDE + tx] = 0
    return bytes(buf)


def decode_frame(segment: bytes, dest: Path, camera_id: str) -> tuple[bytes, bytes]:
    """Один проход ffmpeg: JPEG на диск + 16x16 gray в stdout для хеширования.

    Возвращает (jpeg_bytes, gray_bytes). Падает FrameError(DECODE), если кадра нет.
    """
    with tempfile.NamedTemporaryFile(suffix=".ts", delete=True) as tmp:
        tmp.write(segment)
        tmp.flush()
        dest.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            "ffmpeg", "-v", "error", "-nostdin",
            "-i", tmp.name,
            # выход 1: кадр на диск
            "-frames:v", "1", "-q:v", "3", "-y", str(dest),
            # выход 2: 16x16 grayscale в stdout — для aHash без numpy
            "-frames:v", "1",
            "-vf", f"scale={HASH_SIDE}:{HASH_SIDE},format=gray",
            "-f", "rawvideo", "-y", "pipe:1",
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=30, check=False)
        except subprocess.TimeoutExpired as exc:
            raise FrameError(FrameErrorKind.DECODE, f"ffmpeg timeout: {exc}", camera_id) from exc

    if not dest.exists() or dest.stat().st_size == 0:
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()[:400]
        raise FrameError(FrameErrorKind.DECODE, f"ffmpeg не выдал кадр: {stderr}", camera_id)

    gray = proc.stdout[: HASH_SIDE * HASH_SIDE]
    return dest.read_bytes(), gray


# --------------------------------------------------------------------------- #
# Главная точка входа
# --------------------------------------------------------------------------- #


def load_slate_reference(path: str | Path) -> int | None:
    """Загружает заранее посчитанный aHash заглушки из текстового файла."""
    p = Path(path)
    if not p.exists():
        return None
    try:
        return int(p.read_text(encoding="utf-8").strip(), 16)
    except ValueError:
        log.warning("не разобрал эталон заглушки %s", p)
        return None


def grab_frame(
    cam: Camera,
    dest: Path,
    slate_hash: int | None = None,
    timeout: float = 10.0,
) -> Frame:
    """Берёт один свежий кадр камеры. Поднимает FrameError на любой проблеме.

    Порядок: плейлист -> последний сегмент -> Range 0..N -> ffmpeg -> проверки.
    """
    playlist = fetch_playlist(cam, timeout=timeout)

    if playlist.is_slate:
        raise FrameError(
            FrameErrorKind.SLATE,
            "плейлист состоит из /static/slate/ — Referer не принят или доступ отозван. "
            "Нужен заголовок Referer: https://vladlink.ru/",
            cam.id,
        )

    last = playlist.segments[-1]
    if last.is_slate:
        raise FrameError(FrameErrorKind.SLATE, f"последний сегмент — заглушка: {last.uri}", cam.id)

    captured_at = datetime.now(VLADIVOSTOK_TZ)

    # Сначала частичная загрузка. Если кейфрейма в ней не оказалось — полный сегмент.
    try:
        chunk = _fetch_with_retries(
            last.uri, cam.headers, cam.id, timeout=timeout, byte_range=cam.range_bytes
        )
        jpeg, gray = decode_frame(chunk, dest, cam.id)
    except FrameError as exc:
        if exc.kind is not FrameErrorKind.DECODE:
            raise
        log.info(
            "camera=%s частичный сегмент не декодировался (%d КБ), берём целиком",
            cam.id, cam.range_bytes // 1024,
        )
        full = _fetch_with_retries(last.uri, cam.headers, cam.id, timeout=timeout)
        jpeg, gray = decode_frame(full, dest, cam.id)

    # Детект заглушки по содержимому: сегмент мог быть не с путём /static/slate/,
    # но по картинке всё равно оказаться чёрным экраном блокировки.
    if slate_hash is not None and hamming(average_hash(gray), slate_hash) <= SLATE_HASH_MAX_DISTANCE:
        dest.unlink(missing_ok=True)
        raise FrameError(
            FrameErrorKind.SLATE,
            f"кадр совпал с эталоном заглушки (refs/slate.ahash)",
            cam.id,
        )

    dims = _jpeg_dimensions(jpeg)
    if dims is None:
        raise FrameError(FrameErrorKind.DECODE, "не разобрал размеры JPEG", cam.id)
    width, height = dims

    if (width, height) != cam.frame_size:
        # Не фатально для рекордера, но вся разметка ROI завязана на frame_size.
        raise FrameError(
            FrameErrorKind.RESOLUTION,
            f"поток отдал {width}x{height}, в конфиге {cam.width}x{cam.height} — "
            f"разметка ROI не применима без пересчёта",
            cam.id,
        )

    return Frame(
        camera_id=cam.id,
        path=dest,
        captured_at=captured_at,
        width=width,
        height=height,
        ahash=average_hash(gray),
        content_hash=average_hash(_masked_gray(gray, cam)),
        size_bytes=len(jpeg),
        gray=gray,
    )
