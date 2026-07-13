from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable, Mapping, Any

from .config import DB_PATH, DATA_DIR


def connect(db_path: Path | str = DB_PATH) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: Path | str = DB_PATH) -> None:
    schema_path = Path(__file__).with_name("schema.sql")
    conn = connect(db_path)
    try:
        conn.executescript(schema_path.read_text())
        conn.commit()
    finally:
        conn.close()


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]
