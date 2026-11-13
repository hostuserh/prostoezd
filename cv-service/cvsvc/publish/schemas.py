"""Pydantic v2 модели контракта CV-сервис <-> backend ParkVision.

Источник правды — ``CLAUDE.md`` в корне репозитория. Имена полей всех моделей,
которые есть в спеке (ParkingLot, ParkingSpot, Camera, OccupancyHistory,
payload/response ``POST /internal/occupancy``, response ``GET /health``),
повторены байт-в-байт.

Расширения, которых в спеке нет, помечены комментарием ``# EXT``. Все они
строго additive: payload из примера в CLAUDE.md обязан оставаться валидным.
Полное обоснование — в ``cv-service/docs/CV_BACKEND_CONTRACT.md``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Annotated, Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

__all__ = [
    "VLADIVOSTOK",
    "Camera",
    "CameraStatus",
    "Detection",
    "GeoPoint",
    "HealthResponse",
    "OccupancyHistory",
    "OccupancyPayload",
    "OccupancyResponse",
    "ParkingDetail",
    "ParkingLot",
    "ParkingSpot",
    "ParkingSummary",
    "ParkingType",
    "Recommendation",
    "RecommendationRequest",
    "RecommendationsResponse",
    "RejectedSpot",
    "SearchMode",
    "SpotState",
    "SpotStatus",
    "SpotType",
    "ZoneCount",
    "now_vvo",
    "to_vvo",
]

#: Владивосток, UTC+10 без переходов на летнее время.
VLADIVOSTOK = timezone(timedelta(hours=10), name="UTC+10")

Confidence = Annotated[float, Field(ge=0.0, le=1.0)]
"""Уверенность модели, float в диапазоне 0..1 включительно."""


def now_vvo() -> datetime:
    """Текущее время во владивостокской зоне (timezone-aware)."""
    return datetime.now(VLADIVOSTOK)


def to_vvo(value: datetime) -> datetime:
    """Привести aware-datetime к зоне Владивостока."""
    if value.tzinfo is None:
        raise ValueError("ожидается timezone-aware datetime")
    return value.astimezone(VLADIVOSTOK)


# --------------------------------------------------------------------------- #
# Перечисления
# --------------------------------------------------------------------------- #


class SpotStatus(str, Enum):
    """Статус места. ``unknown`` допустим и в БД, и в payload детекций."""

    FREE = "free"
    OCCUPIED = "occupied"
    UNKNOWN = "unknown"


class SpotType(str, Enum):
    STANDARD = "standard"
    DISABLED = "disabled"
    ELECTRIC = "electric"


class ParkingType(str, Enum):
    FREE = "free"
    PAID = "paid"
    RESTRICTED = "restricted"


class CameraStatus(str, Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    ERROR = "error"


class SearchMode(str, Enum):
    CLOSEST = "closest"
    FASTEST = "fastest"


# --------------------------------------------------------------------------- #
# Базовые классы
# --------------------------------------------------------------------------- #


class _Base(BaseModel):
    """Общая конфигурация: строгий набор полей, enum сериализуются как строки."""

    model_config = ConfigDict(
        extra="forbid",
        use_enum_values=False,
        ser_json_timedelta="iso8601",
        validate_assignment=True,
    )


class _AwareTimestampMixin:
    """Валидация и сериализация timezone-aware меток времени."""

    @staticmethod
    def _require_aware(value: datetime, field_name: str) -> datetime:
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ValueError(
                f"{field_name}: требуется timezone-aware datetime "
                f"(для наших камер это +10:00, Владивосток)"
            )
        return value


# --------------------------------------------------------------------------- #
# Модели данных из CLAUDE.md (зеркала «байт-в-байт»)
# --------------------------------------------------------------------------- #


class ParkingLot(_Base):
    """Парковка. CLAUDE.md -> «Модель данных / ParkingLot».

    В гибридной разметке этой же моделью описывается «зона» — площадка без
    краски на асфальте, у которой нет индивидуальных мест. Признак — поле
    ``has_discrete_spots=False`` (EXT).
    """

    id: str = Field(min_length=1, examples=["P-001"])
    name: str = Field(min_length=1)
    latitude: float = Field(ge=-90.0, le=90.0)
    longitude: float = Field(ge=-180.0, le=180.0)
    capacity: int = Field(ge=0)
    parking_type: ParkingType = ParkingType.FREE
    access_rules: str = ""
    entrance_lat: float = Field(ge=-90.0, le=90.0)
    entrance_lon: float = Field(ge=-180.0, le=180.0)

    # EXT: гибридная разметка. True  — на площадке есть краска, места дискретные
    # и приходят в `detections`; False — зона, приходит только `zone_counts`.
    has_discrete_spots: bool = True
    # EXT: id зоны, если площадка размечена как зона (Z-xxx). Нужен, чтобы
    # связать `zone_counts[].zone_id` с записью парковки в БД.
    zone_id: str | None = None


class ParkingSpot(_Base):
    """Парковочное место. CLAUDE.md -> «Модель данных / ParkingSpot»."""

    id: str = Field(min_length=1, examples=["S-014"])
    parking_lot_id: str = Field(min_length=1)
    camera_id: str = Field(min_length=1)
    latitude: float = Field(ge=-90.0, le=90.0)
    longitude: float = Field(ge=-180.0, le=180.0)
    approach_lat: float = Field(ge=-90.0, le=90.0)
    approach_lon: float = Field(ge=-180.0, le=180.0)
    roi_polygon: list[tuple[int, int]] = Field(min_length=3)
    # EXT: разрешение кадра, в координатах которого размечен roi_polygon.
    # Без него смена разрешения потока молча ломает всю разметку.
    roi_frame_size: tuple[int, int]
    spot_type: SpotType = SpotType.STANDARD
    status: SpotStatus = SpotStatus.UNKNOWN
    confidence: Confidence = 0.0
    updated_at: datetime

    @field_validator("updated_at")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        return _AwareTimestampMixin._require_aware(v, "updated_at")

    @field_validator("roi_frame_size")
    @classmethod
    def _positive_frame(cls, v: tuple[int, int]) -> tuple[int, int]:
        w, h = v
        if w <= 0 or h <= 0:
            raise ValueError("roi_frame_size: ширина и высота должны быть > 0")
        return v

    @model_validator(mode="after")
    def _roi_inside_frame(self) -> ParkingSpot:
        w, h = self.roi_frame_size
        for x, y in self.roi_polygon:
            if not (0 <= x <= w and 0 <= y <= h):
                raise ValueError(
                    f"roi_polygon: точка ({x}, {y}) вне кадра {w}x{h} "
                    f"(проверь roi_frame_size)"
                )
        return self

    @field_serializer("updated_at")
    def _ser_updated_at(self, v: datetime) -> str:
        return v.isoformat()


class Camera(_Base):
    """Камера. CLAUDE.md -> «Модель данных / Camera»."""

    id: str = Field(min_length=1, examples=["CAM-001"])
    parking_lot_id: str = Field(min_length=1)
    source_reference: str = Field(min_length=1)
    status: CameraStatus = CameraStatus.ACTIVE
    last_frame_at: datetime | None = None
    # EXT: разрешение потока. Позволяет backend/админке проверить, что
    # roi_frame_size мест совпадает с текущим разрешением камеры.
    frame_size: tuple[int, int] | None = None

    @field_validator("last_frame_at")
    @classmethod
    def _aware(cls, v: datetime | None) -> datetime | None:
        if v is None:
            return None
        return _AwareTimestampMixin._require_aware(v, "last_frame_at")

    @field_serializer("last_frame_at")
    def _ser_last_frame_at(self, v: datetime | None) -> str | None:
        return None if v is None else v.isoformat()


class OccupancyHistory(_Base):
    """Запись истории занятости. CLAUDE.md -> «Модель данных / OccupancyHistory»."""

    id: int
    spot_id: str = Field(min_length=1)
    status: SpotStatus
    confidence: Confidence
    observed_at: datetime

    @field_validator("observed_at")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        return _AwareTimestampMixin._require_aware(v, "observed_at")

    @field_serializer("observed_at")
    def _ser_observed_at(self, v: datetime) -> str:
        return v.isoformat()


# --------------------------------------------------------------------------- #
# POST /internal/occupancy
# --------------------------------------------------------------------------- #


class Detection(_Base):
    """Одна детекция в payload. CLAUDE.md -> «POST /internal/occupancy».

    ``status`` может быть ``unknown``: в модели ParkingSpot он есть, в примере
    запроса его нет — мы его поддерживаем и шлём (см. пункт 8 контракта).
    """

    spot_id: str = Field(min_length=1, examples=["S-014"])
    status: SpotStatus
    confidence: Confidence


class ZoneCount(_Base):
    """EXT: агрегированный счётчик по зоне без дискретных мест.

    Инвариант: ``free + occupied + unknown == capacity``.
    """

    zone_id: str = Field(min_length=1, examples=["Z-301"])
    capacity: int = Field(ge=0)
    free: int = Field(ge=0)
    occupied: int = Field(ge=0)
    unknown: int = Field(default=0, ge=0)
    confidence: Confidence = 0.0

    @model_validator(mode="after")
    def _counts_match_capacity(self) -> ZoneCount:
        total = self.free + self.occupied + self.unknown
        if total != self.capacity:
            raise ValueError(
                f"zone_counts[{self.zone_id}]: free+occupied+unknown={total} "
                f"!= capacity={self.capacity}"
            )
        return self


class OccupancyPayload(_Base):
    """Снапшот камеры, отправляемый в ``POST /api/v1/internal/occupancy``.

    Обязательные поля (ровно как в CLAUDE.md): ``camera_id``, ``timestamp``,
    ``detections``. Все остальные — additive-расширения с умолчаниями, поэтому
    payload из спеки остаётся валидным.

    ``timestamp`` — время КАДРА, а не время отправки (см. пункт 9 контракта).
    """

    camera_id: str = Field(min_length=1, examples=["CAM-001"])
    timestamp: datetime
    detections: list[Detection] = Field(default_factory=list)

    # EXT: идентификатор кадра для дедупликации ретраев: "{camera_id}-{unix_ts}".
    frame_id: str | None = None
    # EXT: здоровье камеры на момент кадра -> Camera.status / Camera.last_frame_at.
    camera_status: CameraStatus | None = None
    # EXT: счётчики по зонам без дискретных мест.
    zone_counts: list[ZoneCount] = Field(default_factory=list)

    @field_validator("timestamp")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        return _AwareTimestampMixin._require_aware(v, "timestamp")

    @model_validator(mode="after")
    def _non_empty(self) -> OccupancyPayload:
        if not self.detections and not self.zone_counts:
            raise ValueError(
                "payload пуст: нужен хотя бы один элемент в detections "
                "или в zone_counts"
            )
        seen: set[str] = set()
        for d in self.detections:
            if d.spot_id in seen:
                raise ValueError(f"detections: дубликат spot_id={d.spot_id}")
            seen.add(d.spot_id)
        seen_zones: set[str] = set()
        for z in self.zone_counts:
            if z.zone_id in seen_zones:
                raise ValueError(f"zone_counts: дубликат zone_id={z.zone_id}")
            seen_zones.add(z.zone_id)
        return self

    @field_serializer("timestamp")
    def _ser_timestamp(self, v: datetime) -> str:
        return v.isoformat()

    # -- утилиты, нужные клиенту ------------------------------------------- #

    def sort_key(self) -> tuple[str, str]:
        """Ключ упорядочивания снапшотов при дозаливке буфера."""
        return (self.timestamp.astimezone(timezone.utc).isoformat(), self.camera_id)

    def with_frame_id(self) -> OccupancyPayload:
        """Вернуть копию с проставленным ``frame_id``, если его не было."""
        if self.frame_id:
            return self
        unix_ts = int(self.timestamp.timestamp())
        return self.model_copy(update={"frame_id": f"{self.camera_id}-{unix_ts}"})

    def apply_confidence_threshold(self, threshold: float) -> OccupancyPayload:
        """Детекции с ``confidence < threshold`` понизить до ``unknown``.

        CLAUDE.md -> «Обработка ошибок CV»: статус ``unknown`` выставляется при
        низкой уверенности модели. Значение порога — env ``CONFIDENCE_THRESHOLD``.
        """
        changed = False
        new_detections: list[Detection] = []
        for d in self.detections:
            if d.status is not SpotStatus.UNKNOWN and d.confidence < threshold:
                new_detections.append(
                    Detection(
                        spot_id=d.spot_id,
                        status=SpotStatus.UNKNOWN,
                        confidence=d.confidence,
                    )
                )
                changed = True
            else:
                new_detections.append(d)
        if not changed:
            return self
        return self.model_copy(update={"detections": new_detections})

    def summary(self) -> dict[str, Any]:
        """Компактная сводка для структурированных логов (без тела payload)."""
        counts: dict[str, int] = {"free": 0, "occupied": 0, "unknown": 0}
        for d in self.detections:
            counts[d.status.value] += 1
        return {
            "camera_id": self.camera_id,
            "frame_id": self.frame_id,
            "timestamp": self.timestamp.isoformat(),
            "detections": len(self.detections),
            "zones": len(self.zone_counts),
            **{f"n_{k}": v for k, v in counts.items()},
        }


class RejectedSpot(_Base):
    """EXT: элемент списка отклонённых мест при частичном приёме."""

    spot_id: str
    reason: str = "unknown_spot_id"


class OccupancyResponse(_Base):
    """Ответ ``POST /internal/occupancy``. CLAUDE.md -> тот же раздел.

    Обязательные поля спеки: ``status``, ``processed``, ``timestamp``.
    EXT-поля — для частичного приёма и дедупликации.
    """

    status: str = Field(examples=["accepted"])
    processed: int = Field(ge=0)
    timestamp: datetime

    # EXT: частичный приём — места, которых backend не знает (пункт 7).
    rejected: list[RejectedSpot] = Field(default_factory=list)
    # EXT: дедуп по frame_id — снапшот с таким frame_id уже принят (пункт 6).
    duplicate: bool = False

    @field_validator("timestamp")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        return _AwareTimestampMixin._require_aware(v, "timestamp")

    @field_serializer("timestamp")
    def _ser_timestamp(self, v: datetime) -> str:
        return v.isoformat()


# --------------------------------------------------------------------------- #
# GET /health
# --------------------------------------------------------------------------- #


class HealthResponse(_Base):
    """Ответ ``GET /health``. CLAUDE.md -> «Внутренние endpoints / GET /health»."""

    status: str = Field(examples=["healthy"])
    version: str = Field(examples=["1.0.0"])
    timestamp: datetime
    database: str = Field(examples=["connected"])
    cv_service: str = Field(examples=["active"])

    @field_validator("timestamp")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        return _AwareTimestampMixin._require_aware(v, "timestamp")

    @field_serializer("timestamp")
    def _ser_timestamp(self, v: datetime) -> str:
        return v.isoformat()


# --------------------------------------------------------------------------- #
# Публичные endpoints мобильного приложения (зеркала для сверки с Astra)
# --------------------------------------------------------------------------- #


class GeoPoint(_Base):
    """``{"lat": ..., "lon": ...}`` — форма координат в публичных ответах."""

    lat: float = Field(ge=-90.0, le=90.0)
    lon: float = Field(ge=-180.0, le=180.0)


class ParkingSummary(_Base):
    """Элемент ``GET /parkings``."""

    id: str
    name: str
    latitude: float
    longitude: float
    capacity: int = Field(ge=0)
    free_spots: int = Field(ge=0)
    occupied_spots: int = Field(ge=0)
    unknown_spots: int = Field(ge=0)
    parking_type: ParkingType
    last_update: datetime

    @field_serializer("last_update")
    def _ser(self, v: datetime) -> str:
        return v.isoformat()


class ParkingDetail(_Base):
    """Ответ ``GET /parkings/{id}``."""

    id: str
    name: str
    latitude: float
    longitude: float
    capacity: int = Field(ge=0)
    free_spots: int = Field(ge=0)
    parking_type: ParkingType
    access_rules: str
    entrance: GeoPoint
    updated_at: datetime

    @field_serializer("updated_at")
    def _ser(self, v: datetime) -> str:
        return v.isoformat()


class SpotState(_Base):
    """Элемент ``GET /parkings/{id}/spots`` и ответ ``GET /spots/{id}``."""

    spot_id: str
    parking_id: str
    status: SpotStatus
    confidence: Confidence
    location: GeoPoint
    approach_point: GeoPoint
    updated_at: datetime
    forecast_available: bool = False

    @field_serializer("updated_at")
    def _ser(self, v: datetime) -> str:
        return v.isoformat()


class RecommendationRequest(_Base):
    """Тело ``POST /recommendations``."""

    user_lat: float
    user_lon: float
    destination_lat: float
    destination_lon: float
    search_mode: SearchMode = SearchMode.CLOSEST
    arrival_eta_minutes: int = Field(default=10, ge=0)


class Recommendation(_Base):
    """Элемент ответа ``POST /recommendations``."""

    spot_id: str
    parking_id: str
    parking_name: str
    status: SpotStatus
    confidence: Confidence
    location: GeoPoint
    approach_point: GeoPoint
    walking_distance_m: int = Field(ge=0)
    driving_eta_min: int = Field(ge=0)
    score: float
    updated_at: datetime

    @field_serializer("updated_at")
    def _ser(self, v: datetime) -> str:
        return v.isoformat()


class RecommendationsResponse(_Base):
    recommendations: list[Recommendation] = Field(default_factory=list)
    search_mode: SearchMode
