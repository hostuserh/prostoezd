"""Тесты детектора: тайлер, NMS, footprint.

Геометрия (tiler, boxes) тестируется без модели и без onnxruntime — это
быстрые тесты, они должны проходить даже на машине, где ORT не установлен.
Тест с реальным прогоном модели помечен @pytest.mark.slow.

    python -m pytest tests/test_detect.py -v            # всё
    python -m pytest tests/test_detect.py -m "not slow" # без модели
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

np = pytest.importorskip("numpy", reason="детектору нужен numpy")

from cvsvc.config import Camera  # noqa: E402
from cvsvc.detect.boxes import iou, nms  # noqa: E402
from cvsvc.detect.tiler import (  # noqa: E402
    DEFAULT_OVERLAP,
    DEFAULT_TILE_SIZE,
    Tile,
    plan_tiles,
    plan_tiles_for_camera,
    tiles_coverage,
)

ROOT = Path(__file__).resolve().parent.parent


def make_camera(
    cam_id: str = "CAM-TST",
    frame_size: tuple[int, int] = (1920, 1080),
    tiles: list[tuple[int, int, int, int]] | None = None,
    footprint_offset_px: float = 0.0,
) -> Camera:
    return Camera(
        id=cam_id,
        name=cam_id,
        parking_lot_id="P-TST",
        playlist_url="https://example.invalid/index.m3u8",
        frame_size=frame_size,
        headers={"Referer": "https://vladlink.ru/"},
        tiles=tiles or [],
        footprint_offset_px=footprint_offset_px,
    )


# --------------------------------------------------------------------------- #
# Тайлер: раскладка
# --------------------------------------------------------------------------- #


def test_пустые_tiles_тайлят_весь_кадр():
    """cam.tiles пуст -> покрываем кадр целиком, ни один пиксель не теряется."""
    tiles = plan_tiles(None, (1920, 1080))
    assert tiles, "должен быть хотя бы один тайл"
    assert tiles_coverage(tiles, (1920, 1080)) == pytest.approx(1.0)


def test_область_меньше_тайла_даёт_ровно_один_тайл():
    tiles = plan_tiles([(100, 100, 500, 400)], (1920, 1080))
    assert len(tiles) == 1
    assert tiles[0].as_rect() == (100, 100, 500, 400)


def test_тайлы_не_выходят_за_область_и_за_кадр():
    regions = [(1230, 810, 2540, 1090), (0, 0, 2592, 1944)]
    for r in regions:
        for t in plan_tiles([r], (2592, 1944)):
            assert r[0] <= t.x1 < t.x2 <= r[2]
            assert r[1] <= t.y1 < t.y2 <= r[3]


def test_перекрытие_соседних_тайлов_не_меньше_заданного():
    """Машина 120 px не должна целиком попасть в «слепую» полосу между тайлами."""
    region = (0, 0, 2000, 700)
    tiles = plan_tiles([region], (2592, 1944), tile_size=640, overlap=96)
    row = sorted({t.x1 for t in tiles})
    assert len(row) > 1, "регион шире тайла — должно быть несколько колонок"
    for a, b in zip(row, row[1:]):
        overlap = (a + 640) - b
        assert overlap >= 96, f"перекрытие {overlap} px меньше заданных 96"


def test_последний_тайл_полного_размера_а_не_огрызок():
    tiles = plan_tiles([(0, 0, 1300, 640)], (1920, 1080), tile_size=640, overlap=96)
    widths = {t.width for t in tiles}
    assert widths == {640}, f"ожидались только полные тайлы, получено {widths}"


def test_область_покрыта_тайлами_целиком():
    region = (300, 200, 2100, 1400)
    tiles = plan_tiles([region], (2592, 1944))
    mask = np.zeros((1944, 2592), dtype=bool)
    for t in tiles:
        mask[t.y1 : t.y2, t.x1 : t.x2] = True
    assert mask[region[1] : region[3], region[0] : region[2]].all()


def test_тайлинг_по_зонам_дешевле_полного_кадра():
    """Главный смысл cam.tiles: на 2592x1944 это втрое-вчетверо меньше тайлов."""
    full = plan_tiles(None, (2592, 1944))
    zoned = plan_tiles(
        [(1230, 810, 2350, 1090), (1700, 1250, 2490, 1470)], (2592, 1944)
    )
    assert len(zoned) * 2 <= len(full)


def test_регионы_обрезаются_по_границам_кадра():
    tiles = plan_tiles([(-50, -50, 5000, 5000)], (800, 600))
    assert tiles_coverage(tiles, (800, 600)) == pytest.approx(1.0)
    for t in tiles:
        assert 0 <= t.x1 < t.x2 <= 800 and 0 <= t.y1 < t.y2 <= 600


def test_вырожденный_регион_отбрасывается_и_остаётся_фолбэк_на_кадр():
    tiles = plan_tiles([(100, 100, 100, 400)], (800, 600))
    assert tiles_coverage(tiles, (800, 600)) == pytest.approx(1.0)
    assert tiles[0].as_rect()[:2] == (0, 0)


def test_overlap_больше_tile_это_ошибка():
    with pytest.raises(ValueError):
        plan_tiles([(0, 0, 2000, 2000)], (2592, 1944), tile_size=640, overlap=640)


# --------------------------------------------------------------------------- #
# Тайлер: пересчёт координат
# --------------------------------------------------------------------------- #


def test_crop_и_обратный_пересчёт_координат():
    frame = np.zeros((1080, 1920, 3), dtype=np.uint8)
    frame[700:760, 300:420] = 255  # «машина» в координатах кадра

    tile = Tile(200, 600, 840, 1080)
    crop = tile.crop(frame)
    assert crop.shape[:2] == (480, 640)

    ys, xs = np.nonzero(crop[:, :, 0])
    local = (xs.min(), ys.min(), xs.max() + 1, ys.max() + 1)
    assert local == (100, 100, 220, 160)

    assert tile.to_frame(local) == (300, 700, 420, 760)


def test_to_frame_обратно_к_исходному_боксу_для_всех_тайлов():
    box_global = (1500.0, 900.0, 1580.0, 960.0)
    for t in plan_tiles([(1230, 810, 2350, 1090)], (2592, 1944)):
        local = (
            box_global[0] - t.x1,
            box_global[1] - t.y1,
            box_global[2] - t.x1,
            box_global[3] - t.y1,
        )
        assert t.to_frame(local) == pytest.approx(box_global)


def test_координаты_масштабируются_под_реальное_разрешение_потока():
    """frame_size в конфиге 2592x1944, а поток отдал половину — ROI должны поехать."""
    cam = make_camera(frame_size=(2592, 1944), tiles=[(1296, 972, 2592, 1944)])
    tiles = plan_tiles_for_camera(cam, (1296, 972))
    assert tiles
    for t in tiles:
        assert t.x1 >= 648 and t.y1 >= 486
        assert t.x2 <= 1296 and t.y2 <= 972


def test_без_указания_размера_берётся_frame_size_камеры():
    cam = make_camera(frame_size=(1920, 1080), tiles=[(0, 500, 760, 1080)])
    tiles = plan_tiles_for_camera(cam)
    assert len(tiles) == 2
    assert min(t.x1 for t in tiles) == 0
    assert max(t.x2 for t in tiles) == 760


# --------------------------------------------------------------------------- #
# NMS
# --------------------------------------------------------------------------- #


def test_iou_базовые_случаи():
    assert iou((0, 0, 10, 10), (0, 0, 10, 10)) == pytest.approx(1.0)
    assert iou((0, 0, 10, 10), (20, 20, 30, 30)) == 0.0
    assert iou((0, 0, 10, 10), (5, 0, 15, 10)) == pytest.approx(1 / 3)


def test_nms_на_пустом_входе():
    assert nms(np.zeros((0, 4)), np.zeros((0,))) == []


def test_nms_давит_дубль_машины_со_стыка_тайлов():
    """Машина в зоне перекрытия детектится дважды — должен остаться один бокс."""
    boxes = np.array([[100, 100, 200, 200], [104, 98, 205, 203]], dtype=float)
    scores = np.array([0.8, 0.6])
    keep = nms(boxes, scores, 0.5)
    assert keep == [0], "оставлен должен быть бокс с большим score"


def test_nms_не_склеивает_соседние_машины_в_плотном_ряду():
    """Две машины бок о бок (IoU ~0.1) — это две машины, а не одна."""
    boxes = np.array([[0, 0, 100, 60], [90, 0, 190, 60]], dtype=float)
    keep = nms(boxes, np.array([0.9, 0.85]), 0.5)
    assert sorted(keep) == [0, 1]


def test_nms_между_классами_по_умолчанию():
    """В одном тайле car, в соседнем truck — это одна машина, не две."""
    boxes = np.array([[10, 10, 110, 70], [12, 11, 112, 72]], dtype=float)
    scores = np.array([0.5, 0.45])
    classes = np.array([2, 7])
    assert nms(boxes, scores, 0.5, classes, class_agnostic=True) == [0]
    assert sorted(nms(boxes, scores, 0.5, classes, class_agnostic=False)) == [0, 1]


def test_nms_возвращает_индексы_по_убыванию_score():
    boxes = np.array([[0, 0, 10, 10], [100, 100, 110, 110], [200, 200, 210, 210]], float)
    scores = np.array([0.2, 0.9, 0.5])
    assert nms(boxes, scores, 0.5) == [1, 2, 0]


# --------------------------------------------------------------------------- #
# Detection / footprint
# --------------------------------------------------------------------------- #


def test_footprint_это_нижний_центр_бокса():
    from cvsvc.detect.yolo_onnx import Detection

    d = Detection(bbox=(100, 200, 180, 260), score=0.5, class_id=2)
    assert d.footprint == (140.0, 260.0)
    assert d.center == (140.0, 230.0)


def test_footprint_сдвигается_вниз_на_offset_камеры():
    """Косой ракурс: след на земле ниже низа бокса, иначе машина уедет в чужое место."""
    from cvsvc.detect.yolo_onnx import Detection

    d = Detection(bbox=(100, 200, 180, 260), score=0.5, class_id=2, footprint_offset_px=15)
    assert d.footprint == (140.0, 275.0)


def test_footprint_ниже_центра_бокса():
    from cvsvc.detect.yolo_onnx import Detection

    d = Detection(bbox=(0, 0, 100, 80), score=0.5, class_id=2, footprint_offset_px=10)
    assert d.footprint[1] > d.center[1]


def test_size_px_это_длинная_сторона():
    from cvsvc.detect.yolo_onnx import Detection

    assert Detection((0, 0, 120, 60), 0.5, 2).size_px == 120
    assert Detection((0, 0, 40, 95), 0.5, 2).size_px == 95


def test_имена_классов_coco():
    from cvsvc.detect.yolo_onnx import COCO_VEHICLE_CLASSES, Detection

    assert COCO_VEHICLE_CLASSES == {2: "car", 3: "motorcycle", 5: "bus", 7: "truck"}
    assert Detection((0, 0, 1, 1), 0.3, 7).class_name == "truck"


# --------------------------------------------------------------------------- #
# Препроцессинг и декод
# --------------------------------------------------------------------------- #


def test_letterbox_сохраняет_пропорции_и_не_ресайзит_полный_тайл():
    from cvsvc.detect.yolo_onnx import letterbox

    img = np.zeros((640, 640, 3), dtype=np.uint8)
    out, gain, px, py = letterbox(img, 640)
    assert (gain, px, py) == (1.0, 0, 0)
    assert out.shape == (640, 640, 3)

    img = np.zeros((240, 640, 3), dtype=np.uint8)
    out, gain, px, py = letterbox(img, 640)
    assert out.shape == (640, 640, 3)
    assert gain == pytest.approx(1.0)
    assert px == 0 and py == 200


def test_letterbox_обратное_преобразование_точка_в_точку():
    from cvsvc.detect.yolo_onnx import letterbox

    img = np.zeros((500, 900, 3), dtype=np.uint8)
    _, gain, px, py = letterbox(img, 640)
    for sx, sy in ((0, 0), (450, 250), (899, 499)):
        lx, ly = sx * gain + px, sy * gain + py
        assert (lx - px) / gain == pytest.approx(sx)
        assert (ly - py) / gain == pytest.approx(sy)


def test_decode_yolo_output_фильтрует_по_классам_и_порогу():
    from cvsvc.detect.yolo_onnx import decode_yolo_output

    out = np.zeros((84, 3), dtype=np.float32)
    out[:4, 0] = [100, 100, 40, 20]   # car, пройдёт
    out[4 + 2, 0] = 0.9
    out[:4, 1] = [300, 300, 40, 20]   # person (класс 0) — не транспорт
    out[4 + 0, 1] = 0.95
    out[:4, 2] = [500, 500, 40, 20]   # car, но score ниже порога
    out[4 + 2, 2] = 0.05

    boxes, scores, classes = decode_yolo_output(out, conf=0.2, keep_classes=(2, 3, 5, 7))
    assert len(boxes) == 1
    assert classes[0] == 2
    assert scores[0] == pytest.approx(0.9)
    assert tuple(boxes[0]) == pytest.approx((80, 90, 120, 110))


def test_decode_принимает_и_транспонированный_и_батчевый_выход():
    from cvsvc.detect.yolo_onnx import decode_yolo_output

    out = np.zeros((84, 2), dtype=np.float32)
    out[:4, 0] = [10, 10, 4, 4]
    out[6, 0] = 0.8
    a = decode_yolo_output(out, 0.2, (2,))
    b = decode_yolo_output(out.T, 0.2, (2,))
    c = decode_yolo_output(out[None, ...], 0.2, (2,))
    for x, y in zip(a, b):
        assert np.allclose(x, y)
    for x, y in zip(a, c):
        assert np.allclose(x, y)


# --------------------------------------------------------------------------- #
# Прогон модели на реальном кадре
# --------------------------------------------------------------------------- #


@pytest.mark.slow
def test_детект_на_реальном_кадре():
    """Сквозной прогон: модель находит транспорт на эталонном кадре CAM-003.

    Медленный (загрузка ORT-сессии + 5 тайлов 2592x1944), поэтому под маркером.
    """
    pytest.importorskip("onnxruntime")
    pytest.importorskip("cv2")

    from cvsvc.config import load_config
    from cvsvc.detect import Detector, resolve_model_path

    try:
        resolve_model_path()
    except FileNotFoundError:
        pytest.skip("модели нет, сначала python tools/export_model.py")

    ref = ROOT / "refs" / "CAM-003.jpg"
    if not ref.exists():
        pytest.skip("нет эталонного кадра refs/CAM-003.jpg")

    cfg = load_config(ROOT / "config" / "cameras.yml", require_rois=False)
    cam = cfg.camera("CAM-003")
    if not cam.tiles:
        from cvsvc.detect.tiles_proposed import apply_to_config

        apply_to_config(cfg)

    dets = Detector().detect(ref, cam)

    assert len(dets) >= 10, "на паркинге ТЦ должно найтись хотя бы 10 машин"
    w, h = cam.frame_size
    for d in dets:
        x1, y1, x2, y2 = d.bbox
        assert 0 <= x1 < x2 <= w and 0 <= y1 < y2 <= h, "бокс вне кадра"
        assert d.score >= 0.2
        assert d.class_id in (2, 3, 5, 7)
        assert d.footprint[1] >= y2, "след на земле не выше низа бокса"

    # Глобальный NMS отработал: сильных дублей после склейки тайлов не осталось.
    dupes = sum(
        1
        for i, a in enumerate(dets)
        for b in dets[i + 1 :]
        if iou(a.bbox, b.bbox) > 0.6
    )
    assert dupes == 0, f"{dupes} пар боксов с IoU > 0.6 — NMS по кадру не сработал"


@pytest.mark.slow
def test_сессия_переиспользуется_между_вызовами():
    pytest.importorskip("onnxruntime")
    from cvsvc.detect import Detector, resolve_model_path

    try:
        resolve_model_path()
    except FileNotFoundError:
        pytest.skip("модели нет")

    det = Detector()
    frame = np.full((400, 600, 3), 120, dtype=np.uint8)
    cam = make_camera(frame_size=(600, 400))
    det.detect(frame, cam)
    first = det.session
    det.detect(frame, cam)
    assert det.session is first, "сессия ORT создаётся заново на каждый кадр"
