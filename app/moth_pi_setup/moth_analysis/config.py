from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(os.getenv("MOTH_PROJECT_ROOT", Path(__file__).resolve().parents[1]))
DATA_DIR = Path(os.getenv("MOTH_DATA_DIR", PROJECT_ROOT / "data"))
DB_PATH = Path(os.getenv("MOTH_DB_PATH", DATA_DIR / "moth.sqlite"))
UPLOAD_DIR = Path(os.getenv("MOTH_UPLOAD_DIR", DATA_DIR / "uploads"))

DEFAULT_H3_RESOLUTIONS = (8, 9, 10)
PARSER_VERSION = "0.1.0"

# Aden Adde / HCMM approximate map centre. Used only for initial UI viewport.
DEFAULT_MAP_CENTER_LON = 45.3040
DEFAULT_MAP_CENTER_LAT = 2.0140
