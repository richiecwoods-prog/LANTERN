from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# -----------------------------------------------------------------------------
# LANTERN import-quality module
# -----------------------------------------------------------------------------
# This module is intentionally self-contained.  api.py calls:
#
#     raw_df, cleaned_df, summary = load_and_clean_csv(...)
#     save_quality_summary(DB_PATH, summary)
#
# In shadow mode the API still imports the original CSV, but records the summary.
# In clean mode the API imports a cleaned copy made from cleaned_df's original
# columns.  Therefore cleaned_df must preserve all original CSV columns, while also
# adding canonical fields used for future reporting.
# -----------------------------------------------------------------------------


LAT_NAMES = {
    "lat",
    "latitude",
    "latitude_deg",
    "gps_lat",
    "gps_latitude",
    "gpslatitude",
    "location_lat",
    "position_lat",
    "y",
}

LON_NAMES = {
    "lon",
    "lng",
    "long",
    "longitude",
    "longitude_deg",
    "gps_lon",
    "gps_lng",
    "gps_longitude",
    "gpslongitude",
    "location_lon",
    "location_lng",
    "position_lon",
    "position_lng",
    "x",
}

FREQ_NAMES = {
    "frequency",
    "freq",
    "frequency_hz",
    "freq_hz",
    "hz",
    "frequencyhz",
    "frequency_mhz",
    "freq_mhz",
    "mhz",
    "frequencymhz",
}

DBM_NAMES = {
    "dbm",
    "rssi",
    "power",
    "power_dbm",
    "strength",
    "strength_dbm",
    "signal",
    "signal_strength",
    "signal_dbm",
    "level",
    "level_dbm",
}

TIME_NAMES = {
    "timestamp",
    "timestamp_utc",
    "time",
    "time_utc",
    "datetime",
    "date_time",
    "utc",
    "created_at",
    "ts",
}


REJECT_REASON_LABELS = {
    "missing_latlon": "Missing GPS latitude/longitude",
    "invalid_latlon": "Invalid GPS latitude/longitude",
    "zero_zero_gps": "Zero/zero GPS location",
    "missing_frequency": "Missing frequency",
    "bad_frequency": "Bad frequency",
    "missing_dbm": "Missing dBm/strength",
    "duplicate_row": "Duplicate detection row",
}

FLAG_REASON_LABELS = {
    "suspicious_dbm": "Suspicious dBm value",
    "stale_gps_candidate": "Possible stale GPS cluster",
}


# -----------------------------------------------------------------------------
# Database helpers
# -----------------------------------------------------------------------------


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

    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_import_quality_summary_created "
        "ON import_quality_summary (created_at)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_import_quality_summary_file "
        "ON import_quality_summary (source_file)"
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
        item["reject_reason_labels"] = REJECT_REASON_LABELS
        item["flag_reason_labels"] = FLAG_REASON_LABELS
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


# -----------------------------------------------------------------------------
# CSV cleaning helpers
# -----------------------------------------------------------------------------


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(name).strip().lower()).strip("_")


def _pick_col(columns: list[str], accepted: set[str]) -> str | None:
    lookup = {_norm(col): col for col in columns}

    # Exact normalized match first.
    for normalized, original in lookup.items():
        if normalized in accepted:
            return original

    # Then a cautious contains match for common noisy exported names such as
    # "Signal Strength (dBm)" or "Frequency Hz".
    for normalized, original in lookup.items():
        for item in accepted:
            if item and item in normalized:
                return original

    return None


def _empty_series(pd: Any, index: Any, value: Any = None):
    return pd.Series([value] * len(index), index=index)


def _to_numeric(pd: Any, df: Any, column: str | None):
    if column is None:
        return _empty_series(pd, df.index)

    return pd.to_numeric(df[column], errors="coerce")


def _append_reason(series: Any, mask: Any, reason: str) -> None:
    try:
        if not bool(mask.any()):
            return
    except Exception:
        return

    current = series.loc[mask].fillna("").astype(str)
    series.loc[mask] = current.map(lambda value: reason if value == "" else f"{value};{reason}")


def _counter_from_reasons(values: Any) -> dict[str, int]:
    counter: Counter[str] = Counter()

    for value in values.dropna().astype(str):
        if not value:
            continue
        for reason in value.split(";"):
            reason = reason.strip()
            if reason:
                counter[reason] += 1

    return dict(counter)


def _looks_like_mhz(column_name: str | None, values: Any) -> bool:
    normalized = _norm(column_name or "")

    if "mhz" in normalized:
        return True

    if "hz" in normalized and "mhz" not in normalized:
        return False

    try:
        sample = values.dropna()
        if sample.empty:
            return False
        median = float(sample.abs().median())
    except Exception:
        return False

    # GNSS/RF exports commonly use values such as 1176.45, 1227.60, 1575.42
    # when in MHz.  Treat sub-1,000,000 positive frequencies as MHz.
    return 0 < median < 1_000_000


