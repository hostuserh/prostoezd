"""Стабилизация статусов мест: гистерезис, свежесть, честный `unknown`.

Детектор работает по одному кадру и шумит: машина мелькнула на проезде через
полигон, тень легла, бокс не нашёлся из-за блика. Сырой статус отдавать нельзя —
место будет мигать несколько раз в минуту, и приложение станет бесполезным.

Правила:

* **Гистерезис N из M.** Статус меняется только когда 3 из последних 5
  наблюдений согласны на новом значении. Это же требование есть в псевдокоде
  `CLAUDE.md` («только если 3 кадра подряд показали изменение»).
* **Проезжающие машины.** Авто, пересекающее полигон, даёт ложное `occupied`.
  Поэтому мало увидеть машину — её след (footprint) должен стоять на месте
  между циклами. Уехавший дальше `MAX_FOOTPRINT_DRIFT_PX` считается транзитом
  и в голосовании за `occupied` не участвует.
* **Свежесть.** Нет наблюдений дольше `STALE_AFTER_S` — место уходит в
  `unknown`. Спека требует прямо: устаревшее НЕ показывать как `free`.
* **Причина у каждого `unknown`.** Одно место — одна причина, она попадает
  в лог и в отладочный оверлей. «Просто unknown» без причины отлаживать нельзя.

Модуль намеренно не зависит от типа `Detection` из `cvsvc.detect`: на вход
подаются простые `Observation`. Связывание «боксы → места» делает `assign.py`.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum

# Требование CLAUDE.md: данные старше 60 с считаются устаревшими.
STALE_AFTER_S = 60.0
# Голосование: сколько из скольких последних наблюдений должны совпасть.
VOTE_WINDOW = 5
VOTE_REQUIRED = 3
# Насколько след машины может сместиться между циклами и всё ещё считаться
# припаркованной. Больше — это проезжающий транспорт.
MAX_FOOTPRINT_DRIFT_PX = 18.0
# Ниже этого порога уверенность детектора не считается достаточной.
MIN_CONFIDENCE = 0.7


class SpotStatus(str, Enum):
    FREE = "free"
    OCCUPIED = "occupied"
    UNKNOWN = "unknown"


class UnknownReason(str, Enum):
    NO_HISTORY = "no_history"            # место только что появилось
    STALE = "stale"                      # нет свежих наблюдений > 60 с
    SLATE = "slate"                      # поток отдал заглушку
    FROZEN = "frozen"                    # картинка зависла
    NIGHT = "night"                      # темно, детектору не верим
    LOW_CONTRAST = "low_contrast"        # туман / снег / залепленный объектив
    CAMERA_DRIFT = "camera_drift"        # камеру сдвинуло, ROI не на месте
    LOW_CONFIDENCE = "low_confidence"    # детектор не уверен
    NO_CONSENSUS = "no_consensus"        # голоса разошлись, менять статус рано
    DISABLED = "disabled"                # место выключено в конфиге (перекрыто)
    CAMERA_ERROR = "camera_error"        # кадр вообще не удалось взять


@dataclass(frozen=True)
class Observation:
    """Сырое наблюдение по одному месту за один цикл."""

    status: SpotStatus
    confidence: float
    at: datetime
    # След машины на земле (нижний центр бокса). None, если место пустое.
    footprint: tuple[float, float] | None = None


@dataclass
class SpotVerdict:
    """Стабилизированный статус, готовый к отправке на backend."""

    spot_id: str
    status: SpotStatus
    confidence: float
    updated_at: datetime
    reason: UnknownReason | None = None
    changed: bool = False

    @property
    def is_unknown(self) -> bool:
        return self.status is SpotStatus.UNKNOWN


@dataclass
class _SpotState:
    history: deque[Observation] = field(
        default_factory=lambda: deque(maxlen=VOTE_WINDOW)
    )
    committed: SpotStatus = SpotStatus.UNKNOWN
    committed_at: datetime | None = None
    confidence: float = 0.0
    reason: UnknownReason | None = UnknownReason.NO_HISTORY


class Stabilizer:
    """Хранит историю наблюдений по местам и выдаёт устойчивые статусы.

    Один экземпляр на весь сервис. Состояние сериализуемо, чтобы рестарт
    не сбрасывал гистерезис и места не начинали мигать заново.
    """

    def __init__(
        self,
        window: int = VOTE_WINDOW,
        required: int = VOTE_REQUIRED,
        stale_after_s: float = STALE_AFTER_S,
        min_confidence: float = MIN_CONFIDENCE,
        max_drift_px: float = MAX_FOOTPRINT_DRIFT_PX,
    ) -> None:
        if required > window:
            raise ValueError("required не может быть больше window")
        self.window = window
        self.required = required
        self.stale_after = timedelta(seconds=stale_after_s)
        self.min_confidence = min_confidence
        self.max_drift_px = max_drift_px
        self._spots: dict[str, _SpotState] = {}

    # ------------------------------------------------------------------ #

    def _state(self, spot_id: str) -> _SpotState:
        st = self._spots.get(spot_id)
        if st is None:
            st = _SpotState(history=deque(maxlen=self.window))
            self._spots[spot_id] = st
        return st

    def _is_transit(self, st: _SpotState, obs: Observation) -> bool:
        """Машина, чей след заметно сдвинулся с прошлого цикла, — проезжающая."""
        if obs.footprint is None:
            return False
        for prev in reversed(st.history):
            if prev.footprint is None:
                continue
            return math.dist(prev.footprint, obs.footprint) > self.max_drift_px
        # Первое наблюдение машины: транзитом не считаем, но и голоса ей
        # не хватит — нужно ещё VOTE_REQUIRED-1 подтверждений.
        return False

    # ------------------------------------------------------------------ #

    def force_unknown(
        self, spot_ids: list[str], reason: UnknownReason, at: datetime
    ) -> list[SpotVerdict]:
        """Увести места в `unknown` без голосования.

        Для случаев, когда кадра просто нет или ему нельзя верить: заглушка,
        зависание, ночь, дрейф камеры, сетевая ошибка. История при этом
        ОЧИЩАЕТСЯ — иначе после восстановления старые голоса мгновенно вернут
        статус, который успел устареть.
        """
        out: list[SpotVerdict] = []
        for spot_id in spot_ids:
            st = self._state(spot_id)
            changed = st.committed is not SpotStatus.UNKNOWN
            st.history.clear()
            st.committed = SpotStatus.UNKNOWN
            st.committed_at = at
            st.confidence = 0.0
            st.reason = reason
            out.append(
                SpotVerdict(
                    spot_id=spot_id,
                    status=SpotStatus.UNKNOWN,
                    confidence=0.0,
                    updated_at=at,
                    reason=reason,
                    changed=changed,
                )
            )
        return out

    def observe(
        self,
        spot_id: str,
        obs: Observation,
        confidence_scale: float = 1.0,
    ) -> SpotVerdict:
        """Добавляет наблюдение и возвращает стабилизированный статус места."""
        st = self._state(spot_id)

        scaled = Observation(
            status=obs.status,
            confidence=max(0.0, min(1.0, obs.confidence * confidence_scale)),
            at=obs.at,
            footprint=obs.footprint,
        )

        # Проезжающую машину не засчитываем за занятость: её след сдвинулся.
        if scaled.status is SpotStatus.OCCUPIED and self._is_transit(st, scaled):
            scaled = Observation(
                status=SpotStatus.UNKNOWN,
                confidence=scaled.confidence,
                at=scaled.at,
                footprint=scaled.footprint,
            )

        # Детектор не уверен — это не «свободно», это «не знаю».
        if (
            scaled.status is not SpotStatus.UNKNOWN
            and scaled.confidence < self.min_confidence
        ):
            scaled = Observation(
                status=SpotStatus.UNKNOWN,
                confidence=scaled.confidence,
                at=scaled.at,
                footprint=scaled.footprint,
            )

        st.history.append(scaled)
        return self._commit(spot_id, st, scaled.at)

    # ------------------------------------------------------------------ #

    def _commit(self, spot_id: str, st: _SpotState, now: datetime) -> SpotVerdict:
        # Свежесть важнее голосов: спека запрещает показывать устаревшее как free.
        fresh = [o for o in st.history if now - o.at <= self.stale_after]
        if not fresh:
            changed = st.committed is not SpotStatus.UNKNOWN
            st.committed = SpotStatus.UNKNOWN
            st.committed_at = now
            st.confidence = 0.0
            st.reason = UnknownReason.STALE
            return SpotVerdict(
                spot_id, SpotStatus.UNKNOWN, 0.0, now, UnknownReason.STALE, changed
            )

        votes = {s: 0 for s in SpotStatus}
        for o in fresh:
            votes[o.status] += 1

        winner = max(
            (s for s in (SpotStatus.FREE, SpotStatus.OCCUPIED)),
            key=lambda s: votes[s],
        )
        changed = False
        reason: UnknownReason | None = None

        if votes[winner] >= self.required:
            changed = st.committed is not winner
            st.committed = winner
            agreeing = [o.confidence for o in fresh if o.status is winner]
            st.confidence = round(
                (votes[winner] / len(fresh)) * (sum(agreeing) / len(agreeing)), 3
            )
            st.reason = None
        else:
            # Консенсуса нет. Прошлый статус сохраняем, только если он ещё свежий,
            # иначе честно уходим в unknown.
            if st.committed is SpotStatus.UNKNOWN:
                reason = (
                    UnknownReason.NO_HISTORY
                    if len(fresh) < self.required
                    else UnknownReason.NO_CONSENSUS
                )
                st.reason = reason
                st.confidence = 0.0
            else:
                reason = st.reason

        st.committed_at = now
        return SpotVerdict(
            spot_id=spot_id,
            status=st.committed,
            confidence=st.confidence,
            updated_at=now,
            reason=st.reason,
            changed=changed,
        )

    def refresh_staleness(self, now: datetime) -> list[SpotVerdict]:
        """Переоценивает все места по времени, без новых наблюдений.

        Вызывать каждый цикл: камера могла отвалиться, и места обязаны
        протухнуть в unknown, а не остаться висеть как free.
        """
        return [self._commit(sid, st, now) for sid, st in self._spots.items()]

    # ------------------------------------------------------------------ #

    def to_dict(self) -> dict:
        """Сериализация состояния: рестарт не должен сбрасывать гистерезис."""
        return {
            "spots": {
                sid: {
                    "committed": st.committed.value,
                    "committed_at": st.committed_at.isoformat() if st.committed_at else None,
                    "confidence": st.confidence,
                    "reason": st.reason.value if st.reason else None,
                    "history": [
                        {
                            "status": o.status.value,
                            "confidence": o.confidence,
                            "at": o.at.isoformat(),
                            "footprint": list(o.footprint) if o.footprint else None,
                        }
                        for o in st.history
                    ],
                }
                for sid, st in self._spots.items()
            }
        }

    def load_dict(self, data: dict) -> None:
        for sid, raw in (data.get("spots") or {}).items():
            st = self._state(sid)
            st.committed = SpotStatus(raw["committed"])
            st.committed_at = (
                datetime.fromisoformat(raw["committed_at"]) if raw["committed_at"] else None
            )
            st.confidence = float(raw.get("confidence", 0.0))
            st.reason = UnknownReason(raw["reason"]) if raw.get("reason") else None
            st.history.clear()
            for o in raw.get("history", []):
                st.history.append(
                    Observation(
                        status=SpotStatus(o["status"]),
                        confidence=float(o["confidence"]),
                        at=datetime.fromisoformat(o["at"]),
                        footprint=tuple(o["footprint"]) if o.get("footprint") else None,
                    )
                )


def zone_counts(
    capacity: int, occupied: int, unknown: int = 0
) -> tuple[int, int, int]:
    """Счётчики зоны с соблюдением инварианта free + occupied + unknown == capacity.

    `occupied > capacity` — это ошибка конфига (неверная вместимость), а не
    реальность: вызывающий обязан это залогировать. Здесь только клампим,
    чтобы наружу не уехали отрицательные числа.
    """
    occupied = max(0, min(occupied, capacity))
    unknown = max(0, min(unknown, capacity - occupied))
    free = capacity - occupied - unknown
    return free, occupied, unknown
