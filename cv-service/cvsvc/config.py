"""Загрузка и валидация config/cameras.yml — источника правды по разметке ROI.

Зависимости: только stdlib + PyYAML. Рекордер должен запускаться на голом сервере
в первый час, до того как появятся numpy/onnxruntime.

Все пиксельные координаты в конфиге заданы в системе эталонного кадра камеры
(`frame_size`). Если поток сменит разрешение, координаты масштабируются —
см. Camera.scale_to().
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

import yaml

# Минимальная длинная сторона квада места в пикселях эталонного кадра.
# Меньше — детектор машину не увидит, место брать нельзя.
MIN_SPOT_LONG_SIDE_PX = 28.0

# Владивосток. Координаты вне этой рамки почти наверняка опечатка
# (в CLAUDE.md примеры с Clover House — заглушка, не наши камеры).
VLADIVOSTOK_BBOX = (42.8, 131.5, 43.5, 132.3)  # lat_min, lon_min, lat_max, lon_max

SPOT_TYPES = frozenset({"standard", "disabled", "electric"})

Point = tuple[float, float]


class ConfigError(Exception):
    """Конфиг невалиден. Содержит полный список проблем, а не первую попавшуюся."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__(
            "config/cameras.yml невалиден:\n"
            + "\n".join(f"  - {p}" for p in problems)
        )


@dataclass(frozen=True)
class GeoPoint:
    lat: float
    lon: float


@dataclass
class Spot:
    """Дискретное парковочное место. Используем только там, где на асфальте есть краска."""

    id: str
    quad: list[Point]  # ровно 4 точки, наземный след, обход по часовой
    lat: float
    lon: float
    approach: GeoPoint
    type: str = "standard"
    enabled: bool = True
    notes: str = ""

    @property
    def long_side_px(self) -> float:
        sides = [
            math.dist(self.quad[i], self.quad[(i + 1) % 4]) for i in range(4)
        ]
        return max(sides)

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        xs = [p[0] for p in self.quad]
        ys = [p[1] for p in self.quad]
        return min(xs), min(ys), max(xs), max(ys)


@dataclass
class Zone:
    """Площадка без разметки на асфальте: отдаём capacity и счётчик, а не выдуманные ID мест."""

    id: str
    name: str
    polygon: list[Point]  # >= 3 точек
    capacity: int
    lat: float
    lon: float
    entrance: GeoPoint

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        xs = [p[0] for p in self.polygon]
        ys = [p[1] for p in self.polygon]
        return min(xs), min(ys), max(xs), max(ys)


@dataclass
class Camera:
    id: str
    name: str
    parking_lot_id: str
    playlist_url: str
    frame_size: tuple[int, int]
    headers: dict[str, str]
    poll_interval_s: int = 15
    range_bytes: int = 500_000
    reference_frame: str | None = None
    # Области оверлеев (таймкод, вотермарка): исключаются из детекта И из freeze-хеша.
    # Без этого тикающий таймкод маскирует зависшую картинку.
    overlay_exclude: list[tuple[int, int, int, int]] = field(default_factory=list)
    # Тайлы для детектора — только по областям парковок, не по всему кадру.
    tiles: list[tuple[int, int, int, int]] = field(default_factory=list)
    # Косой ракурс: корпус машины «уезжает» вверх от её следа на земле.
    # Сдвиг нижнего центра бокса вниз, в пикселях эталонного кадра.
    footprint_offset_px: float = 0.0
    spots: list[Spot] = field(default_factory=list)
    zones: list[Zone] = field(default_factory=list)

    @property
    def width(self) -> int:
        return self.frame_size[0]

    @property
    def height(self) -> int:
        return self.frame_size[1]

    def scale_factor(self, actual: tuple[int, int]) -> tuple[float, float]:
        """Коэффициенты пересчёта ROI, если поток отдал не то разрешение, что в конфиге."""
        return actual[0] / self.frame_size[0], actual[1] / self.frame_size[1]