def _frequency_to_hz(pd: Any, values: Any, column_name: str | None):
    freq = values.copy()
    if _looks_like_mhz(column_name, freq):
        freq = freq * 1_000_000.0
    return freq


def _optional_h3_cell(lat: float, lon: float, resolution: int) -> str | None:
    try:
        import h3  # type: ignore
    except Exception:
        return None

    try:
        if hasattr(h3, "latlng_to_cell"):
            return h3.latlng_to_cell(float(lat), float(lon), int(resolution))
        if hasattr(h3, "geo_to_h3"):
            return h3.geo_to_h3(float(lat), float(lon), int(resolution))
    except Exception:
        return None

    return None


def _add_time_bins(pd: Any, cleaned: Any) -> None:
    if "timestamp_utc" not in cleaned.columns:
        cleaned["time_bin_30m"] = ""
        cleaned["time_bin_60m"] = ""
        return

    try:
        dt = pd.to_datetime(cleaned["timestamp_utc"], utc=True, errors="coerce")
        cleaned["time_bin_30m"] = dt.dt.floor("30min").dt.strftime("%Y-%m-%dT%H:%M:%SZ").fillna("")
        cleaned["time_bin_60m"] = dt.dt.floor("60min").dt.strftime("%Y-%m-%dT%H:%M:%SZ").fillna("")
    except Exception:
        cleaned["time_bin_30m"] = ""
        cleaned["time_bin_60m"] = ""


def _detect_stale_gps(pd: Any, raw: Any, lat: Any, lon: Any, time_col: str | None) -> Any:
    """Flag likely stale GPS clusters without rejecting them.

    Conservative heuristic: same rounded lat/lon repeated >= 250 times.  This is
    only a flag because a static survey can legitimately contain many repeated
    positions.
    """
    try:
        grouped = pd.DataFrame({"lat": lat.round(6), "lon": lon.round(6)})
        counts = grouped.groupby(["lat", "lon"])["lat"].transform("count")
        return counts >= 250
    except Exception:
        return pd.Series([False] * len(raw), index=raw.index)


# -----------------------------------------------------------------------------
# Main public cleaner
# -----------------------------------------------------------------------------


