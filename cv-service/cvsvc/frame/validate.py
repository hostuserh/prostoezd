"""Проверка пригодности кадра до того, как его увидит детектор.

Требование спеки (CLAUDE.md): устаревшие и неопределённые статусы НЕ показывать
как `free`. Значит плохой кадр должен уводить места в `unknown`, а не молча
скармливаться детектору, который на чёрном кадре честно не найдёт ни одной
машины и тем самым объявит всю парковку свободной.

Что проверяем:

* **Заглушка** — ловится раньше, в `ingest/hls.py` (HTTP 200 + валидное видео,
  но картинка «Трансляция заблокирована»). Сюда приходит уже как FrameError.
* **Зависание.** ⚠️ Таймкод в углу кадра тикает каждую секунду даже когда
  картинка замерла, поэтому сравниваем `content_hash` — хеш кадра БЕЗ областей
  `overlay_exclude`. По полному кадру зависание не поймать никогда.
* **Ночь.** Городские камеры ночью дают фары и тёмные пятна. Честное поведение
  для MVP — `unknown`, а не выдуманные статусы.
* **Низкий контраст.** Туман, снег, залепленный объектив. Детектируется по
  стандартному отклонению яркости.

Чего здесь НЕТ: оценки расфокуса. Честная дисперсия лапласиана требует полного
кадра и numpy; на превью 16x16 это бессмысленно. Если понадобится — считать
в детекторе, где кадр всё равно уже разжат.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from cvsvc.ingest.hls import Frame

# Средняя яркость ниже — считаем, что это ночь и детектору верить нельзя.
NIGHT_MEAN_LUMA = 45.0
# Сумеречная зона: кадр ещё рабочий, но confidence надо прижать.
DUSK_MEAN_LUMA = 70.0
# Стандартное отклонение яркости ниже — туман / снег / залепленный объектив.
MIN_CONTRAST = 12.0
# Сколько одинаковых кадров подряд считаем зависанием.
FROZEN_STREAK_THRESHOLD = 3


class FrameQuality(str, Enum):
    OK = "ok"
    DUSK = "dusk"                  # сумерки: работаем, но с пониженной уверенностью
    NIGHT = "night"
    FROZEN = "frozen"
    LOW_CONTRAST = "low_contrast"


@dataclass
class FrameVerdict:
    quality: FrameQuality
    usable: bool          # можно ли вообще отдавать кадр детектору
    reason: str
    mean_luma: float
    contrast: float
    frozen_streak: int
    # Множитель уверенности для статусов, посчитанных по этому кадру.
    confidence_scale: float = 1.0

    @property
    def is_ok(self) -> bool:
        return self.quality is FrameQuality.OK


@dataclass
class CameraFrameState:
    """Состояние между циклами. По одному объекту на камеру."""

    last_content_hash: int | None = None
    frozen_streak: int = 0
    history: list[FrameQuality] = field(default_factory=list)

    def remember(self, quality: FrameQuality, limit: int = 20) -> None:
        self.history.append(quality)
        if len(self.history) > limit:
            del self.history[0 : len(self.history) - limit]


def validate_frame(frame: Frame, state: CameraFrameState) -> FrameVerdict:
    """Оценивает кадр и обновляет состояние камеры.

    Порядок проверок — от самой жёсткой причины к самой мягкой: кадр, который
    и замёрз, и тёмный, отчитывается как замёрзший, чтобы в логе была та причина,
    с которой надо разбираться.
    """
    luma = frame.mean_luma
    contrast = frame.contrast

    # Зависание: хеш БЕЗ оверлеев не изменился.
    if state.last_content_hash is not None and frame.content_hash == state.last_content_hash:
        state.frozen_streak += 1
    else:
        state.frozen_streak = 0
    state.last_content_hash = frame.content_hash

    if state.frozen_streak >= FROZEN_STREAK_THRESHOLD:
        verdict = FrameVerdict(
            quality=FrameQuality.FROZEN,
            usable=False,
            reason=(
                f"картинка не меняется {state.frozen_streak} циклов подряд "
                f"(хеш без оверлеев совпадает) — поток завис"
            ),
            mean_luma=luma,
            contrast=contrast,
            frozen_streak=state.frozen_streak,
            confidence_scale=0.0,
        )
    elif luma < NIGHT_MEAN_LUMA:
        verdict = FrameVerdict(
            quality=FrameQuality.NIGHT,
            usable=False,
            reason=f"средняя яркость {luma:.1f} < {NIGHT_MEAN_LUMA} — ночь, детектору не верим",
            mean_luma=luma,
            contrast=contrast,
            frozen_streak=state.frozen_streak,
            confidence_scale=0.0,
        )
    elif contrast < MIN_CONTRAST:
        verdict = FrameVerdict(
            quality=FrameQuality.LOW_CONTRAST,
            usable=False,
            reason=(
                f"контраст {contrast:.1f} < {MIN_CONTRAST} — туман, снег "
                f"или залепленный объектив"
            ),
            mean_luma=luma,
            contrast=contrast,
            frozen_streak=state.frozen_streak,
            confidence_scale=0.0,
        )
    elif luma < DUSK_MEAN_LUMA:
        # Сумерки: кадр рабочий, но уверенность занижаем линейно.
        scale = 0.5 + 0.5 * (luma - NIGHT_MEAN_LUMA) / (DUSK_MEAN_LUMA - NIGHT_MEAN_LUMA)
        verdict = FrameVerdict(
            quality=FrameQuality.DUSK,
            usable=True,
            reason=f"сумерки: яркость {luma:.1f}, уверенность снижена до x{scale:.2f}",
            mean_luma=luma,
            contrast=contrast,
            frozen_streak=state.frozen_streak,
            confidence_scale=round(scale, 3),
        )
    else:
        verdict = FrameVerdict(
            quality=FrameQuality.OK,
            usable=True,
            reason="",
            mean_luma=luma,
            contrast=contrast,
            frozen_streak=state.frozen_streak,
            confidence_scale=1.0,
        )

    state.remember(verdict.quality)
    return verdict
