from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _install_quality_tables(db_path: str | Path) -> None:
    con = sqlite3.connect(str(db_path))
    cur = con.cursor()
    cur.execute(
        """
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
        """
    )
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

    recent: list[dict[str, Any]] = []
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
            summary.get("created_at") or _utc_now(),
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


def _normalise_col(name: str) -> str:
    return "".join(ch.lower() for ch in str(name) if ch.isalnum())


def _find_col(columns: list[str], candidates: list[str]) -> str | None:
    norm = {_normalise_col(c): c for c in columns}
    for cand in candidates:
        key = _normalise_col(cand)
        if key in norm:
            return norm[key]
    # Fallback: partial semantic matching.
    for col in columns:
        n = _normalise_col(col)
        for cand in candidates:
            c = _normalise_col(cand)
            if c and (c in n or n in c):
                return col
    return None


def _counter_add(counter: dict[str, int], key: str, amount: int = 1) -> None:
    counter[key] = int(counter.get(key, 0)) + int(amount)


def _as_numeric(series):
    import pandas as pd
    return pd.to_numeric(series, errors="coerce")


def _as_datetime(series):
    import pandas as pd
    return pd.to_datetime(series, errors="coerce", utc=True)


def load_and_clean_csv(
    csv_path: str | Path,
    *,
    source_file: str | None = None,
    mode: str = "standard",
    **_kwargs,
):
    """Load a MOTH/LAMP CSV and apply a conservative import quality gate.

    Returns (raw_df, clean_df, summary). The function deliberately keeps original
    columns so the existing LANTERN/MOTH importer can parse the cleaned file with
    the same logic it uses for a raw export.

    Modes:
      off/none/disabled: no filtering; summary only.
      standard: reject rows that are clearly unusable or duplicate, flag suspicious rows.
      strict: reject hard failures and suspicious rows.

    Reject reasons include:
      zero_zero_gps, missing_frequency, bad_frequency, missing_dbm, bad_dbm,
      invalid_latlon, duplicate_row, strict_missing_time, strict_missing_gps,
      suspicious_dbm.

    Flag reasons include:
      missing_time, missing_gps, suspicious_dbm, future_or_old_time,
      possible_units_frequency, no_latlon_columns, no_timestamp_column.
    """
    import pandas as pd

    path = Path(csv_path)
    df = pd.read_csv(path, low_memory=False)
    raw = df.copy()
    mode_norm = str(mode or "standard").strip().lower()
    off_mode = mode_norm in {"off", "none", "disabled", "0", "false"}
    strict_mode = mode_norm in {"strict", "enforce", "hard"}

    reject_reasons: dict[str, int] = {}
    flag_reasons: dict[str, int] = {}

    work = df.copy()
    work["quality_status"] = "kept"
    work["quality_flags"] = ""
    work["quality_reject_reasons"] = ""

    cols = list(work.columns)
    lat_col = _find_col(cols, ["lat", "latitude", "gps_lat", "gps latitude", "Latitude"])
    lon_col = _find_col(cols, ["lon", "lng", "longitude", "gps_lon", "gps longitude", "Longitude"])
    freq_col = _find_col(cols, ["frequency_hz", "frequency", "freq_hz", "freq", "frequencyhz", "frequency (Hz)", "Hz"])
    dbm_col = _find_col(cols, ["strength_dbm", "dbm", "rssi", "power_dbm", "level_dbm", "signal", "strength", "dBm"])
    time_col = _find_col(cols, ["timestamp_utc", "timestamp", "time", "datetime", "date_time", "utc", "Detection Time"])

    reject_mask = pd.Series(False, index=work.index)
    flag_map: dict[int, list[str]] = {int(i): [] for i in work.index}
    reject_map: dict[int, list[str]] = {int(i): [] for i in work.index}

    def flag(mask, reason: str) -> None:
        count = int(mask.fillna(False).sum())
        if count:
            _counter_add(flag_reasons, reason, count)
            for idx in work.index[mask.fillna(False)]:
                flag_map[int(idx)].append(reason)

    def reject(mask, reason: str) -> None:
        nonlocal reject_mask
        mask = mask.fillna(False)
        count = int(mask.sum())
        if count:
            _counter_add(reject_reasons, reason, count)
            reject_mask = reject_mask | mask
            for idx in work.index[mask]:
                reject_map[int(idx)].append(reason)

    if off_mode:
        summary = {
            "created_at": _utc_now(),
            "source_file": source_file or path.name,
            "mode": mode_norm,
            "raw_rows": int(len(work)),
            "kept_rows": int(len(work)),
            "rejected_rows": 0,
            "flagged_rows": 0,
            "reject_reasons": {},
            "flag_reasons": {},
        }
        return raw, work, summary

    if freq_col:
        freq = _as_numeric(work[freq_col])
        reject(freq.isna(), "missing_frequency")
        reject(freq <= 0, "bad_frequency")
        # MOTH exports are usually Hz; values under 10,000 are suspicious but not always fatal.
        flag((freq > 0) & (freq < 10000), "possible_units_frequency")
    else:
        # No frequency column means the importer may still parse a non-standard field,
        # so flag rather than reject the whole file.
        flag(pd.Series(True, index=work.index), "missing_frequency_column")

    if dbm_col:
        dbm = _as_numeric(work[dbm_col])
        reject(dbm.isna(), "missing_dbm")
        reject((dbm < -250) | (dbm > 100), "bad_dbm")
        flag((dbm < -160) | (dbm > 20), "suspicious_dbm")
    else:
        flag(pd.Series(True, index=work.index), "missing_dbm_column")

    if lat_col and lon_col:
        lat = _as_numeric(work[lat_col])
        lon = _as_numeric(work[lon_col])
        missing_gps = lat.isna() | lon.isna()
        zero_zero = (lat == 0) & (lon == 0)
        invalid_latlon = (~lat.between(-90, 90)) | (~lon.between(-180, 180))
        flag(missing_gps, "missing_gps")
        reject(zero_zero, "zero_zero_gps")
        reject(invalid_latlon, "invalid_latlon")
        if strict_mode:
            reject(missing_gps, "strict_missing_gps")
    else:
        flag(pd.Series(True, index=work.index), "no_latlon_columns")

    if time_col:
        dt = _as_datetime(work[time_col])
        missing_time = dt.isna()
        flag(missing_time, "missing_time")
        too_old_or_future = dt.notna() & ((dt.dt.year < 2020) | (dt.dt.year > 2035))
        flag(too_old_or_future, "future_or_old_time")
        if strict_mode:
            reject(missing_time | too_old_or_future, "strict_missing_or_bad_time")
    else:
        flag(pd.Series(True, index=work.index), "no_timestamp_column")

    duplicate_mask = work.drop(columns=["quality_status", "quality_flags", "quality_reject_reasons"], errors="ignore").duplicated(keep="first")
    reject(duplicate_mask, "duplicate_row")

    if strict_mode and "suspicious_dbm" in flag_reasons and dbm_col:
        dbm = _as_numeric(work[dbm_col])
        reject((dbm < -160) | (dbm > 20), "suspicious_dbm")

    for idx in work.index:
        flags = sorted(set(flag_map[int(idx)]))
        rejects = sorted(set(reject_map[int(idx)]))
        if rejects:
            work.at[idx, "quality_status"] = "rejected"
        elif flags:
            work.at[idx, "quality_status"] = "flagged"
        else:
            work.at[idx, "quality_status"] = "kept"
        work.at[idx, "quality_flags"] = ";".join(flags)
        work.at[idx, "quality_reject_reasons"] = ";".join(rejects)

    clean = work.loc[~reject_mask].copy()
    flagged_rows = int((clean["quality_status"] == "flagged").sum()) if "quality_status" in clean.columns else 0

    summary = {
        "created_at": _utc_now(),
        "source_file": source_file or path.name,
        "mode": mode_norm,
        "raw_rows": int(len(work)),
        "kept_rows": int(len(clean)),
        "rejected_rows": int(reject_mask.sum()),
        "flagged_rows": flagged_rows,
        "reject_reasons": reject_reasons,
        "flag_reasons": flag_reasons,
    }
    return raw, clean, summary
