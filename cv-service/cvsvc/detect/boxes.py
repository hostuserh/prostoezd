"""Операции над боксами: IoU и NMS. Чистый numpy, без onnxruntime.

Вынесено отдельно от детектора, чтобы тесты на геометрию не тянули модель.
"""

from __future__ import annotations

import numpy as np

Box = tuple[float, float, float, float]  # x1, y1, x2, y2


def box_area(box: Box) -> float:
    x1, y1, x2, y2 = box
    return max(0.0, x2 - x1) * max(0.0, y2 - y1)


def iou(a: Box, b: Box) -> float:
    """Intersection over Union двух боксов."""
    ix1 = max(a[0], b[0])
    iy1 = max(a[1], b[1])
    ix2 = min(a[2], b[2])
    iy2 = min(a[3], b[3])
    inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    if inter <= 0.0:
        return 0.0
    union = box_area(a) + box_area(b) - inter
    return inter / union if union > 0 else 0.0


def nms(
    boxes: np.ndarray,
    scores: np.ndarray,
    iou_threshold: float = 0.5,
    class_ids: np.ndarray | None = None,
    class_agnostic: bool = True,
) -> list[int]:
    """Greedy NMS. Возвращает индексы оставшихся боксов в порядке убывания score.

    boxes: (N, 4) в формате x1, y1, x2, y2 — координаты ПОЛНОГО кадра.
    class_agnostic=True по умолчанию: одна и та же машина в зоне перекрытия
    тайлов легко получает в одном тайле class=car, а в другом class=truck.
    Нам важен факт «тут стоит транспорт», а не его класс, поэтому давим между
    классами тоже. Иначе A4 посчитает одну машину за две.
    """
    n = len(boxes)
    if n == 0:
        return []
    boxes = np.asarray(boxes, dtype=np.float64).reshape(n, 4)
    scores = np.asarray(scores, dtype=np.float64).reshape(n)

    if not class_agnostic and class_ids is not None:
        keep: list[int] = []
        class_ids = np.asarray(class_ids).reshape(n)
        for cid in np.unique(class_ids):
            idx = np.nonzero(class_ids == cid)[0]
            sub = nms(boxes[idx], scores[idx], iou_threshold)
            keep.extend(int(idx[s]) for s in sub)
        keep.sort(key=lambda i: -scores[i])
        return keep

    x1, y1, x2, y2 = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
    areas = np.clip(x2 - x1, 0, None) * np.clip(y2 - y1, 0, None)
    order = np.argsort(-scores, kind="stable")

    keep_idx: list[int] = []
    while order.size > 0:
        i = int(order[0])
        keep_idx.append(i)
        if order.size == 1:
            break
        rest = order[1:]
        ix1 = np.maximum(x1[i], x1[rest])
        iy1 = np.maximum(y1[i], y1[rest])
        ix2 = np.minimum(x2[i], x2[rest])
        iy2 = np.minimum(y2[i], y2[rest])
        inter = np.clip(ix2 - ix1, 0, None) * np.clip(iy2 - iy1, 0, None)
        union = areas[i] + areas[rest] - inter
        ovr = np.where(union > 0, inter / np.maximum(union, 1e-9), 0.0)
        order = rest[ovr <= iou_threshold]

    return keep_idx
