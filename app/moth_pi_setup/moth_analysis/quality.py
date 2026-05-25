from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any


def _install_quality_tables(db_path: str | Path) -> None:
    con = sqlite3.connect(str(db_path))
    cur = con.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS import_quality_summary (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL,
        source_file TEXT,
        mode TEXT NOT NULL,
        raw_rows INTEGER NOT NULL DEFAULT 0,
        kept_rows INTEGER NOT NULL DEFAULT 0,
        rejected_rows INTEGER NOT NULL DEFAULT 0,
        flagged_rows INTEGER NOT NULL DEFAULT 0,
        reject_reasons_json TEXT NOT NULL DEFAULT '{}',
        flag_reasons_json TEXT NOT NULL DEFAULT '{}'
    )
    """)

    con.commit()
    con.close()


def get_latest_quality_summary(db_path: str | Path) -> dict[str, Any]:
    _install_quality_tables(db_path)

    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    rows = cur.execute(
        """
        SELECT *
        FROM import_quality_summary
        ORDER BY id DESC
        LIMIT 10
        """
    ).fetchall()

    con.close()

    recent = []

    for row in rows:
        item = dict(row)
        item["reject_reasons"] = json.loads(item.pop("reject_reasons_json") or "{}")
        item["flag_reasons"] = json.loads(item.pop("flag_reasons_json") or "{}")
        recent.append(item)

    return {
        "ok": True,
        "latest": recent[0] if recent else None,
        "recent": recent,
        "message": None if recent else "No import quality summary has been recorded yet.",
    }


def save_quality_summary(db_path: str | Path, summary: dict[str, Any]) -> None:
    _install_quality_tables(db_path)

    con = sqlite3.connect(str(db_path))
    cur = con.cursor()

    cur.execute(
        """
        INSERT INTO import_quality_summary (
            created_at,
            source_file,
            mode,
            raw_rows,
            kept_rows,
            rejected_rows,
            flagged_rows,
            reject_reasons_json,
            flag_reasons_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            summary.get("created_at") or datetime.utcnow().isoformat(timespec="seconds") + "Z",
            summary.get("source_file"),
            summary.get("mode", "standard"),
            int(summary.get("raw_rows", 0)),
            int(summary.get("kept_rows", 0)),
            int(summary.get("rejected_rows", 0)),
            int(summary.get("flagged_rows", 0)),
            json.dumps(summary.get("reject_reasons", {}), sort_keys=True),
            json.dumps(summary.get("flag_reasons", {}), sort_keys=True),
        ),
    )

    con.commit()
    con.close()


def load_and_clean_csv(
    csv_path: str | Path,
    *,
    source_file: str | None = None,
    mode: str = "standard",
    **_kwargs,
):
    """
    Startup-safe placeholder.

    It records row counts without changing import behavior. Full reject/flag logic
    can be restored once the backend is reachable again.
    """
    try:
        import pandas as pd
    except Exception as exc:
        raise RuntimeError(f"pandas is required for CSV quality filtering: {exc}") from exc

    path = Path(csv_path)
    df = pd.read_csv(path, low_memory=False)

    summary = {
        "created_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "source_file": source_file or path.name,
        "mode": mode,
        "raw_rows": int(len(df)),
        "kept_rows": int(len(df)),
        "rejected_rows": 0,
        "flagged_rows": 0,
        "reject_reasons": {},
        "flag_reasons": {},
    }

    return df, df.copy(), summary