def load_and_clean_csv(
    csv_path: str | Path,
    *,
    source_file: str | None = None,
    mode: str = "standard",
    h3_resolution: int = 9,
    **_kwargs: Any,
):
    """Load a MOTH/LAMP CSV, classify suspect rows, and return clean data.

    Returns:
        raw_df, cleaned_df, summary

    raw_df:
        Original CSV dataframe with no rows removed.

    cleaned_df:
        Original CSV columns preserved, plus canonical analysis columns.  Rows
        with hard-reject reasons are removed.  Flagged rows are kept.

    summary:
        Row counts and reason counters.  Saved by api.py into
        import_quality_summary.

    Reject rules:
        missing/invalid lat-lon, zero-zero GPS, missing/bad frequency,
        missing dBm, exact duplicate detection rows.

    Flag rules:
        suspicious dBm, possible stale GPS cluster.
    """

    try:
        import pandas as pd
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(f"pandas is required for CSV quality filtering: {exc}") from exc

    path = Path(csv_path)
    source_file = source_file or path.name
    mode = (mode or "standard").strip().lower()

    raw = pd.read_csv(path, low_memory=False)
    columns = list(raw.columns)
    raw_rows = len(raw)

    lat_col = _pick_col(columns, LAT_NAMES)
    lon_col = _pick_col(columns, LON_NAMES)
    freq_col = _pick_col(columns, FREQ_NAMES)
    dbm_col = _pick_col(columns, DBM_NAMES)
    time_col = _pick_col(columns, TIME_NAMES)

    lat = _to_numeric(pd, raw, lat_col)
    lon = _to_numeric(pd, raw, lon_col)
    freq_raw = _to_numeric(pd, raw, freq_col)
    freq_hz = _frequency_to_hz(pd, freq_raw, freq_col)
    dbm = _to_numeric(pd, raw, dbm_col)

    reject_reasons = pd.Series([""] * raw_rows, index=raw.index, dtype="object")
    flag_reasons = pd.Series([""] * raw_rows, index=raw.index, dtype="object")

    missing_latlon = lat.isna() | lon.isna()
    invalid_latlon = (~missing_latlon) & (
        (lat < -90) | (lat > 90) | (lon < -180) | (lon > 180)
    )
    zero_zero = (~missing_latlon) & (lat.abs() < 1e-12) & (lon.abs() < 1e-12)

    missing_frequency = freq_hz.isna()
    bad_frequency = (~missing_frequency) & ((freq_hz <= 0) | (freq_hz > 20_000_000_000))

    missing_dbm = dbm.isna()
    suspicious_dbm = (~missing_dbm) & ((dbm > 10) | (dbm < -180))

    _append_reason(reject_reasons, missing_latlon, "missing_latlon")
    _append_reason(reject_reasons, invalid_latlon, "invalid_latlon")
    _append_reason(reject_reasons, zero_zero, "zero_zero_gps")
    _append_reason(reject_reasons, missing_frequency, "missing_frequency")
    _append_reason(reject_reasons, bad_frequency, "bad_frequency")
    _append_reason(reject_reasons, missing_dbm, "missing_dbm")

    _append_reason(flag_reasons, suspicious_dbm, "suspicious_dbm")

    stale_gps = _detect_stale_gps(pd, raw, lat, lon, time_col)
    _append_reason(flag_reasons, stale_gps, "stale_gps_candidate")

    # Exact duplicate detector.  A duplicate is only counted after the first
    # occurrence.  This protects map density, candidate scoring, and report counts
    # from repeated imports or repeated identical rows inside one CSV.
    try:
        timestamp_values = raw[time_col].astype(str) if time_col else pd.Series([""] * raw_rows, index=raw.index)
        dedupe = pd.DataFrame(
            {
                "timestamp": timestamp_values,
                "lat": lat.round(7),
                "lon": lon.round(7),
                "frequency_hz": freq_hz.round(1),
                "dbm": dbm.round(1),
            }
        )
        duplicate_mask = dedupe.duplicated(keep="first")
        # If all key fields are missing, let the missing-field reasons explain the
        # row instead of over-counting duplicate_row.
        has_any_key = dedupe.notna().any(axis=1)
        _append_reason(reject_reasons, duplicate_mask & has_any_key, "duplicate_row")
    except Exception:
        pass

    reject_mask = reject_reasons.astype(str) != ""
    keep_mask = ~reject_mask
    kept_rows = int(keep_mask.sum())

    cleaned = raw.loc[keep_mask].copy()

    # Canonical fields for future report/cache work. These do not remove the
    # original columns, which keeps clean-mode imports compatible with the old
    # insert_collection_from_csv parser.
    if kept_rows:
        cleaned["lat"] = lat.loc[keep_mask].astype(float)
        cleaned["lon"] = lon.loc[keep_mask].astype(float)
        cleaned["frequency_hz"] = freq_hz.loc[keep_mask].astype(float)
        cleaned["frequency_mhz"] = cleaned["frequency_hz"] / 1_000_000.0
        cleaned["dbm"] = dbm.loc[keep_mask].astype(float)
    else:
        cleaned["lat"] = []
        cleaned["lon"] = []
        cleaned["frequency_hz"] = []
        cleaned["frequency_mhz"] = []
        cleaned["dbm"] = []

    cleaned["source_file"] = source_file

    if time_col:
        cleaned["timestamp_utc"] = raw.loc[keep_mask, time_col].astype(str)
    else:
        cleaned["timestamp_utc"] = ""

    cleaned_flag_reasons = flag_reasons.loc[keep_mask].fillna("").astype(str)
    cleaned["quality_status"] = "kept"
    cleaned.loc[cleaned_flag_reasons != "", "quality_status"] = "flagged"
    cleaned["quality_reasons"] = cleaned_flag_reasons

    # Duplicate hash is useful for audits even if the DB does not store it yet.
    try:
        cleaned["duplicate_hash"] = (
            cleaned["timestamp_utc"].astype(str)
            + "|" + cleaned["lat"].round(7).astype(str)
            + "|" + cleaned["lon"].round(7).astype(str)
            + "|" + cleaned["frequency_hz"].round(1).astype(str)
            + "|" + cleaned["dbm"].round(1).astype(str)
        )
    except Exception:
        cleaned["duplicate_hash"] = ""

    # H3 cells are optional; failure must not break import.
    if kept_rows and kept_rows <= 500_000:
        cleaned["h3_cell"] = [
            _optional_h3_cell(float(row.lat), float(row.lon), h3_resolution)
            for row in cleaned[["lat", "lon"]].itertuples(index=False)
        ]
    else:
        cleaned["h3_cell"] = None

    _add_time_bins(pd, cleaned)

    flag_mask_kept = keep_mask & (flag_reasons.astype(str) != "")

    summary = {
        "ok": True,
        "created_at": _utc_now(),
        "source_file": source_file,
        "mode": mode,
        "raw_rows": int(raw_rows),
        "kept_rows": int(kept_rows),
        "rejected_rows": int(reject_mask.sum()),
        "flagged_rows": int(flag_mask_kept.sum()),
        "reject_reasons": _counter_from_reasons(reject_reasons),
        "flag_reasons": _counter_from_reasons(flag_reasons.loc[keep_mask]),
        "reject_reason_labels": REJECT_REASON_LABELS,
        "flag_reason_labels": FLAG_REASON_LABELS,
        "columns": {
            "lat": lat_col,
            "lon": lon_col,
            "frequency": freq_col,
            "dbm": dbm_col,
            "timestamp": time_col,
        },
        "notes": [
            "Rejected rows are excluded from cleaned_df.",
            "Flagged rows remain in cleaned_df but are marked with quality_status='flagged'.",
            "In API shadow mode, the original CSV is still imported; in clean mode, api.py imports a cleaned copy.",
        ],
    }

    return raw, cleaned, summary