@dataclass
class Config:
    version: int
    cameras: list[Camera]
    source_path: Path

    def camera(self, camera_id: str) -> Camera:
        for cam in self.cameras:
            if cam.id == camera_id:
                return cam
        raise KeyError(f"камера {camera_id!r} не найдена в {self.source_path}")

    def __iter__(self) -> Iterator[Camera]:
        return iter(self.cameras)


# --------------------------------------------------------------------------- #
# Парсинг
# --------------------------------------------------------------------------- #


def _points(raw: Any, where: str, problems: list[str]) -> list[Point]:
    if not isinstance(raw, list) or not raw:
        problems.append(f"{where}: ожидался непустой список точек")
        return []
    out: list[Point] = []
    for i, p in enumerate(raw):
        if (
            not isinstance(p, (list, tuple))
            or len(p) != 2
            or not all(isinstance(c, (int, float)) for c in p)
        ):
            problems.append(f"{where}[{i}]: точка должна быть [x, y] из чисел, получено {p!r}")
            continue
        out.append((float(p[0]), float(p[1])))
    return out


def _rect(raw: Any, where: str, problems: list[str]) -> tuple[int, int, int, int] | None:
    if (
        not isinstance(raw, (list, tuple))
        or len(raw) != 4
        or not all(isinstance(c, (int, float)) for c in raw)
    ):
        problems.append(f"{where}: ожидался прямоугольник [x1, y1, x2, y2], получено {raw!r}")
        return None
    x1, y1, x2, y2 = (int(c) for c in raw)
    if x2 <= x1 or y2 <= y1:
        problems.append(f"{where}: вырожденный прямоугольник {raw!r} (x2>x1 и y2>y1)")
        return None
    return x1, y1, x2, y2


def _geo(raw: Any, where: str, problems: list[str]) -> GeoPoint | None:
    if not isinstance(raw, dict) or "lat" not in raw or "lon" not in raw:
        problems.append(f"{where}: ожидался объект {{lat: ..., lon: ...}}")
        return None
    return GeoPoint(float(raw["lat"]), float(raw["lon"]))


def _polygon_area(pts: list[Point]) -> float:
    """Площадь по формуле шнурков, по модулю."""
    a = 0.0
    for i in range(len(pts)):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % len(pts)]
        a += x1 * y2 - x2 * y1
    return abs(a) / 2.0


def _check_geo(lat: float, lon: float, where: str, problems: list[str]) -> None:
    lat_min, lon_min, lat_max, lon_max = VLADIVOSTOK_BBOX
    if not (lat_min <= lat <= lat_max and lon_min <= lon <= lon_max):
        problems.append(
            f"{where}: координаты ({lat}, {lon}) вне Владивостока. "
            f"Примеры в CLAUDE.md (43.115141, 131.885341) — заглушка, не наши камеры"
        )


def _check_inside(
    pts: list[Point], size: tuple[int, int], where: str, problems: list[str]
) -> None:
    w, h = size
    for i, (x, y) in enumerate(pts):
        if not (0 <= x <= w and 0 <= y <= h):
            problems.append(
                f"{where}[{i}]: точка ({x}, {y}) вне кадра {w}x{h}"
            )


