"""Рекордер кадров — задача №1 проекта.

У стримов НЕТ архива (DVR): старые сегменты отдают 403, окно плейлиста ~60 с.
Исторических кадров взять негде. Значит любой датасет — для калибровки
квантизации детектора, для разметки ROI, для замера точности — нужно писать
живьём, начиная прямо сейчас. Чем раньше запущен рекордер, тем больше у нас
вариантов освещения (утро / день / сумерки / ночь) к моменту замера.

Запуск:
    python -m cvsvc.ingest.recorder --config config/cameras.yml --out frames/
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from cvsvc.config import Camera, Config, ConfigError, load_config
from cvsvc.ingest.hls import (
    Frame,
    FrameError,
    FrameErrorKind,
    grab_frame,
    load_slate_reference,
)

log = logging.getLogger("recorder")

DEFAULT_INTERVAL_S = 20
DEFAULT_MAX_AGE_DAYS = 7
DEFAULT_MAX_TOTAL_MB = 20_000


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


@dataclass
class CameraStats:
    saved: int = 0
    failed: int = 0
    bytes_written: int = 0
    consecutive_failures: int = 0
    last_error: str = ""
    # Хеш предыдущего кадра БЕЗ оверлеев — детект зависшей картинки.
    last_content_hash: int | None = None
    frozen_streak: int = 0

    def as_dict(self) -> dict:
        return {
            "saved": self.saved,
            "failed": self.failed,
            "mb_written": round(self.bytes_written / 1e6, 1),
            "consecutive_failures": self.consecutive_failures,
            "frozen_streak": self.frozen_streak,
            "last_error": self.last_error,
        }


@dataclass
class Recorder:
    config: Config
    out_dir: Path
    interval_s: int = DEFAULT_INTERVAL_S
    max_age_days: int = DEFAULT_MAX_AGE_DAYS
    max_total_mb: int = DEFAULT_MAX_TOTAL_MB
    slate_hash: int | None = None
    stats: dict[str, CameraStats] = field(default_factory=dict)
    _stop: bool = False

    def __post_init__(self) -> None:
        for cam in self.config:
            self.stats.setdefault(cam.id, CameraStats())

    def stop(self, *_: object) -> None:
        log.info("получен сигнал остановки, доработаем текущий цикл")
        self._stop = True

    # ------------------------------------------------------------------ #

    def frame_path(self, cam: Camera, ts: float) -> Path:
        lt = time.localtime(ts)
        return (
            self.out_dir
            / cam.id
            / time.strftime("%Y%m%d", lt)
            / f"{time.strftime('%H%M%S', lt)}.jpg"
        )

    def capture_one(self, cam: Camera) -> Frame | None:
        st = self.stats[cam.id]
        dest = self.frame_path(cam, time.time())
        try:
            frame = grab_frame(cam, dest, slate_hash=self.slate_hash)
        except FrameError as exc:
            st.failed += 1
            st.consecutive_failures += 1
            st.last_error = f"{exc.kind.value}: {exc.detail}"
            level = logging.ERROR if exc.kind is FrameErrorKind.SLATE else logging.WARNING
            log.log(
                level,
                "camera=%s capture_failed kind=%s streak=%d detail=%s",
                cam.id, exc.kind.value, st.consecutive_failures, exc.detail,
            )
            return None

        st.consecutive_failures = 0
        st.last_error = ""
        st.saved += 1
        st.bytes_written += frame.size_bytes

        # Зависшая картинка: хеш БЕЗ оверлеев не меняется.
        # По полному кадру это не поймать — таймкод тикает каждую секунду.
        if st.last_content_hash is not None and frame.content_hash == st.last_content_hash:
            st.frozen_streak += 1
            if st.frozen_streak in (3, 10, 50):
                log.warning(
                    "camera=%s картинка не меняется %d циклов подряд — возможно поток завис",
                    cam.id, st.frozen_streak,
                )
        else:
            st.frozen_streak = 0
        st.last_content_hash = frame.content_hash

        log.info(
            "camera=%s saved path=%s size_kb=%d %dx%d",
            cam.id, frame.path, frame.size_bytes // 1024, frame.width, frame.height,
        )
        return frame

    # ------------------------------------------------------------------ #

    def prune(self) -> None:
        """Удаляет старые кадры: сначала по возрасту, затем по суммарному объёму."""
        if not self.out_dir.exists():
            return
        files = sorted(self.out_dir.rglob("*.jpg"), key=lambda p: p.stat().st_mtime)
        if not files:
            return

        cutoff = time.time() - self.max_age_days * 86400
        removed = 0
        kept: list[Path] = []
        for f in files:
            if f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)
                removed += 1
            else:
                kept.append(f)

        total = sum(f.stat().st_size for f in kept if f.exists())
        limit = self.max_total_mb * 1_000_000
        idx = 0
        while total > limit and idx < len(kept):
            f = kept[idx]
            if f.exists():
                total -= f.stat().st_size
                f.unlink(missing_ok=True)
                removed += 1
            idx += 1

        if removed:
            log.info("prune removed=%d total_mb=%.1f", removed, total / 1e6)

    def run(self) -> int:
        log.info(
            "recorder start cameras=%s interval=%ds out=%s",
            [c.id for c in self.config], self.interval_s, self.out_dir,
        )
        cycle = 0
        while not self._stop:
            started = time.monotonic()
            # Камеры обрабатываем ПОСЛЕДОВАТЕЛЬНО: на 4 ГБ нельзя держать три
            # буфера декода 2592x1944 одновременно.
            for cam in self.config:
                if self._stop:
                    break
                self.capture_one(cam)

            cycle += 1
            if cycle % 30 == 0:
                self.prune()
                log.info(
                    "stats %s",
                    json.dumps(
                        {cid: s.as_dict() for cid, s in self.stats.items()},
                        ensure_ascii=False,
                    ),
                )

            elapsed = time.monotonic() - started
            sleep_for = max(0.0, self.interval_s - elapsed)
            if elapsed > self.interval_s:
                log.warning(
                    "цикл занял %.1f с при интервале %d с — не успеваем",
                    elapsed, self.interval_s,
                )
            deadline = time.monotonic() + sleep_for
            while not self._stop and time.monotonic() < deadline:
                time.sleep(min(0.5, deadline - time.monotonic()))

        log.info(
            "recorder stop stats=%s",
            json.dumps({cid: s.as_dict() for cid, s in self.stats.items()}, ensure_ascii=False),
        )
        return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Рекордер кадров ParkVision")
    ap.add_argument("--config", default="config/cameras.yml")
    ap.add_argument("--out", default="frames")
    ap.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_S)
    ap.add_argument("--max-age-days", type=int, default=DEFAULT_MAX_AGE_DAYS)
    ap.add_argument("--max-total-mb", type=int, default=DEFAULT_MAX_TOTAL_MB)
    ap.add_argument("--slate-ref", default="refs/slate.ahash")
    ap.add_argument("--once", action="store_true", help="один цикл и выход (для проверки)")
    args = ap.parse_args(argv)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)

    try:
        # Рекордеру ROI не нужны: он стартует до того, как появится разметка.
        config = load_config(args.config, require_rois=False)
    except ConfigError as exc:
        log.error("%s", exc)
        return 2
    except FileNotFoundError:
        log.error("конфиг не найден: %s", args.config)
        return 2

    rec = Recorder(
        config=config,
        out_dir=Path(args.out),
        interval_s=args.interval,
        max_age_days=args.max_age_days,
        max_total_mb=args.max_total_mb,
        slate_hash=load_slate_reference(args.slate_ref),
    )
    if rec.slate_hash is None:
        log.warning(
            "эталон заглушки %s не найден — детект работает только по пути "
            "/static/slate/ в URI сегмента. Сгенерируйте: python -m cvsvc.ingest.make_slate_ref",
            args.slate_ref,
        )

    signal.signal(signal.SIGINT, rec.stop)
    signal.signal(signal.SIGTERM, rec.stop)

    if args.once:
        for cam in config:
            rec.capture_one(cam)
        log.info(
            "once stats=%s",
            json.dumps({c: s.as_dict() for c, s in rec.stats.items()}, ensure_ascii=False),
        )
        return 0 if all(s.saved for s in rec.stats.values()) else 1

    return rec.run()


if __name__ == "__main__":
    raise SystemExit(main())
