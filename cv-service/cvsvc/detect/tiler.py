"""Нарезка кадра на тайлы под детектор и пересчёт координат обратно.

Зачем вообще тайлить. Машина в кадре 1920x1080 занимает 25-70 px. Если отдать
весь кадр в YOLO на вход 640x640, масштаб падает втрое и машина становится
8-23 px — ниже порога, на котором сеть вообще что-то видит. Тайл 640x640,
взятый из кадра в нативном разрешении, сохраняет исходный размер объекта.

Зачем тайлить ТОЛЬКО по областям парковок (cam.tiles). На CAM-003 (2592x1944)
полный кадр — это 6 тайлов (3x2 со степом 544), а две-три области парковок —
2-3 тайла. Втрое меньше работы при той же полезной информации: небо,
многоэтажки и проезжая часть нам не нужны. Если cam.tiles пуст, тайлим весь
кадр — это рабочий, но втрое более дорогой режим по умолчанию.

Перекрытие 96 px: самая крупная машина у нас 120 px (CAM-003, паркинг перед ТЦ).
При перекрытии 96 машина, попавшая на стык, ц��ликом видна хотя бы в одном
тайле, если её центр не дальше 48 px от шва. Остаток ловится глобальным NMS
после склейки (см. boxes.nms) — без него машина на стыке даст два бокса.

Модуль не зависит от onnxruntime: чистая геометрия, тестируется без модели.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

import numpy as np

# Вход YOLOv8n/YOLO11n. Менять вместе с моделью.
DEFAULT_TILE_SIZE = 640
# Максимальная машина в кадре — 120 px (CAM-003). 96 px перекрытия с запасом.
DEFAULT_OVERLAP = 96

Rect = tuple[int, int, int, int]  # x1, y1, x2, y2


@dataclass(frozen=True)
class Tile:
    """Прямоугольник в системе координат ПОЛНОГО кадра."""

    x1: int
    y1: int
    x2: int
    y2: int
    region: int = 0  # индекс исходной области из cam.tiles

    @property
    def width(self) -> int:
        return self.x2 - self.x1

    @property
    def height(self) -> int:
        return self.y2 - self.y1

    @property
    def origin(self) -> tuple[int, int]:
        return self.x1, self.y1

    def as_rect(self) -> Rect:
        return self.x1, self.y1, self.x2, self.y2

    def crop(self, frame: np.ndarray) -> np.ndarray:
        """Вырезать тайл из кадра. Возвращает view, не копию."""
        return frame[self.y1 : self.y2, self.x1 : self.x2]

    def to_frame(self, box: Sequence[float]) -> tuple[float, float, float, float]:
        """Бокс из локальных координат тайла -> в координаты полного кадра."""
        x1, y1, x2, y2 = box
        return (x1 + self.x1, y1 + self.y1, x2 + self.x1, y2 + self.y1)


def _starts(length: int, tile: int, overlap: int) -> list[int]:
    """Начала тайлов вдоль одной оси.

    Шаги распределяются равномерно, а не «степ-степ-огрызок»: последний тайл
    всегда полного размера, фактическое перекрытие >= заданного. Так на краю
    области не появляется узкая полоска, в которой машина обрезана.
    """
    if length <= tile:
        return [0]
    step = tile - overlap
    if step <= 0:
        raise ValueError(f"overlap {overlap} >= tile {tile}: шаг нулевой или отрицательный")
    count = math.ceil((length - tile) / step) + 1
    span = length - tile
    return [round(i * span / (count - 1)) for i in range(count)]


def plan_tiles(
    regions: Sequence[Rect] | None,
    frame_size: tuple[int, int],
    tile_size: int = DEFAULT_TILE_SIZE,
    overlap: int = DEFAULT_OVERLAP,
) -> list[Tile]:
    """Разложить области парковок на тайлы.

    regions: список [x1, y1, x2, y2] из cam.tiles. Пустой/None -> весь кадр.
    frame_size: (width, height) РЕАЛЬНОГО кадра. Регионы обрезаются по нему.

    Область меньше tile_size не растягивается до 640 — отдаётся как есть,
    letterbox в детекторе добьёт её серым. Лишние пиксели кадра в тайл
    не подмешиваем: это были бы детекты вне размеченной зоны.
    """
    w, h = frame_size
    if w <= 0 or h <= 0:
        raise ValueError(f"некорректный размер кадра {frame_size}")

    rects: list[Rect] = []
    if regions:
        for r in regions:
            x1, y1, x2, y2 = (int(round(v)) for v in r)
            x1, y1 = max(0, x1), max(0, y1)
            x2, y2 = min(w, x2), min(h, y2)
            if x2 > x1 and y2 > y1:
                rects.append((x1, y1, x2, y2))
    if not rects:
        rects = [(0, 0, w, h)]

    tiles: list[Tile] = []
    for ri, (rx1, ry1, rx2, ry2) in enumerate(rects):
        rw, rh = rx2 - rx1, ry2 - ry1
        for oy in _starts(rh, tile_size, overlap):
            for ox in _starts(rw, tile_size, overlap):
                tx1 = rx1 + ox
                ty1 = ry1 + oy
                tx2 = min(tx1 + tile_size, rx2)
                ty2 = min(ty1 + tile_size, ry2)
                tiles.append(Tile(tx1, ty1, tx2, ty2, region=ri))
    return tiles


def plan_tiles_for_camera(
    camera,
    frame_size: tuple[int, int] | None = None,
    tile_size: int = DEFAULT_TILE_SIZE,
    overlap: int = DEFAULT_OVERLAP,
) -> list[Tile]:
    """plan_tiles для cvsvc.config.Camera с учётом реального разрешения кадра.

    Координаты в cameras.yml заданы в системе эталонного кадра (camera.frame_size).
    Если поток отдал другое разрешение, регионы масштабируются — хардкодить
    пиксели под одно разрешение нельзя, у камер оно разное (1920x1080 и 2592x1944).
    """
    actual = tuple(frame_size) if frame_size else tuple(camera.frame_size)
    sx, sy = camera.scale_factor(actual) if frame_size else (1.0, 1.0)
    regions = [
        (round(x1 * sx), round(y1 * sy), round(x2 * sx), round(y2 * sy))
        for (x1, y1, x2, y2) in (camera.tiles or [])
    ]
    return plan_tiles(regions, actual, tile_size=tile_size, overlap=overlap)


def tiles_coverage(tiles: Sequence[Tile], frame_size: tuple[int, int]) -> float:
    """Доля площади кадра, покрытая тайлами (с учётом перекрытий, без двойного счёта).

    Нужна для отчёта: показывает, во сколько раз тайлинг по зонам дешевле
    полного кадра.
    """
    w, h = frame_size
    if not tiles or w <= 0 or h <= 0:
        return 0.0
    mask = np.zeros((h, w), dtype=bool)
    for t in tiles:
        mask[t.y1 : t.y2, t.x1 : t.x2] = True
    return float(mask.sum()) / float(w * h)
