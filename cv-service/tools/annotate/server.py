#!/usr/bin/env python3
"""Сервер-помощник редактора разметки ROI.

Браузер не может писать в файловую систему, поэтому статику отдаёт и запись
config/cameras.yml делает этот крошечный сервер. Зависимостей нет — только
stdlib; PyYAML нужен лишь для валидации (он и так в requirements.txt, но без
него сервер работает, просто не проверяет конфиг).

    python tools/annotate/server.py --port 8800

Эндпоинты:
    GET  /                      редактор
    GET  /api/config            текущий config/cameras.yml (text/yaml)
    POST /api/config[?force=1]  валидация + запись с бэкапом
    POST /api/validate          только валидация, ничего не пишет
    GET  /api/frames?camera=ID  эталон + накопленные рекордером кадры
    GET  /api/image?path=...    картинка из refs/ или frames/

Слушает 127.0.0.1: инструмент локальный, наружу его выставлять незачем —
кадры содержат читаемые номера и лица.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import mimetypes
import shutil
import sys
import tempfile
import urllib.parse
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent          # tools/annotate
ROOT = HERE.parent.parent                       # cv-service
CONFIG_PATH = ROOT / "config" / "cameras.yml"
BACKUP_DIR = ROOT / "config" / "backups"
IMAGE_ROOTS = (ROOT / "refs", ROOT / "frames")
MAX_BODY = 8 * 1024 * 1024                      # разметка столько не весит
MAX_FRAMES = 300

# Картинки, которые отдаём редактору. Рекордер пишет jpg.
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


# --------------------------------------------------------------------------- #
# Валидация через настоящий cvsvc.config — тот же код, что у детектора.
# --------------------------------------------------------------------------- #

def validate_yaml_text(text: str) -> tuple[bool, list[str]]:
    """(ok, problems). Если валидатор недоступен — (True, [предупреждение])."""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    try:
        from cvsvc.config import ConfigError, load_config
    except Exception as exc:  # noqa: BLE001 — нет PyYAML/cvsvc, не повод падать
        return True, [f"валидатор недоступен ({exc}); проверьте вручную: "
                      f"python -c \"from cvsvc.config import load_config; "
                      f"load_config('config/cameras.yml')\""]

    # Пишем во временный файл, а не рядом с конфигом: load_config читает
    # только сам YAML, пути reference_frame он не резолвит.
    with tempfile.NamedTemporaryFile("w", suffix=".yml", encoding="utf-8",
                                     delete=False) as fh:
        fh.write(text)
        tmp = Path(fh.name)
    try:
        try:
            load_config(tmp)
        except ConfigError as err:
            return False, list(err.problems)
        except Exception as err:  # noqa: BLE001 — кривой YAML и пр.
            return False, [f"{type(err).__name__}: {err}"]
        return True, []
    finally:
        tmp.unlink(missing_ok=True)


def backup_config() -> Path | None:
    if not CONFIG_PATH.exists():
        return None
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = BACKUP_DIR / f"cameras-{stamp}.yml"
    n = 1
    while dest.exists():
        dest = BACKUP_DIR / f"cameras-{stamp}-{n}.yml"
        n += 1
    shutil.copy2(CONFIG_PATH, dest)
    return dest


def list_frames(camera_id: str) -> list[dict]:
    """Эталон + свежие кадры рекордера, новые сверху."""
    out: list[dict] = []
    for ref in sorted((ROOT / "refs").glob(f"{camera_id}*")):
        if ref.suffix.lower() in IMAGE_SUFFIXES:
            out.append({
                "path": ref.relative_to(ROOT).as_posix(),
                "label": f"эталон · {ref.name}",
                "kind": "ref",
            })

    cam_dir = ROOT / "frames" / camera_id
    if cam_dir.is_dir():
        files: list[Path] = []
        for day in sorted((d for d in cam_dir.iterdir() if d.is_dir()), reverse=True):
            for f in sorted(day.iterdir(), reverse=True):
                if f.suffix.lower() in IMAGE_SUFFIXES:
                    files.append(f)
            if len(files) >= MAX_FRAMES:
                break
        for f in files[:MAX_FRAMES]:
            day = f.parent.name
            t = f.stem
            label = f"{day[:4]}-{day[4:6]}-{day[6:8]} {t[:2]}:{t[2:4]}:{t[4:6]}" \
                if len(day) == 8 and len(t) == 6 and t.isdigit() else f"{day}/{f.name}"
            out.append({
                "path": f.relative_to(ROOT).as_posix(),
                "label": label,
                "kind": "frame",
            })
    return out


def safe_image_path(rel: str) -> Path | None:
    """Пускаем только внутрь refs/ и frames/ — это веб-сервер, пусть и локальный."""
    try:
        candidate = (ROOT / rel).resolve()
    except (OSError, ValueError):
        return None
    for base in IMAGE_ROOTS:
        try:
            candidate.relative_to(base.resolve())
        except ValueError:
            continue
        if candidate.is_file() and candidate.suffix.lower() in IMAGE_SUFFIXES:
            return candidate
    return None


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

class Handler(SimpleHTTPRequestHandler):
    # Статика редактора лежит рядом с этим файлом.
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(HERE), **kwargs)

    # -- helpers -------------------------------------------------------- #

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload: dict) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _query(self) -> dict:
        q = urllib.parse.urlparse(self.path).query
        return {k: v[0] for k, v in urllib.parse.parse_qs(q).items()}

    def _read_body(self) -> str | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None
        if length <= 0 or length > MAX_BODY:
            return None
        return self.rfile.read(length).decode("utf-8")

    def log_message(self, fmt, *args):  # статика молчит, api — логируем
        if "/api/" in (self.requestline or ""):
            super().log_message(fmt, *args)

    # -- GET ------------------------------------------------------------ #

    def do_GET(self) -> None:
        route = urllib.parse.urlparse(self.path).path

        if route == "/api/config":
            if not CONFIG_PATH.exists():
                self._json(HTTPStatus.NOT_FOUND, {"ok": False,
                                                  "message": f"нет файла {CONFIG_PATH}"})
                return
            self._send(HTTPStatus.OK, CONFIG_PATH.read_bytes(), "text/yaml; charset=utf-8")
            return

        if route == "/api/frames":
            cam = self._query().get("camera", "")
            if not cam or "/" in cam or "\\" in cam or cam.startswith("."):
                self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "message": "нужен ?camera=CAM-00X"})
                return
            self._json(HTTPStatus.OK, {"camera": cam, "images": list_frames(cam)})
            return

        if route == "/api/image":
            rel = self._query().get("path", "")
            path = safe_image_path(rel)
            if path is None:
                self._json(HTTPStatus.NOT_FOUND,
                           {"ok": False, "message": f"картинка недоступна: {rel!r}"})
                return
            ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            self._send(HTTPStatus.OK, path.read_bytes(), ctype)
            return

        super().do_GET()

    # -- POST ----------------------------------------------------------- #

    def do_POST(self) -> None:
        route = urllib.parse.urlparse(self.path).path
        if route not in ("/api/config", "/api/validate"):
            self._json(HTTPStatus.NOT_FOUND, {"ok": False, "message": "нет такого эндпоинта"})
            return

        text = self._read_body()
        if text is None:
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "message": "пустое или слишком большое тело"})
            return

        ok, problems = validate_yaml_text(text)

        if route == "/api/validate":
            self._json(HTTPStatus.OK, {
                "ok": ok,
                "message": "Конфиг проходит валидатор." if ok else "Валидатор нашёл проблемы:",
                "problems": problems,
            })
            return

        force = self._query().get("force") in ("1", "true", "yes")
        if not ok and not force:
            self._json(HTTPStatus.UNPROCESSABLE_ENTITY, {
                "ok": False,
                "message": "Не записал: валидатор нашёл проблемы.",
                "problems": problems,
            })
            return

        try:
            backup = backup_config()
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = CONFIG_PATH.parent / (CONFIG_PATH.name + ".tmp")
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(CONFIG_PATH)  # атомарная подмена в пределах одной ФС
        except OSError as exc:
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR,
                       {"ok": False, "message": f"не смог записать: {exc}", "problems": []})
            return

        msg = f"Записано в {CONFIG_PATH.relative_to(ROOT)}"
        if backup:
            msg += f"; бэкап: {backup.relative_to(ROOT)}"
        if not ok:
            msg += " — ВНИМАНИЕ: записано принудительно, конфиг невалиден"
        self._json(HTTPStatus.OK, {"ok": True, "message": msg, "problems": problems})


def main() -> int:
    ap = argparse.ArgumentParser(description="Сервер-помощник редактора разметки ParkVision")
    ap.add_argument("--port", type=int, default=8800)
    ap.add_argument("--host", default="127.0.0.1",
                    help="по умолчанию только localhost: кадры содержат номера и лица")
    args = ap.parse_args()

    if not CONFIG_PATH.exists():
        print(f"! нет {CONFIG_PATH} — редактор откроется пустым", file=sys.stderr)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"ParkVision annotate → http://{args.host}:{args.port}/")
    print(f"  корень проекта: {ROOT}")
    print(f"  конфиг:         {CONFIG_PATH}")
    print("  Ctrl-C для выхода")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nостановлен")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
