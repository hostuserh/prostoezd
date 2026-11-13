"""Общие фикстуры тестов CV-сервиса.

Добавляет корень ``cv-service/`` в ``sys.path``, чтобы ``import cvsvc...``
работал без установки пакета (на хакатоне никто не делает ``pip install -e``).
"""

from __future__ import annotations

import sys
from pathlib import Path

CV_SERVICE_ROOT = Path(__file__).resolve().parent.parent
if str(CV_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(CV_SERVICE_ROOT))
