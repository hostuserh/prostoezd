#!/usr/bin/env python3
"""Замеры детектора: латентность/память и качество детекта.

    python tools/bench.py latency --runs 12 --model models/yolov8n-int8.onnx
    python tools/bench.py latency --compare            # fp32 и int8 рядом
    python tools/bench.py quality --render             # картинки с боксами в docs/img
    python tools/bench.py quality --score              # recall по размерным группам

Бюджет (целевой сервер 4 ГБ / 4 ядра, без GPU):
полный проход по трём камерам <= 2.5 с CPU, пиковый RSS < 1.2 ГБ.

Про «ограничение в 4 ядра»: детектор сам себя ограничивает —
intra_op_num_threads=3, inter_op=1, OMP_NUM_THREADS=3. Больше трёх потоков
ORT не поднимает, какое б�� ядро ни было доступно, так что замер на машине
с 6 ядрами не завышает результат. Абсолютные цифры всё равно зависят от
модели CPU — она указана в шапке отчёта.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DOCS = ROOT / "docs"
IMG_DIR = DOCS / "img"
FRAMES = ROOT / "frames"
CONFIG = ROOT / "config" / "cameras.yml"

BUDGET_FULL_PASS_S = 2.5
BUDGET_RSS_MB = 1200

# Граница «крупных» и «мелких» машин. Выше 60 px — must have recall 0.95,
# 30-60 px — то, ради чего вообще затевался отчёт о качестве.
SIZE_SPLIT_PX = 60.0
SIZE_MIN_PX = 30.0

# --------------------------------------------------------------------------- #
# Разметка истины (ground truth), посчитанная ГЛАЗАМИ по картинкам из docs/img.
#
# Как считалось: для каждого кадра и каждой области парковки картинка с
# нарисованными боксами открывалась в нативном масштабе; считались (а) все
# различимые единицы транспорта, стоящие В ОБЛАСТИ ПАРКОВКИ, (б) из них те,
# у которых бокса нет. Проезжающие по дороге машины в GT не входят: занятость
# они не определяют, а их детект нам всё равно не мешает (A4 отбросит по
# геометрии места).
#
# Размерная группа определяется по видимому размеру машины в кадре:
#   big   — длинная сторона > 60 px
#   small — 30-60 px
# Машины мельче 30 px не считаются вовсе: в cameras.yml такие участки
# запрещены валидатором (MIN_SPOT_LONG_SIDE_PX = 28).
#
# Формат: "CAM-ID/HHMMSS": {"big": [всего, пропущено], "small": [всего, пропущено]}
# --------------------------------------------------------------------------- #
QUALITY_GT: dict[str, dict[str, list[int]]] = {}

GT_PATH_HINT = "QUALITY_GT в tools/bench.py"


# --------------------------------------------------------------------------- #
# Общее
# --------------------------------------------------------------------------- #


def load_cameras():
    from cvsvc.config import load_config
    from cvsvc.detect.tiles_proposed import apply_to_config

    cfg = load_config(CONFIG, require_rois=False)
    applied = apply_to_config(cfg)
    return cfg, applied


def sample_frames(cam_id: str, count: int) -> list[Path]:
    """Равномерная выборка кадров по времени из архива рекордера."""
    files = sorted((FRAMES / cam_id).rglob("*.jpg"))
    if not files:
        ref = ROOT / "refs" / f"{cam_id}.jpg"
        return [ref] if ref.exists() else []
    if len(files) <= count:
        return files
    step = (len(files) - 1) / (count - 1) if count > 1 else 1
    return [files[round(i * step)] for i in range(count)]


def rss_mb() -> float:
    import resource

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # macOS отдаёт байты, Linux — килобайты.
    return peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024


def current_rss_mb() -> float:
    import psutil

    return psutil.Process().memory_info().rss / (1024 * 1024)


def host_line() -> str:
    import onnxruntime as ort

    return (
        f"{platform.platform()} | "
        f"{platform.processor() or 'cpu'} | "
        f"{os.cpu_count()} логических ядер | "
        f"python {platform.python_version()} | onnxruntime {ort.__version__}"
    )


# --------------------------------------------------------------------------- #
# Латентность
# --------------------------------------------------------------------------- #


@dataclass
class CamResult:
    cam_id: str
    tiles: int
    coverage: float
    p50_ms: float
    p95_ms: float
    mean_ms: float
    dets_mean: float


def bench_model(model_path: Path, runs: int, warmup_runs: int = 2) -> tuple[list[CamResult], float, float, float]:
    from cvsvc.detect import Detector
    from cvsvc.detect.tiler import plan_tiles_for_camera, tiles_coverage
    import cv2

    cfg, _ = load_cameras()
    det = Detector(model_path=model_path)
    det.warmup()

    # Кадры читаем заранее: нас интересует время детекта, а не диска.
    loaded: dict[str, list] = {}
    for cam in cfg.cameras:
        paths = sample_frames(cam.id, runs)
        loaded[cam.id] = [cv2.imread(str(p), cv2.IMREAD_COLOR) for p in paths]

    for cam in cfg.cameras:  # прогрев по каждому разрешению отдельно
        for _ in range(warmup_runs):
            if loaded[cam.id]:
                det.detect(loaded[cam.id][0], cam)

    results: list[CamResult] = []
    per_run_totals: list[float] = []
    for cam in cfg.cameras:
        frames = loaded[cam.id]
        if not frames:
            continue
        tiles = plan_tiles_for_camera(cam, cam.frame_size)
        times, counts = [], []
        for i in range(runs):
            frame = frames[i % len(frames)]
            t0 = time.perf_counter()
            dets = det.detect(frame, cam)
            times.append((time.perf_counter() - t0) * 1000)
            counts.append(len(dets))
        times.sort()
        results.append(
            CamResult(
                cam_id=cam.id,
                tiles=len(tiles),
                coverage=tiles_coverage(tiles, tuple(cam.frame_size)),
                p50_ms=statistics.median(times),
                p95_ms=times[min(len(times) - 1, int(round(0.95 * (len(times) - 1))))],
                mean_ms=statistics.fmean(times),
                dets_mean=statistics.fmean(counts),
            )
        )

    # Полный проход: три камеры подряд, как это делает планировщик.
    full: list[float] = []
    for i in range(runs):
        t0 = time.perf_counter()
        for cam in cfg.cameras:
            frames = loaded[cam.id]
            if frames:
                det.detect(frames[i % len(frames)], cam)
        full.append(time.perf_counter() - t0)
    full.sort()
    full_p50 = statistics.median(full)
    full_p95 = full[min(len(full) - 1, int(round(0.95 * (len(full) - 1))))]
    return results, full_p50, full_p95, current_rss_mb()


def cmd_latency(args) -> int:
    models: list[Path] = []
    if args.compare:
        models = sorted((ROOT / "models").glob("*.onnx"))
    else:
        from cvsvc.detect.yolo_onnx import resolve_model_path

        models = [resolve_model_path(args.model)]

    print(host_line())
    print(f"runs={args.runs} на камеру, intra_op_num_threads=3\n")

    report: dict[str, object] = {"host": host_line(), "runs": args.runs, "models": {}}
    for mp in models:
        res, p50, p95, rss = bench_model(mp, args.runs)
        peak = rss_mb()
        print(f"=== {mp.name} ({mp.stat().st_size / 1e6:.1f} МБ)")
        print(f"{'камера':<10} {'тайлов':>7} {'покрытие':>9} {'p50 мс':>9} {'p95 мс':>9} {'детектов':>9}")
        for r in res:
            print(
                f"{r.cam_id:<10} {r.tiles:>7} {r.coverage * 100:>8.0f}% "
                f"{r.p50_ms:>9.0f} {r.p95_ms:>9.0f} {r.dets_mean:>9.1f}"
            )
        print(f"полный проход 3 камеры: p50 {p50:.2f} с, p95 {p95:.2f} с "
              f"(бюджет {BUDGET_FULL_PASS_S} с) -> {'OK' if p95 <= BUDGET_FULL_PASS_S else 'ПРЕВЫШЕН'}")
        print(f"RSS процесса: текущий {rss:.0f} МБ, пиковый {peak:.0f} МБ "
              f"(бюджет {BUDGET_RSS_MB} МБ) -> {'OK' if peak <= BUDGET_RSS_MB else 'ПРЕВЫШЕН'}\n")
        report["models"][mp.name] = {
            "size_mb": round(mp.stat().st_size / 1e6, 1),
            "cameras": [r.__dict__ for r in res],
            "full_pass_p50_s": round(p50, 3),
            "full_pass_p95_s": round(p95, 3),
            "rss_mb": round(rss),
            "peak_rss_mb": round(peak),
        }

    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"json -> {args.json}")
    return 0


# --------------------------------------------------------------------------- #
# Качество
# --------------------------------------------------------------------------- #


def _draw(img, dets, origin, scale_note: str):
    import cv2

    ox, oy = origin
    for d in dets:
        x1, y1, x2, y2 = (int(round(v)) for v in d.bbox)
        big = d.size_px > SIZE_SPLIT_PX
        color = (0, 220, 0) if big else (0, 170, 255)  # BGR: крупные зелёные, мелкие оранжевые
        cv2.rectangle(img, (x1 - ox, y1 - oy), (x2 - ox, y2 - oy), color, 2)
        fx, fy = d.footprint
        cv2.circle(img, (int(fx - ox), int(fy - oy)), 3, (255, 0, 255), -1)
        cv2.putText(
            img, f"{d.size_px:.0f}", (x1 - ox, max(10, y1 - oy - 3)),
            cv2.FONT_HERSHEY_SIMPLEX, 0.4, color, 1, cv2.LINE_AA,
        )
    cv2.putText(img, scale_note, (6, img.shape[0] - 8), cv2.FONT_HERSHEY_SIMPLEX,
                0.5, (255, 255, 255), 2, cv2.LINE_AA)
    cv2.putText(img, scale_note, (6, img.shape[0] - 8), cv2.FONT_HERSHEY_SIMPLEX,
                0.5, (0, 0, 0), 1, cv2.LINE_AA)
    return img


def cmd_quality(args) -> int:
    if args.render:
        return _quality_render(args)
    return _quality_score(args)


def _quality_render(args) -> int:
    import cv2
    import numpy as np

    from cvsvc.detect import Detector

    cfg, _ = load_cameras()
    det = Detector(model_path=args.model)
    det.warmup()
    IMG_DIR.mkdir(parents=True, exist_ok=True)

    index: list[dict] = []
    for cam in cfg.cameras:
        for path in sample_frames(cam.id, args.frames):
            frame = cv2.imread(str(path), cv2.IMREAD_COLOR)
            if frame is None:
                continue
            dets = det.detect(frame, cam)
            panels = []
            for ri, (x1, y1, x2, y2) in enumerate(cam.tiles or [(0, 0, cam.width, cam.height)]):
                crop = frame[y1:y2, x1:x2].copy()
                inside = [
                    d
                    for d in dets
                    if x1 <= (d.bbox[0] + d.bbox[2]) / 2 <= x2
                    and y1 <= (d.bbox[1] + d.bbox[3]) / 2 <= y2
                ]
                panels.append(_draw(crop, inside, (x1, y1), f"{cam.id} region{ri} [{x1},{y1},{x2},{y2}]"))
            width = max(p.shape[1] for p in panels)
            padded = [
                np.pad(p, ((0, 6), (0, width - p.shape[1]), (0, 0)), constant_values=30)
                for p in panels
            ]
            sheet = np.concatenate(padded, axis=0)
            tag = "" if det.model_path.name.startswith("yolov8n") else f"_{det.model_path.stem}"
            name = f"{cam.id}_{path.stem}{tag}.jpg"
            cv2.imwrite(str(IMG_DIR / name), sheet, [cv2.IMWRITE_JPEG_QUALITY, 92])
            big = sum(1 for d in dets if d.size_px > SIZE_SPLIT_PX)
            small = sum(1 for d in dets if SIZE_MIN_PX <= d.size_px <= SIZE_SPLIT_PX)
            index.append(
                {"key": f"{cam.id}/{path.stem}", "image": name, "det_big": big, "det_small": small}
            )
            print(f"{name}: всего {len(dets)}, >60px {big}, 30-60px {small}")

    print(f"\n{len(index)} картинок в {IMG_DIR}")
    print(f"Теперь посчитайте истину глазами и заполните {GT_PATH_HINT}")
    return 0


def _quality_score(args) -> int:
    if not QUALITY_GT:
        print(f"{GT_PATH_HINT} пуст. Сначала: tools/bench.py quality --render", file=sys.stderr)
        return 1

    buckets = {"big": {}, "small": {}}
    per_cam: dict[str, dict[str, list[int]]] = {}
    for key, g in QUALITY_GT.items():
        cam_id = key.split("/")[0]
        slot = per_cam.setdefault(cam_id, {"big": [0, 0], "small": [0, 0]})
        for b in ("big", "small"):
            total, missed = g.get(b, [0, 0])
            slot[b][0] += total
            slot[b][1] += missed

    print(f"Кадров в разметке: {len(QUALITY_GT)}\n")
    header = f"{'камера':<10} {'>60px GT':>9} {'проп.':>6} {'recall':>8} | {'30-60 GT':>9} {'проп.':>6} {'recall':>8}"
    print(header)
    overall = {"big": [0, 0], "small": [0, 0]}
    for cam_id in sorted(per_cam):
        s = per_cam[cam_id]
        row = [f"{cam_id:<10}"]
        for b in ("big", "small"):
            total, missed = s[b]
            overall[b][0] += total
            overall[b][1] += missed
            rec = (total - missed) / total if total else float("nan")
            row.append(f"{total:>9} {missed:>6} {rec:>8.2f}")
        print(" | ".join([row[0] + " " + row[1], row[2]]))
    print("-" * len(header))
    out = {}
    for b in ("big", "small"):
        total, missed = overall[b]
        out[b] = (total - missed) / total if total else float("nan")
    print(f"{'ИТОГО':<10} {overall['big'][0]:>9} {overall['big'][1]:>6} {out['big']:>8.2f} | "
          f"{overall['small'][0]:>9} {overall['small'][1]:>6} {out['small']:>8.2f}")
    print()
    print(f"recall >60px  = {out['big']:.2f}  (must have >= 0.95) -> "
          f"{'OK' if out['big'] >= 0.95 else 'НЕ ВЫПОЛНЕН'}")
    print(f"recall 30-60px = {out['small']:.2f}  (порог обсуждения 0.80) -> "
          f"{'OK' if out['small'] >= 0.80 else 'НИЖЕ ПОРОГА, решение за командой'}")
    return 0


# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    lat = sub.add_parser("latency", help="латентность p50/p95 и пиковый RSS")
    lat.add_argument("--model", default=None)
    lat.add_argument("--runs", type=int, default=12)
    lat.add_argument("--compare", action="store_true", help="прогнать fp32 и int8 рядом")
    lat.add_argument("--json", default=None)
    lat.set_defaults(func=cmd_latency)

    q = sub.add_parser("quality", help="картинки с боксами и recall по размерным группам")
    q.add_argument("--model", default=None)
    q.add_argument("--frames", type=int, default=7, help="кадров на камеру")
    q.add_argument("--render", action="store_true")
    q.add_argument("--score", action="store_true")
    q.set_defaults(func=cmd_quality)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
