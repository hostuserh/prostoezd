"""Детектор транспорта для ParkVision.

Публичный вход — Detector.detect(frame, camera) -> list[Detection].
Слой occupancy (A4) привязывает Detection.footprint к полигонам мест/зон.

Импорт onnxruntime ленивый: tiler и boxes работают без него, и рекордер,
который стартует на голом сервере, этот пакет не тянет.
"""

from .boxes import iou, nms
from .tiler import (
    DEFAULT_OVERLAP,
    DEFAULT_TILE_SIZE,
    Tile,
    plan_tiles,
    plan_tiles_for_camera,
    tiles_coverage,
)
from .yolo_onnx import (
    COCO_VEHICLE_CLASSES,
    DEFAULT_CONF,
    Detection,
    Detector,
    decode_yolo_output,
    letterbox,
    resolve_model_path,
)

__all__ = [
    "COCO_VEHICLE_CLASSES",
    "DEFAULT_CONF",
    "DEFAULT_OVERLAP",
    "DEFAULT_TILE_SIZE",
    "Detection",
    "Detector",
    "Tile",
    "decode_yolo_output",
    "iou",
    "letterbox",
    "nms",
    "plan_tiles",
    "plan_tiles_for_camera",
    "resolve_model_path",
    "tiles_coverage",
]
