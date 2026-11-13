"""Детектор транспорта: YOLOv8n/YOLO11n в ONNX на onnxruntime CPU.

Почему именно так, а не классификатор занятости по ROI:
у потоков vladlink нет архива (DVR, см. README), размеченного датасета под наш
ракурс нет и взять его негде. YOLO по COCO-классам работает из коробки, без
единого размеченного кадра. Цена решения — неизвестный recall на мелких
объектах под нашим ракурсом сверху-вбок; он измерен, см. docs/DETECTION_QUALITY.md.

Детектор НЕ решает, занято место или нет. Он отдаёт боксы транспорта; привязку
бокса к полигону места делает слой occupancy (A4). Поэтому порог уверенности
низкий (0.20): лучше отдать лишний бокс, который отсеется геометрией места,
чем потерять машину.

Потоки: intra_op_num_threads=3 из четырёх ядер. Четвёртое остаётся планировщику,
ingest (качает сегменты) и publisher. Если отдать ORT все 4, цикл ingest начинает
опаздывать, а выигрыш по латентности детекта в пределах 10%.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np

from .boxes import nms
from .tiler import (
    DEFAULT_OVERLAP,
    DEFAULT_TILE_SIZE,
    Tile,
    plan_tiles_for_camera,
)

# Классы COCO, которые считаем транспортом на парковке.
# Автобусы и грузовики нужны: на CAM-001 в кармане регулярно стоит газель,
# а YOLO уверенно зовёт её truck.
COCO_VEHICLE_CLASSES: dict[int, str] = {
    2: "car",
    3: "motorcycle",
    5: "bus",
    7: "truck",
}

DEFAULT_CONF = 0.20
DEFAULT_NMS_IOU = 0.5
DEFAULT_INTRA_OP_THREADS = 3
# Серый паддинг letterbox — как в ultralytics, иначе смещается нормировка.
LETTERBOX_PAD = 114

_MODELS_DIR = Path(__file__).resolve().parents[2] / "models"
# Порядок поиска модели по умолчанию: сперва int8, потом fp32.
DEFAULT_MODEL_CANDIDATES = ("yolov8n-int8.onnx", "yolov8n.onnx")


@dataclass(frozen=True)
class Detection:
    """Один задетектированный транспорт в координатах ПОЛНОГО кадра."""

    bbox: tuple[float, float, float, float]  # x1, y1, x2, y2
    score: float
    class_id: int
    # Сдвиг следа на земле относительно низа бокса, в пикселях этого кадра.
    footprint_offset_px: float = 0.0

    @property
    def class_name(self) -> str:
        return COCO_VEHICLE_CLASSES.get(self.class_id, str(self.class_id))

    @property
    def width(self) -> float:
        return self.bbox[2] - self.bbox[0]

    @property
    def height(self) -> float:
        return self.bbox[3] - self.bbox[1]

    @property
    def size_px(self) -> float:
        """Длинная сторона бокса — ею меряем «машина крупнее/мельче 60 px»."""
        return max(self.width, self.height)

    @property
    def center(self) -> tuple[float, float]:
        x1, y1, x2, y2 = self.bbox
        return ((x1 + x2) / 2.0, (y1 + y2) / 2.0)

    @property
    def footprint(self) -> tuple[float, float]:
        """Точка касания машины с асфальтом: нижний центр бокса + offset камеры.

        Зачем не центр бокса. Камеры смотрят сверху-вбок, корпус машины на
        картинке «уезжает» вверх от своего следа на земле: чем выше кузов и
        острее ракурс, тем сильнее. Привязка по центру бокса систематически
        отправляет машину в место ПОЗАДИ её собственного — ошибка не случайная,
        а всегда в одну сторону, и на плотном ряду сдвигает всю разметку.
        Нижний центр ближе к следу; остаток добирается камерным
        footprint_offset_px (вниз по кадру, подбирается глазами по ряду).
        """
        x1, _, x2, y2 = self.bbox
        return ((x1 + x2) / 2.0, y2 + self.footprint_offset_px)

    def as_dict(self) -> dict[str, Any]:
        return {
            "bbox": [round(v, 1) for v in self.bbox],
            "score": round(self.score, 3),
            "class_id": self.class_id,
            "class_name": self.class_name,
            "footprint": [round(v, 1) for v in self.footprint],
        }


# --------------------------------------------------------------------------- #
# Препроцессинг
# --------------------------------------------------------------------------- #


def letterbox(
    img: np.ndarray, size: int = DEFAULT_TILE_SIZE, pad_value: int = LETTERBOX_PAD
) -> tuple[np.ndarray, float, int, int]:
    """Вписать изображение в квадрат size x size без искажения пропорций.

    Возвращает (картинка, gain, pad_x, pad_y). Обратное преобразование:
    x_src = (x_letterboxed - pad_x) / gain.
    """
    h, w = img.shape[:2]
    gain = min(size / w, size / h)
    # Тайл 640x640 проходит без ресайза (gain == 1.0) — так сохраняется
    # нативный масштаб объекта, ради которого весь тайлинг и затевался.
    nw, nh = int(round(w * gain)), int(round(h * gain))
    if (nw, nh) != (w, h):
        import cv2

        interp = cv2.INTER_AREA if gain < 1 else cv2.INTER_LINEAR
        img = cv2.resize(img, (nw, nh), interpolation=interp)
    canvas = np.full((size, size, img.shape[2]), pad_value, dtype=np.uint8)
    pad_x = (size - nw) // 2
    pad_y = (size - nh) // 2
    canvas[pad_y : pad_y + nh, pad_x : pad_x + nw] = img
    return canvas, gain, pad_x, pad_y


def _to_blob(tiles_img: Sequence[np.ndarray]) -> np.ndarray:
    """BGR HWC uint8 -> RGB NCHW float32 [0, 1]."""
    arr = np.stack(tiles_img, axis=0)[:, :, :, ::-1]  # BGR -> RGB
    arr = np.ascontiguousarray(arr.transpose(0, 3, 1, 2), dtype=np.float32)
    arr /= 255.0
    return arr


# --------------------------------------------------------------------------- #
# Декод выхода YOLOv8/YOLO11
# --------------------------------------------------------------------------- #


def decode_yolo_output(
    output: np.ndarray,
    conf: float,
    keep_classes: Iterable[int],
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Разобрать один выход YOLOv8/YOLO11: (84, 8400) -> боксы xyxy в letterbox-координатах.

    Первые 4 строки — cx, cy, w, h; остальные 80 — скоры классов (без objectness,
    это v8, а не v5). Берём максимум ТОЛЬКО по нужным классам: максимум по всем
    80 дал бы потерю машины, которую сеть на 0.3 считает car и на 0.35 — boat.
    """
    out = np.asarray(output)
    if out.ndim == 3:
        out = out[0]
    # Приводим к (anchors, channels). Каналов всегда >= 5 (4 координаты + классы),
    # якорей у реальной модели 8400 — но в тестах их бывает два, поэтому
    # «меньшая ось = каналы» было бы неверным правилом.
    if out.shape[1] < 5 <= out.shape[0]:  # (84, 2) -> (2, 84)
        out = out.T
    elif 5 <= out.shape[0] < out.shape[1]:  # (84, 8400) -> (8400, 84)
        out = out.T
    if out.shape[1] < 5:
        raise ValueError(f"неожиданная форма выхода модели: {output.shape}")

    cls_scores = out[:, 4:]
    idx = np.array(sorted(keep_classes), dtype=np.int64)
    idx = idx[idx < cls_scores.shape[1]]
    sub = cls_scores[:, idx]

    best = sub.argmax(axis=1)
    scores = sub[np.arange(sub.shape[0]), best]
    mask = scores >= conf
    if not mask.any():
        return (
            np.zeros((0, 4), dtype=np.float32),
            np.zeros((0,), dtype=np.float32),
            np.zeros((0,), dtype=np.int64),
        )

    cx, cy, w, h = out[mask, 0], out[mask, 1], out[mask, 2], out[mask, 3]
    boxes = np.stack([cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2], axis=1)
    return boxes.astype(np.float32), scores[mask].astype(np.float32), idx[best[mask]]


