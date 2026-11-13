"""Генератор эталона заглушки `refs/slate.ahash`.

Заглушка — это чёрный кадр с текстом «Трансляция заблокирована для данного сайта»,
который stream.vladlink.ru отдаёт вместо видео, когда Referer не принят.
Отдаётся с HTTP 200 и валидным h264, то есть неотличима от нормального потока
по кодам ответа. Единственный надёжный признак — содержимое кадра.

Скрипт намеренно ходит БЕЗ Referer, чтобы получить заглушку, и сохраняет её aHash.

    python -m cvsvc.ingest.make_slate_ref --config config/cameras.yml
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

from cvsvc.config import load_config
from cvsvc.ingest.hls import (
    HASH_SIDE,
    average_hash,
    decode_frame,
    fetch_playlist,
    mean_luma,
    _fetch_with_retries,
)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Сохранить эталонный хеш заглушки")
    ap.add_argument("--config", default="config/cameras.yml")
    ap.add_argument("--camera", default=None, help="id камеры; по умолчанию первая")
    ap.add_argument("--out", default="refs/slate.ahash")
    ap.add_argument("--save-jpg", default="refs/slate.jpg")
    args = ap.parse_args(argv)

    config = load_config(args.config, require_rois=False)
    cam = config.camera(args.camera) if args.camera else config.cameras[0]

    # Намеренно ломаем Referer — сервер отдаст заглушку.
    blocked = replace(cam, headers={**cam.headers, "Referer": "https://example.invalid/"})

    playlist = fetch_playlist(blocked)
    if not playlist.is_slate:
        print(
            "Плейлист НЕ заглушка даже без корректного Referer. "
            "Либо провайдер снял блокировку по сайту, либо изменилась её схема. "
            "Эталон не сохранён — проверьте вручную.",
            file=sys.stderr,
        )
        return 1

    uri = playlist.segments[-1].uri
    data = _fetch_with_retries(uri, blocked.headers, blocked.id)

    out_jpg = Path(args.save_jpg)
    with tempfile.TemporaryDirectory() as td:
        tmp_jpg = Path(td) / "slate.jpg"
        _, gray = decode_frame(data, tmp_jpg, blocked.id)
        out_jpg.parent.mkdir(parents=True, exist_ok=True)
        out_jpg.write_bytes(tmp_jpg.read_bytes())

    h = average_hash(gray)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(f"{h:0{HASH_SIDE * HASH_SIDE // 4}x}\n", encoding="utf-8")

    print(f"камера:       {cam.id}")
    print(f"сегмент:      {uri}")
    print(f"средняя яркость: {mean_luma(gray):.1f} (заглушка почти чёрная)")
    print(f"эталон aHash:  {out}")
    print(f"кадр:          {out_jpg}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
