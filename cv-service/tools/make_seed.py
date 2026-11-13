#!/usr/bin/env python3
"""Генератор сидов для backend из config/cameras.yml.

    python tools/make_seed.py                 # seed/spots.json + seed/zones.json
    python tools/make_seed.py --stdout        # посмотреть, ничего не записывая
    python tools/make_seed.py --include-disabled

`seed/spots.json` — строго по модели ParkingSpot из ../CLAUDE.md
(id, parking_lot_id, camera_id, latitude, longitude, approach_lat, approach_lon,
roi_polygon, spot_type, status, confidence, updated_at) плюс `roi_frame_size`:
без размера эталонного кадра пиксельный roi_polygon бессмысленен, а камеры
у нас разного разрешения (1920x1080 и 2592x1944).

Зоны уходят отдельным файлом `seed/zones.json`: у них нет ID мест, только
полигон, capacity и счётчик — гибридная модель разметки, см. docs/ANNOTATION.md.

Стартовый статус у всего — `unknown` с confidence 0.0. Это осознанно: пока CV
не прислал ни одной детекции, показывать место свободным нельзя.

Зависимости: stdlib + PyYAML (через cvsvc.config). Своей валидации нет —
используется ровно тот же валидатор, что у детектора.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent      # cv-service
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cvsvc.config import Camera, Config, ConfigError, load_config  # noqa: E402

# Владивосток, UTC+10. Время в сидах — момент генерации, а не наблюдения:
# наблюдений ещё не было, поэтому статус unknown.
VLADIVOSTOK_TZ = dt.timezone(dt.timedelta(hours=10))


def iso_now() -> str:
    return dt.datetime.now(VLADIVOSTOK_TZ).replace(microsecond=0).isoformat()


def spot_record(cam: Camera, spot, stamp: str) -> dict:
    return {
        "id": spot.id,
        "parking_lot_id": cam.parking_lot_id,
        "camera_id": cam.id,
        "latitude": spot.lat,
        "longitude": spot.lon,
        "approach_lat": spot.approach.lat,
        "approach_lon": spot.approach.lon,
        "roi_polygon": [[int(round(x)), int(round(y))] for x, y in spot.quad],
        "roi_frame_size": [cam.width, cam.height],
        "spot_type": spot.type,
        "status": "unknown",
        "confidence": 0.0,
        "updated_at": stamp,
    }


def zone_record(cam: Camera, zone, stamp: str) -> dict:
    return {
        "id": zone.id,
        "parking_lot_id": cam.parking_lot_id,
        "camera_id": cam.id,
        "name": zone.name,
        "capacity": zone.capacity,
        "latitude": zone.lat,
        "longitude": zone.lon,
        "entrance_lat": zone.entrance.lat,
        "entrance_lon": zone.entrance.lon,
        "roi_polygon": [[int(round(x)), int(round(y))] for x, y in zone.polygon],
        "roi_frame_size": [cam.width, cam.height],
        "free_count": None,
        "occupied_count": None,
        "status": "unknown",
        "updated_at": stamp,
    }


def build(cfg: Config, include_disabled: bool) -> tuple[dict, dict, int]:
    stamp = iso_now()
    spots: list[dict] = []
    zones: list[dict] = []
    skipped = 0

    for cam in cfg:
        for spot in cam.spots:
            if not spot.enabled and not include_disabled:
                skipped += 1
                continue
            spots.append(spot_record(cam, spot, stamp))
        for zone in cam.zones:
            zones.append(zone_record(cam, zone, stamp))

    meta = {
        "generated_at": stamp,
        "source": "config/cameras.yml",
        "note": "Черновая разметка. Пиксельные ROI заданы в системе эталонного "
                "кадра камеры — см. roi_frame_size. Статусы unknown до первой "
                "детекции CV.",
    }
    return (
        {**meta, "count": len(spots), "spots": spots},
        {**meta, "count": len(zones), "zones": zones},
        skipped,
    )


# Пары координат печатаем в одну строку: иначе roi_polygon из 7 точек
# разворачивается в 21 строку и файл невозможно просматривать глазами.
_PAIR_RE = re.compile(
    r"\[\s*\n\s*(-?\d+(?:\.\d+)?),\s*\n\s*(-?\d+(?:\.\d+)?)\s*\n\s*\]"
)


def dumps(payload: dict) -> str:
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    return _PAIR_RE.sub(r"[\1, \2]", text) + "\n"


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(dumps(payload), encoding="utf-8")
    tmp.replace(path)


def main() -> int:
    ap = argparse.ArgumentParser(description="Сиды парковочных мест и зон для backend")
    ap.add_argument("--config", default=str(ROOT / "config" / "cameras.yml"))
    ap.add_argument("--out", default=str(ROOT / "seed"), help="каталог для spots.json / zones.json")
    ap.add_argument("--include-disabled", action="store_true",
                    help="выгружать и места с enabled: false")
    ap.add_argument("--stdout", action="store_true", help="напечатать, ничего не записывая")
    args = ap.parse_args()

    try:
        cfg = load_config(args.config)
    except ConfigError as err:
        print(f"! {args.config} не прошёл валидацию, сиды не собраны:", file=sys.stderr)
        for p in err.problems:
            print(f"    - {p}", file=sys.stderr)
        return 1
    except FileNotFoundError:
        print(f"! нет файла {args.config}", file=sys.stderr)
        return 1

    spots_doc, zones_doc, skipped = build(cfg, args.include_disabled)

    if args.stdout:
        sys.stdout.write(dumps({"spots": spots_doc, "zones": zones_doc}))
        return 0

    out = Path(args.out)
    write_json(out / "spots.json", spots_doc)
    write_json(out / "zones.json", zones_doc)

    print(f"{out / 'spots.json'}: {spots_doc['count']} мест"
          + (f" (пропущено enabled:false — {skipped})" if skipped else ""))
    print(f"{out / 'zones.json'}: {zones_doc['count']} зон"
          f" (суммарная capacity {sum(z['capacity'] for z in zones_doc['zones'])})")
    if not spots_doc["count"]:
        print("  ! дискретных мест нет — backend получит только зоны", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
