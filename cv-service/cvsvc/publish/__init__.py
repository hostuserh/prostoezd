"""Слой доставки статусов занятости из CV-сервиса в backend ParkVision.

Публичный интерфейс:

    from cvsvc.publish import BackendClient, OccupancyPayload, Detection, ZoneCount

Контракт с backend описан в ``cv-service/docs/CV_BACKEND_CONTRACT.md``.
"""

from cvsvc.publish.schemas import (  # noqa: F401
    VLADIVOSTOK,
    Camera,
    CameraStatus,
    Detection,
    GeoPoint,
    HealthResponse,
    OccupancyHistory,
    OccupancyPayload,
    OccupancyResponse,
    ParkingLot,
    ParkingSpot,
    ParkingType,
    RejectedSpot,
    SpotStatus,
    SpotType,
    ZoneCount,
    now_vvo,
)
from cvsvc.publish.client import (  # noqa: F401
    BackendClient,
    PublishOutcome,
    PublishResult,
)
from cvsvc.publish.buffer import SnapshotBuffer  # noqa: F401

__all__ = [
    "VLADIVOSTOK",
    "BackendClient",
    "Camera",
    "CameraStatus",
    "Detection",
    "GeoPoint",
    "HealthResponse",
    "OccupancyHistory",
    "OccupancyPayload",
    "OccupancyResponse",
    "ParkingLot",
    "ParkingSpot",
    "ParkingType",
    "PublishOutcome",
    "PublishResult",
    "RejectedSpot",
    "SnapshotBuffer",
    "SpotStatus",
    "SpotType",
    "ZoneCount",
    "now_vvo",
]