def _parse_spot(
    raw: dict, cam_id: str, size: tuple[int, int], problems: list[str]
) -> Spot | None:
    where = f"{cam_id}.spots[{raw.get('id', '?')}]"
    sid = raw.get("id")
    if not isinstance(sid, str) or not sid:
        problems.append(f"{where}: отсутствует строковый id")
        return None

    quad = _points(raw.get("quad"), f"{where}.quad", problems)
    if len(quad) != 4:
        if quad:
            problems.append(f"{where}.quad: нужно ровно 4 точки, получено {len(quad)}")
        return None
    _check_inside(quad, size, f"{where}.quad", problems)

    if _polygon_area(quad) < 1.0:
        problems.append(f"{where}.quad: вырожденный четырёхугольник (нулевая площадь)")
        return None

    stype = raw.get("type", "standard")
    if stype not in SPOT_TYPES:
        problems.append(f"{where}.type: {stype!r} не из {sorted(SPOT_TYPES)}")
        stype = "standard"

    try:
        lat, lon = float(raw["lat"]), float(raw["lon"])
    except (KeyError, TypeError, ValueError):
        problems.append(f"{where}: нужны числовые lat и lon")
        return None
    _check_geo(lat, lon, where, problems)

    approach = _geo(raw.get("approach"), f"{where}.approach", problems)
    if approach is None:
        return None
    _check_geo(approach.lat, approach.lon, f"{where}.approach", problems)

    spot = Spot(
        id=sid,
        quad=quad,
        lat=lat,
        lon=lon,
        approach=approach,
        type=stype,
        enabled=bool(raw.get("enabled", True)),
        notes=str(raw.get("notes", "")),
    )
    if spot.enabled and spot.long_side_px < MIN_SPOT_LONG_SIDE_PX:
        problems.append(
            f"{where}: длинная сторона {spot.long_side_px:.1f} px < "
            f"{MIN_SPOT_LONG_SIDE_PX} px — детектор машину не увидит. "
            f"Переведите участок в зону или выставьте enabled: false"
        )
    return spot


def _parse_zone(
    raw: dict, cam_id: str, size: tuple[int, int], problems: list[str]
) -> Zone | None:
    where = f"{cam_id}.zones[{raw.get('id', '?')}]"
    zid = raw.get("id")
    if not isinstance(zid, str) or not zid:
        problems.append(f"{where}: отсутствует строковый id")
        return None

    poly = _points(raw.get("polygon"), f"{where}.polygon", problems)
    if len(poly) < 3:
        problems.append(f"{where}.polygon: нужно минимум 3 точки, получено {len(poly)}")
        return None
    _check_inside(poly, size, f"{where}.polygon", problems)
    if _polygon_area(poly) < 1.0:
        problems.append(f"{where}.polygon: вырожденный полигон (нулевая площадь)")
        return None

    try:
        capacity = int(raw["capacity"])
    except (KeyError, TypeError, ValueError):
        problems.append(f"{where}: нужна целочисленная capacity")
        return None
    if capacity <= 0:
        problems.append(f"{where}.capacity: должна быть > 0, получено {capacity}")
        return None

    try:
        lat, lon = float(raw["lat"]), float(raw["lon"])
    except (KeyError, TypeError, ValueError):
        problems.append(f"{where}: нужны числовые lat и lon")
        return None
    _check_geo(lat, lon, where, problems)

    entrance = _geo(raw.get("entrance"), f"{where}.entrance", problems)
    if entrance is None:
        return None
    _check_geo(entrance.lat, entrance.lon, f"{where}.entrance", problems)

    return Zone(
        id=zid,
        name=str(raw.get("name", zid)),
        polygon=poly,
        capacity=capacity,
        lat=lat,
        lon=lon,
        entrance=entrance,
    )