def _unletterbox(
    boxes: np.ndarray, gain: float, pad_x: int, pad_y: int, tile: Tile
) -> np.ndarray:
    """Боксы из letterbox-квадрата -> в координаты полного кадра."""
    if len(boxes) == 0:
        return boxes
    out = boxes.copy()
    out[:, [0, 2]] = (out[:, [0, 2]] - pad_x) / gain + tile.x1
    out[:, [1, 3]] = (out[:, [1, 3]] - pad_y) / gain + tile.y1
    # Клип по границам тайла: часть машины за швом принадлежит соседнему тайлу,
    # там она и будет найдена целиком, а глобальный NMS оставит лучший бокс.
    out[:, 0] = np.clip(out[:, 0], tile.x1, tile.x2)
    out[:, 2] = np.clip(out[:, 2], tile.x1, tile.x2)
    out[:, 1] = np.clip(out[:, 1], tile.y1, tile.y2)
    out[:, 3] = np.clip(out[:, 3], tile.y1, tile.y2)
    return out


# --------------------------------------------------------------------------- #
# Детектор
# --------------------------------------------------------------------------- #


def resolve_model_path(explicit: str | os.PathLike[str] | None = None) -> Path:
    if explicit:
        p = Path(explicit)
        if not p.exists():
            raise FileNotFoundError(f"модель не найдена: {p}")
        return p
    env = os.environ.get("PARKVISION_MODEL")
    if env:
        return resolve_model_path(env)
    for name in DEFAULT_MODEL_CANDIDATES:
        p = _MODELS_DIR / name
        if p.exists():
            return p
    raise FileNotFoundError(
        f"нет модели в {_MODELS_DIR}. Сначала: python tools/export_model.py"
    )


