#!/usr/bin/env python3
"""Экспорт YOLOv8n/YOLO11n в ONNX + статическая int8-квантизация на НАШИХ кадрах.

Почему калибруемся не на COCO: у нас ракурс сверху-вбок, мелкие объекты,
серо-коричневый Владивосток в ноябре и тени на половину кадра. Диапазоны
активаций на таких картинках заметно уже и смещены относительно COCO-валидации;
калибровка на 200 своих тайлах даёт честные масштабы квантования.

Скрипт идемпотентный: с --skip-if-exists ничего не делает, если готовая модель
уже лежит в models/ и её SHA256 совпадает с models/CHECKSUMS.txt.

    python tools/export_model.py --frames frames --calib 200
    python tools/export_model.py --skip-if-exists
    python tools/export_model.py --model yolo11n.pt --no-quantize

Сами .onnx в .gitignore (десятки мегабайт), в репозиторий едет только
models/CHECKSUMS.txt — по нему проверяется, что у всех одна и та же модель.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import random
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

MODELS_DIR = ROOT / "models"
CHECKSUMS = MODELS_DIR / "CHECKSUMS.txt"
IMGSZ = 640
DEFAULT_CALIB = 200


# --------------------------------------------------------------------------- #
# Утилиты
# --------------------------------------------------------------------------- #


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_checksums() -> dict[str, str]:
    if not CHECKSUMS.exists():
        return {}
    out: dict[str, str] = {}
    for line in CHECKSUMS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            out[parts[1].lstrip("*")] = parts[0]
    return out


def write_checksums(entries: dict[str, str], notes: list[str]) -> None:
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    lines = [
        "# ParkVision — контрольные суммы моделей детектора.",
        "# Сами .onnx в .gitignore. Этот файл — чтобы у всех была одна модель.",
        "# Пересоздать: python tools/export_model.py",
        f"# Сгенерировано: {time.strftime('%Y-%m-%dT%H:%M:%S%z')}",
    ]
    lines += [f"# {n}" for n in notes]
    lines.append("")
    for name in sorted(entries):
        lines.append(f"{entries[name]}  {name}")
    CHECKSUMS.write_text("\n".join(lines) + "\n", encoding="utf-8")


def verify(path: Path) -> bool:
    """Файл на месте и его SHA256 совпадает с записанной."""
    if not path.exists():
        return False
    known = read_checksums().get(path.name)
    return bool(known) and known == sha256(path)


# --------------------------------------------------------------------------- #
# Калибровочная выборка
# --------------------------------------------------------------------------- #


def collect_calibration_tiles(
    frames_dir: Path,
    config_path: Path,
    count: int,
    seed: int = 1313,
) -> list["np.ndarray"]:  # noqa: F821
    """Нарезать `count` тайлов 640x640 из реальных кадров рекордера.

    Берём тайлы по тем же областям парковок, что и в проде: калибровать по
    небу и многоэтажкам бессмысленно, детектор туда не смотрит. Кадры
    перемешиваются по камерам и по времени, иначе калибровка уедет
    в освещение одного часа.
    """
    import cv2
    import numpy as np

    from cvsvc.config import load_config
    from cvsvc.detect.tiler import plan_tiles_for_camera
    from cvsvc.detect.tiles_proposed import apply_to_config
    from cvsvc.detect.yolo_onnx import letterbox

    config = load_config(config_path, require_rois=False)
    applied = apply_to_config(config)
    if applied:
        print(f"  tiles из tiles_proposed.py для: {', '.join(applied)}")

    rng = random.Random(seed)
    per_camera: dict[str, list[Path]] = {}
    for cam in config.cameras:
        files = sorted((frames_dir / cam.id).rglob("*.jpg"))
        if files:
            per_camera[cam.id] = files

    if not per_camera:
        refs = sorted((ROOT / "refs").glob("CAM-*.jpg"))
        if not refs:
            raise SystemExit(
                f"нет кадров ни в {frames_dir}, ни в refs/. "
                f"Сначала запустите рекордер: python -m cvsvc.ingest.recorder"
            )
        print(
            f"  ВНИМАНИЕ: в {frames_dir} пусто, калибруюсь по {len(refs)} эталонным "
            f"кадрам refs/. Это хуже: один момент времени, одно освещение."
        )
        per_camera = {p.stem: [p] for p in refs}

    total_frames = sum(len(v) for v in per_camera.values())
    print(f"  доступно кадров: {total_frames} по {len(per_camera)} камерам")

    tiles_out: list[np.ndarray] = []
    # Раунд-робин по камерам, чтобы ни одна не доминировала в выборке.
    cursors = {cid: 0 for cid in per_camera}
    order = {cid: rng.sample(files, len(files)) for cid, files in per_camera.items()}
    cam_ids = sorted(per_camera)
    guard = 0
    while len(tiles_out) < count and guard < count * 20:
        guard += 1
        for cid in cam_ids:
            if len(tiles_out) >= count:
                break
            files = order[cid]
            path = files[cursors[cid] % len(files)]
            cursors[cid] += 1
            img = cv2.imread(str(path), cv2.IMREAD_COLOR)
            if img is None:
                continue
            try:
                cam = config.camera(cid)
            except KeyError:
                continue
            h, w = img.shape[:2]
            plan = plan_tiles_for_camera(cam, (w, h))
            if not plan:
                continue
            tile = plan[rng.randrange(len(plan))]
            lb, _, _, _ = letterbox(tile.crop(img), IMGSZ)
            tiles_out.append(lb)

    print(f"  собрано калибровочных тайлов: {len(tiles_out)}")
    return tiles_out[:count]


# --------------------------------------------------------------------------- #
# Шаги
# --------------------------------------------------------------------------- #


def export_onnx(weights: str, out_path: Path, opset: int) -> Path:
    """Скачать .pt (если нужно) и экспортировать в ONNX с динамической батч-осью."""
    from ultralytics import YOLO

    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    pt_path = MODELS_DIR / Path(weights).name
    model = YOLO(str(pt_path) if pt_path.exists() else weights)
    # ultralytics кладёт .pt рядом с cwd — прибираем в models/.
    src_pt = Path(getattr(model, "ckpt_path", "") or weights)
    if src_pt.exists() and src_pt.resolve() != pt_path.resolve():
        shutil.move(str(src_pt), str(pt_path))

    # dynamic=True: батч-ось динамическая, мы отдаём все тайлы кадра одним
    # прогоном. simplify=False — onnxsim на 3.12 тянет лишнее, а выигрыша нет.
    exported = model.export(
        format="onnx",
        imgsz=IMGSZ,
        opset=opset,
        dynamic=True,
        simplify=False,
        half=False,
        device="cpu",
    )
    exported = Path(exported)
    if exported.resolve() != out_path.resolve():
        shutil.move(str(exported), str(out_path))
    return out_path


def head_nodes(model_path: Path, prefix: str) -> list[str]:
    """Имена узлов головы детекции — их НЕ квантуем.

    Проверено на наших кадрах: если квантовать всю сеть целиком, боксы
    выживают, а скоры классов схлопываются ровно в 0.0 — модель перестаёт
    детектить что-либо вообще. Виновата голова: логиты классов до сигмоиды
    лежат в широком диапазоне, а после int8-округления сигмоида выдаёт ноль.
    Голова — это единицы процентов FLOPs, оставить её в fp32 почти бесплатно.
    """
    import onnx

    model = onnx.load(str(model_path), load_external_data=False)
    return [n.name for n in model.graph.node if n.name and n.name.startswith(prefix)]


def quantize_int8(
    fp32: Path,
    out_path: Path,
    calib_tiles,
    per_channel: bool,
    exclude_prefix: str = "/model.22/",
) -> Path:
    """Статическая int8-квантизация (QDQ) с калибровкой на наших тайлах."""
    import numpy as np
    import onnxruntime
    from onnxruntime.quantization import (
        CalibrationDataReader,
        CalibrationMethod,
        QuantFormat,
        QuantType,
        quantize_static,
    )
    from onnxruntime.quantization.shape_inference import quant_pre_process

    sess = onnxruntime.InferenceSession(str(fp32), providers=["CPUExecutionProvider"])
    input_name = sess.get_inputs()[0].name
    del sess

    class Reader(CalibrationDataReader):
        def __init__(self, tiles) -> None:
            self._data = tiles
            self._i = 0

        def get_next(self):
            if self._i >= len(self._data):
                return None
            img = self._data[self._i]
            self._i += 1
            blob = img[:, :, ::-1].transpose(2, 0, 1)[None].astype(np.float32) / 255.0
            return {input_name: np.ascontiguousarray(blob)}

        def rewind(self) -> None:
            self._i = 0

    prepped = fp32.with_name(fp32.stem + ".prep.onnx")
    print("  shape inference / pre-process...")
    # skip_symbolic_shape=True обязателен: у модели динамическая батч-ось, и
    # символьный вывод формы в ORT на ней падает «Incomplete symbolic shape
    # inference». Обычного onnx shape inference для квантизации достаточно.
    quant_pre_process(str(fp32), str(prepped), skip_symbolic_shape=True)

    excluded = head_nodes(prepped, exclude_prefix) if exclude_prefix else []
    print(
        f"  калибровка на {len(calib_tiles)} тайлах (MinMax, QDQ), "
        f"узлов головы вне квантизации: {len(excluded)}"
    )
    t0 = time.time()
    quantize_static(
        model_input=str(prepped),
        model_output=str(out_path),
        calibration_data_reader=Reader(calib_tiles),
        quant_format=QuantFormat.QDQ,
        activation_type=QuantType.QUInt8,
        weight_type=QuantType.QInt8,
        per_channel=per_channel,
        reduce_range=False,
        calibrate_method=CalibrationMethod.MinMax,
        nodes_to_exclude=excluded,
        extra_options={"CalibMovingAverage": True, "CalibMovingAverageConstant": 0.01},
    )
    print(f"  квантизация заняла {time.time() - t0:.1f} с")
    prepped.unlink(missing_ok=True)
    return out_path


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default="yolov8n.pt", help="веса ultralytics (yolov8n.pt | yolo11n.pt)")
    ap.add_argument("--frames", default=str(ROOT / "frames"), help="архив кадров рекордера")
    ap.add_argument("--config", default=str(ROOT / "config" / "cameras.yml"))
    ap.add_argument("--calib", type=int, default=DEFAULT_CALIB, help="сколько тайлов на калибровку")
    ap.add_argument("--opset", type=int, default=13)
    ap.add_argument("--no-quantize", action="store_true", help="только fp32 ONNX")
    ap.add_argument("--no-per-channel", action="store_true", help="квантизация весов по тензору, а не по каналу")
    ap.add_argument(
        "--exclude-prefix",
        default="/model.22/",
        help="префикс имён узлов головы детекции, которые не квантуются "
             "(пустая строка — квантовать всё; тогда скоры схлопнутся в 0)",
    )
    ap.add_argument("--skip-if-exists", action="store_true", help="ничего не делать, если модели уже на месте")
    args = ap.parse_args(argv)

    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    stem = Path(args.model).stem
    fp32_path = MODELS_DIR / f"{stem}.onnx"
    int8_path = MODELS_DIR / f"{stem}-int8.onnx"
    want_int8 = not args.no_quantize

    if args.skip_if_exists:
        done = verify(fp32_path) and (not want_int8 or verify(int8_path))
        if done:
            print(f"модели на месте и контрольные суммы сходятся: {fp32_path.name}"
                  + (f", {int8_path.name}" if want_int8 else ""))
            return 0
        print("--skip-if-exists: моделей нет или суммы не сходятся, экспортирую заново")

    notes: list[str] = [f"модель: {args.model}, imgsz={IMGSZ}, opset={args.opset}, dynamic batch"]

    print(f"[1/3] экспорт {args.model} -> {fp32_path.name}")
    if verify(fp32_path):
        print("  уже есть, сумма сходится — пропускаю")
    else:
        export_onnx(args.model, fp32_path, args.opset)
        print(f"  готово: {fp32_path} ({fp32_path.stat().st_size / 1e6:.1f} МБ)")

    entries = {fp32_path.name: sha256(fp32_path)}

    if want_int8:
        print(f"[2/3] калибровочная выборка из {args.frames}")
        tiles = collect_calibration_tiles(
            Path(args.frames), Path(args.config), args.calib
        )
        notes.append(f"калибровка: {len(tiles)} тайлов 640x640 из наших кадров (не COCO)")

        print(f"[3/3] int8 -> {int8_path.name}")
        quantize_int8(
            fp32_path,
            int8_path,
            tiles,
            per_channel=not args.no_per_channel,
            exclude_prefix=args.exclude_prefix,
        )
        notes.append(
            f"голова детекции ({args.exclude_prefix}) оставлена в fp32: "
            f"полная int8-квантизация обнуляет скоры классов"
        )
        print(f"  готово: {int8_path} ({int8_path.stat().st_size / 1e6:.1f} МБ)")
        entries[int8_path.name] = sha256(int8_path)

    write_checksums(entries, notes)
    print(f"\nконтрольные суммы записаны в {CHECKSUMS}")
    for name, digest in sorted(entries.items()):
        print(f"  {digest}  {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