def _parse_camera(
    raw: dict, defaults: dict, problems: list[str], require_rois: bool
) -> Camera | None:
    cam_id = raw.get("id")
    if not isinstance(cam_id, str) or not cam_id:
        problems.append("cameras[]: отсутствует строковый id")
        return None

    url = raw.get("playlist_url")
    if not isinstance(url, str) or not url.startswith("https://"):
        problems.append(f"{cam_id}.playlist_url: нужен https-URL, получено {url!r}")
        return None

    fs = raw.get("frame_size")
    if (
        not isinstance(fs, (list, tuple))
        or len(fs) != 2
        or not all(isinstance(c, int) and c > 0 for c in fs)
    ):
        problems.append(f"{cam_id}.frame_size: нужен [width, height] из целых > 0")
        return None
    size = (int(fs[0]), int(fs[1]))

    headers = {**defaults.get("headers", {}), **raw.get("headers", {})}
    if "Referer" not in headers:
        problems.append(
            f"{cam_id}.headers: нет Referer. Без него stream.vladlink.ru отдаёт "
            f"HTTP 200 и валидное видео-ЗАГЛУШКУ «Трансляция заблокирована», "
            f"а не ошибку. Нужен Referer: https://vladlink.ru/"
        )

    overlays: list[tuple[int, int, int, int]] = []
    for i, r in enumerate(raw.get("overlay_exclude", []) or []):
        rect = _rect(r, f"{cam_id}.overlay_exclude[{i}]", problems)
        if rect:
            overlays.append(rect)

    tiles: list[tuple[int, int, int, int]] = []
    for i, r in enumerate(raw.get("tiles", []) or []):
        rect = _rect(r, f"{cam_id}.tiles[{i}]", problems)
        if rect:
            if rect[2] > size[0] or rect[3] > size[1]:
                problems.append(
                    f"{cam_id}.tiles[{i}]: {list(rect)} выходит за кадр {size[0]}x{size[1]}"
                )
            else:
                tiles.append(rect)

    spots, zones = [], []
    for s in raw.get("spots", []) or []:
        parsed = _parse_spot(s, cam_id, size, problems)
        if parsed:
            spots.append(parsed)
    for z in raw.get("zones", []) or []:
        parsed_z = _parse_zone(z, cam_id, size, problems)
        if parsed_z:
            zones.append(parsed_z)

    if require_rois and not spots and not zones:
        problems.append(f"{cam_id}: нет ни одного места и ни одной зоны")

    return Camera(
        id=cam_id,
        name=str(raw.get("name", cam_id)),
        parking_lot_id=str(raw.get("parking_lot_id", "")),
        playlist_url=url,
        frame_size=size,
        headers=headers,
        poll_interval_s=int(raw.get("poll_interval_s", defaults.get("poll_interval_s", 15))),
        range_bytes=int(raw.get("range_bytes", defaults.get("range_bytes", 500_000))),
        reference_frame=raw.get("reference_frame"),
        overlay_exclude=overlays,
        tiles=tiles,
        footprint_offset_px=float(raw.get("footprint_offset_px", 0.0)),
        spots=spots,
        zones=zones,
    )


def load_config(path: str | os.PathLike[str], require_rois: bool = True) -> Config:
    """Читает и валидирует cameras.yml. Поднимает ConfigError со ВСЕМИ проблемами сразу.

    require_rois=False нужен рекордеру: он стартует в первый час проекта, когда
    разметки ROI ещё нет, и его задача — просто копить кадры.
    """
    p = Path(path)
    with p.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)

    problems: list[str] = []
    if not isinstance(raw, dict):
        raise ConfigError(["корень cameras.yml должен быть объектом"])

    version = raw.get("version")
    if version != 1:
        problems.append(f"version: поддерживается только 1, получено {version!r}")

    defaults = raw.get("defaults") or {}
    raw_cameras = raw.get("cameras")
    if not isinstance(raw_cameras, list) or not raw_cameras:
        raise ConfigError(problems + ["cameras: ожидался непустой список"])

    cameras: list[Camera] = []
    for rc in raw_cameras:
        if not isinstance(rc, dict):
            problems.append(f"cameras[]: ожидался объект, получено {type(rc).__name__}")
            continue
        cam = _parse_camera(rc, defaults, problems, require_rois)
        if cam:
            cameras.append(cam)

    # ID уникальны глобально: spot_id и zone_id уходят в backend как ключи.
    for label, ids in (
        ("камер", [c.id for c in cameras]),
        ("мест", [s.id for c in cameras for s in c.spots]),
        ("зон", [z.id for c in cameras for z in c.zones]),
    ):
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            problems.append(f"неуникальные id {label}: {', '.join(dupes)}")

    if problems:
        raise ConfigError(problems)

    return Config(version=1, cameras=cameras, source_path=p)