class Detector:
    """Ленивая обёртка над ORT-сессией. Переиспользуйте один экземпляр на процесс.

    Создание сессии стоит ~0.3-0.5 с и ~100 МБ RSS: создавать её на каждый кадр
    нельзя, при трёх камерах раз в 15 секунд это съест и время, и память.
    """

    def __init__(
        self,
        model_path: str | os.PathLike[str] | None = None,
        conf: float = DEFAULT_CONF,
        nms_iou: float = DEFAULT_NMS_IOU,
        tile_size: int = DEFAULT_TILE_SIZE,
        overlap: int = DEFAULT_OVERLAP,
        intra_op_threads: int = DEFAULT_INTRA_OP_THREADS,
        keep_classes: Iterable[int] = tuple(COCO_VEHICLE_CLASSES),
        max_batch: int = 8,
    ) -> None:
        self.model_path = Path(model_path) if model_path else None
        self.conf = float(conf)
        self.nms_iou = float(nms_iou)
        self.tile_size = int(tile_size)
        self.overlap = int(overlap)
        self.intra_op_threads = int(intra_op_threads)
        self.keep_classes = tuple(sorted(set(int(c) for c in keep_classes)))
        self.max_batch = int(max_batch)
        self._session = None
        self._input_name: str | None = None
        self._batch_is_dynamic = True

    # -- сессия ------------------------------------------------------------- #

    @property
    def session(self):
        if self._session is None:
            self._session = self._build_session()
        return self._session

    def _build_session(self):
        # OMP-переменные читаются при ЗАГРУЗКЕ нативной библиотеки ORT,
        # выставлять их после import onnxruntime поздно.
        os.environ.setdefault("OMP_NUM_THREADS", str(self.intra_op_threads))
        os.environ.setdefault("OMP_WAIT_POLICY", "PASSIVE")

        import onnxruntime as ort

        path = resolve_model_path(self.model_path)
        self.model_path = path

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = self.intra_op_threads
        opts.inter_op_num_threads = 1
        opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        # Арена памяти на 4 ГБ сервере отдаёт RSS обратно неохотно.
        opts.enable_cpu_mem_arena = False

        sess = ort.InferenceSession(
            str(path), sess_options=opts, providers=["CPUExecutionProvider"]
        )
        inp = sess.get_inputs()[0]
        self._input_name = inp.name
        self._batch_is_dynamic = not isinstance(inp.shape[0], int)
        return sess

    def warmup(self, camera=None) -> None:
        """Прогнать один пустой тайл: первый вызов ORT дороже последующих в 2-4 раза."""
        blob = np.zeros((1, 3, self.tile_size, self.tile_size), dtype=np.float32)
        self.session.run(None, {self._input_name: blob})

    # -- детект ------------------------------------------------------------- #

    def detect(self, frame_bgr_or_path, camera) -> list[Detection]:
        """Публичный API. frame: np.ndarray BGR либо путь к JPEG.

        camera: cvsvc.config.Camera — из него берутся tiles и footprint_offset_px.
        Возвращает боксы в координатах ПОЛНОГО кадра, отсортированные по score.
        """
        frame = _as_frame(frame_bgr_or_path)
        h, w = frame.shape[:2]
        tiles = plan_tiles_for_camera(
            camera, (w, h), tile_size=self.tile_size, overlap=self.overlap
        )
        if not tiles:
            return []

        # footprint_offset задан в пикселях эталонного кадра: если поток отдал
        # другое разрешение, сдвиг тоже надо масштабировать.
        _, sy = camera.scale_factor((w, h))
        offset = float(getattr(camera, "footprint_offset_px", 0.0)) * sy

        prepared = [letterbox(t.crop(frame), self.tile_size) for t in tiles]
        blob = _to_blob([p[0] for p in prepared])

        sess = self.session
        batch = len(tiles) if self._batch_is_dynamic else 1
        batch = max(1, min(batch, self.max_batch))

        all_boxes: list[np.ndarray] = []
        all_scores: list[np.ndarray] = []
        all_cls: list[np.ndarray] = []
        for start in range(0, len(tiles), batch):
            chunk = blob[start : start + batch]
            outputs = sess.run(None, {self._input_name: chunk})[0]
            outputs = np.asarray(outputs)
            if outputs.ndim == 2:  # сессия без батч-оси
                outputs = outputs[None, ...]
            for k in range(outputs.shape[0]):
                tile = tiles[start + k]
                _, gain, pad_x, pad_y = prepared[start + k]
                b, s, c = decode_yolo_output(outputs[k], self.conf, self.keep_classes)
                if len(b) == 0:
                    continue
                all_boxes.append(_unletterbox(b, gain, pad_x, pad_y, tile))
                all_scores.append(s)
                all_cls.append(c)

        if not all_boxes:
            return []

        boxes = np.concatenate(all_boxes, axis=0)
        scores = np.concatenate(all_scores, axis=0)
        classes = np.concatenate(all_cls, axis=0)

        # Глобальный NMS по ВСЕМУ кадру, а не внутри тайлов: машина на шве
        # детектится дважды, в каждом из перекрывающихся тайлов.
        keep = nms(boxes, scores, self.nms_iou, classes, class_agnostic=True)

        return [
            Detection(
                bbox=(
                    float(boxes[i][0]),
                    float(boxes[i][1]),
                    float(boxes[i][2]),
                    float(boxes[i][3]),
                ),
                score=float(scores[i]),
                class_id=int(classes[i]),
                footprint_offset_px=offset,
            )
            for i in keep
        ]


def _as_frame(src) -> np.ndarray:
    if isinstance(src, np.ndarray):
        return src
    import cv2

    img = cv2.imread(str(src), cv2.IMREAD_COLOR)
    if img is None:
        raise FileNotFoundError(f"не удалось прочитать кадр: {src}")
    return img
