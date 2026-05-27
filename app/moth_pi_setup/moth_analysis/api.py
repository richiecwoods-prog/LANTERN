from __future__ import annotations

import csv
import math
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Query
from fastapi.responses import HTMLResponse, ORJSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import DB_PATH, UPLOAD_DIR
from .db import connect, init_db, rows_to_dicts
from .geo import clamp, haversine_m
from .h3tools import cell_to_boundary_lnglat, latlon_to_cell
from .ingest import insert_candidate_site, insert_collection_from_csv
from .scoring import percentile, score_candidate_sites
from .quality import (
    get_latest_quality_summary,
    load_and_clean_csv,
    save_quality_summary,
)

app = FastAPI(title="MOTH Data Analysis", version="0.6.0", default_response_class=ORJSONResponse)

STATIC_DIR = Path(__file__).with_name("static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class CandidateIn(BaseModel):
    name: str = Field(min_length=1)
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    antenna_height_agl_m: float | None = None
    practical_score: float = Field(default=0.5, ge=0, le=1)
    site_notes: str | None = None


@app.on_event("startup")
def startup() -> None:
    init_db()
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

@app.get("/api/quality/summary")
def api_quality_summary():
    return get_latest_quality_summary(DB_PATH)


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "db_path": str(DB_PATH), "version": "0.6.0"}


def parse_collection_ids(collection_id: int | None = None, collection_ids: str | None = None) -> list[int]:
    ids: set[int] = set()
    if collection_id is not None:
        ids.add(int(collection_id))
    if collection_ids:
        for part in collection_ids.replace(";", ",").split(","):
            part = part.strip()
            if not part:
                continue
            try:
                ids.add(int(part))
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=f"Bad collection id: {part}") from exc
    return sorted(ids)


def add_collection_filter(where: list[str], params: list[Any], ids: list[int]) -> None:
    if ids:
        placeholders = ",".join("?" for _ in ids)
        where.append(f"collection_id IN ({placeholders})")
        params.extend(ids)


def add_frequency_filter(where: list[str], params: list[Any], target_min_hz: float | None, target_max_hz: float | None) -> None:
    if target_min_hz is not None:
        where.append("frequency_hz >= ?")
        params.append(target_min_hz)
    if target_max_hz is not None:
        where.append("frequency_hz <= ?")
        params.append(target_max_hz)


def add_time_filter(where: list[str], params: list[Any], start_utc: str | None, end_utc: str | None) -> None:
    if start_utc:
        where.append("timestamp_utc >= ?")
        params.append(start_utc)
    if end_utc:
        where.append("timestamp_utc <= ?")
        params.append(end_utc)


def add_event_filters(
    where: list[str],
    params: list[Any],
    *,
    collection_id: int | None = None,
    collection_ids: str | None = None,
    min_dbm: float | None = None,
    target_min_hz: float | None = None,
    target_max_hz: float | None = None,
    start_utc: str | None = None,
    end_utc: str | None = None,
) -> list[int]:
    ids = parse_collection_ids(collection_id=collection_id, collection_ids=collection_ids)
    add_collection_filter(where, params, ids)
    if min_dbm is not None:
        where.append("strength_dbm >= ?")
        params.append(min_dbm)
    add_frequency_filter(where, params, target_min_hz, target_max_hz)
    add_time_filter(where, params, start_utc, end_utc)
    return ids


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def iso_no_ms(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    return 2.0 * r * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))


def save_upload_file(file: UploadFile) -> Path:
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Upload CSV files exported by LAMP/MOTH")
    safe_name = Path(file.filename).name
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    dest = UPLOAD_DIR / safe_name
    if dest.exists():
        stem = dest.stem
        suffix = dest.suffix
        i = 1
        while True:
            candidate = UPLOAD_DIR / f"{stem}_{i}{suffix}"
            if not candidate.exists():
                dest = candidate
                break
            i += 1
    with dest.open("wb") as out:
        shutil.copyfileobj(file.file, out)
    return dest


def do_import(
    file: UploadFile,
    *,
    collection_name: str | None = None,
    device_serial: str | None = None,
    firmware_version: str | None = None,
    hardware_version: str | None = None,
    source_type: str = "lamp_csv",
    scan_mode: str | None = None,
    detection_threshold_db: float | None = None,
    white_list_enabled: bool = False,
    antenna_height_agl_m: float | None = None,
    antenna_notes: str | None = None,
    operator_notes: str | None = None,
) -> dict[str, Any]:
    dest = save_upload_file(file)

    # LANTERN import quality gate.
    #
    # Default behaviour is shadow mode:
    #   - save the original upload
    #   - calculate and store quality/filter summary
    #   - import the original CSV unchanged
    #
    # Set LANTERN_IMPORT_QUALITY_MODE=clean to import a cleaned copy after
    # the quality summary has been proven against real field data.
    quality_mode = os.getenv("LANTERN_IMPORT_QUALITY_MODE", "shadow").strip().lower()
    import_path = dest
    quality_summary: dict[str, Any] | None = None

    try:
        raw_df, cleaned_df, quality_summary = load_and_clean_csv(
            dest,
            source_file=Path(file.filename or dest.name).name,
            mode=quality_mode or "shadow",
        )
        save_quality_summary(DB_PATH, quality_summary)

        if quality_mode in {"clean", "active", "standard"}:
            original_columns = [column for column in raw_df.columns if column in cleaned_df.columns]

            if not original_columns:
                raise ValueError("Quality gate could not identify original CSV columns for cleaned import.")

            cleaned_for_import = cleaned_df[original_columns].copy()
            clean_dest = dest.with_name(f"{dest.stem}__quality_cleaned{dest.suffix}")
            counter = 1

            while clean_dest.exists():
                clean_dest = dest.with_name(f"{dest.stem}__quality_cleaned_{counter}{dest.suffix}")
                counter += 1

            cleaned_for_import.to_csv(clean_dest, index=False)
            import_path = clean_dest

    except Exception as exc:
        # Never break CSV import just because the quality summary failed.
        # The backend terminal will show the warning while normal import continues.
        print(f"[LANTERN quality gate] warning: {exc}")
        quality_summary = {
            "ok": False,
            "source_file": Path(file.filename or dest.name).name,
            "mode": quality_mode or "shadow",
            "error": str(exc),
        }

    try:
        result = insert_collection_from_csv(
            import_path,
            collection_name=collection_name,
            device_serial=device_serial,
            firmware_version=firmware_version,
            hardware_version=hardware_version,
            source_type=source_type,
            scan_mode=scan_mode,
            detection_threshold_db=detection_threshold_db,
            white_list_enabled=white_list_enabled,
            antenna_height_agl_m=antenna_height_agl_m,
            antenna_notes=antenna_notes,
            operator_notes=operator_notes,
        )

        if isinstance(result, dict):
            result["quality_mode"] = quality_mode or "shadow"
            result["quality_summary"] = quality_summary

            if import_path != dest:
                result["original_upload_path"] = str(dest)
                result["quality_cleaned_import_path"] = str(import_path)

        return result

    except Exception as exc:
        if "UNIQUE constraint failed" in str(exc):
            raise HTTPException(status_code=409, detail="This file hash already exists in the database") from exc
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/import")
def import_moth_csv(
    file: Annotated[UploadFile, File()],
    collection_name: Annotated[str | None, Form()] = None,
    device_serial: Annotated[str | None, Form()] = None,
    firmware_version: Annotated[str | None, Form()] = None,
    hardware_version: Annotated[str | None, Form()] = None,
    source_type: Annotated[str, Form()] = "lamp_csv",
    scan_mode: Annotated[str | None, Form()] = None,
    detection_threshold_db: Annotated[float | None, Form()] = None,
    white_list_enabled: Annotated[bool, Form()] = False,
    antenna_height_agl_m: Annotated[float | None, Form()] = None,
    antenna_notes: Annotated[str | None, Form()] = None,
    operator_notes: Annotated[str | None, Form()] = None,
) -> dict[str, Any]:
    return do_import(
        file,
        collection_name=collection_name,
        device_serial=device_serial,
        firmware_version=firmware_version,
        hardware_version=hardware_version,
        source_type=source_type,
        scan_mode=scan_mode,
        detection_threshold_db=detection_threshold_db,
        white_list_enabled=white_list_enabled,
        antenna_height_agl_m=antenna_height_agl_m,
        antenna_notes=antenna_notes,
        operator_notes=operator_notes,
    )


@app.post("/api/import-batch")
def import_moth_csv_batch(
    files: Annotated[list[UploadFile], File()],
    collection_name_prefix: Annotated[str | None, Form()] = None,
    device_serial: Annotated[str | None, Form()] = None,
    firmware_version: Annotated[str | None, Form()] = None,
    hardware_version: Annotated[str | None, Form()] = None,
    source_type: Annotated[str, Form()] = "lamp_csv",
    scan_mode: Annotated[str | None, Form()] = None,
    detection_threshold_db: Annotated[float | None, Form()] = None,
    white_list_enabled: Annotated[bool, Form()] = False,
    antenna_height_agl_m: Annotated[float | None, Form()] = None,
    antenna_notes: Annotated[str | None, Form()] = None,
    operator_notes: Annotated[str | None, Form()] = None,
) -> dict[str, Any]:
    if not files:
        raise HTTPException(status_code=400, detail="No CSV files supplied")
    imported: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for file in files:
        name = Path(file.filename or "moth_scan.csv").stem
        collection_name = f"{collection_name_prefix} - {name}" if collection_name_prefix else name
        try:
            imported.append(do_import(
                file,
                collection_name=collection_name,
                device_serial=device_serial,
                firmware_version=firmware_version,
                hardware_version=hardware_version,
                source_type=source_type,
                scan_mode=scan_mode,
                detection_threshold_db=detection_threshold_db,
                white_list_enabled=white_list_enabled,
                antenna_height_agl_m=antenna_height_agl_m,
                antenna_notes=antenna_notes,
                operator_notes=operator_notes,
            ))
        except HTTPException as exc:
            if exc.status_code == 409:
                skipped.append({"file_name": file.filename, "reason": exc.detail})
            else:
                raise
    return {"imported_count": len(imported), "skipped_count": len(skipped), "imported": imported, "skipped": skipped}


@app.get("/api/collections")
def collections() -> list[dict[str, Any]]:
    conn = connect()
    rows = rows_to_dicts(conn.execute(
        """
        SELECT c.*,
               COUNT(e.event_id) AS total_events_in_db,
               SUM(CASE WHEN e.valid = 1 THEN 1 ELSE 0 END) AS valid_events_in_db
        FROM moth_collections c
        LEFT JOIN moth_events e ON e.collection_id = c.collection_id
        GROUP BY c.collection_id
        ORDER BY c.upload_time_utc DESC, c.collection_id DESC
        """
    ).fetchall())
    conn.close()
    return rows


@app.delete("/api/collections/{collection_id}")
def delete_collection(collection_id: int) -> dict[str, Any]:
    conn = connect()
    with conn:
        cur = conn.execute("DELETE FROM moth_collections WHERE collection_id = ?", (collection_id,))
    conn.close()
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="Collection not found")
    return {"deleted_collection_id": collection_id}


@app.get("/api/summary")
def summary(
    collection_id: int | None = None,
    collection_ids: str | None = None,
    min_dbm: float | None = None,
    target_min_hz: float | None = None,
    target_max_hz: float | None = None,
    start_utc: str | None = None,
    end_utc: str | None = None,
) -> dict[str, Any]:
    where = ["1 = 1"]
    params: list[Any] = []
    ids = add_event_filters(
        where,
        params,
        collection_id=collection_id,
        collection_ids=collection_ids,
        min_dbm=min_dbm,
        target_min_hz=target_min_hz,
        target_max_hz=target_max_hz,
        start_utc=start_utc,
        end_utc=end_utc,
    )
    where_sql = " AND ".join(where)
    conn = connect()
    collections_rows = rows_to_dicts(conn.execute("SELECT * FROM moth_collections ORDER BY upload_time_utc DESC, collection_id DESC").fetchall())
    totals = dict(conn.execute(
        f"""
        SELECT COUNT(*) AS total_events,
               SUM(CASE WHEN valid = 1 THEN 1 ELSE 0 END) AS valid_events,
               MIN(timestamp_utc) AS first_timestamp_utc,
               MAX(timestamp_utc) AS last_timestamp_utc,
               MIN(lat) AS min_lat,
               MAX(lat) AS max_lat,
               MIN(lon) AS min_lon,
               MAX(lon) AS max_lon,
               MIN(strength_dbm) AS min_dbm,
               MAX(strength_dbm) AS max_dbm
        FROM moth_events
        WHERE {where_sql}
        """,
        params,
    ).fetchone())
    freq = rows_to_dicts(conn.execute(
        f"""
        SELECT CASE
                 WHEN frequency_hz IS NULL THEN 'unknown'
                 WHEN frequency_hz < 30000000 THEN '<30 MHz'
                 WHEN frequency_hz < 300000000 THEN '30-300 MHz'
                 WHEN frequency_hz < 1000000000 THEN '300-1000 MHz'
                 WHEN frequency_hz < 3000000000 THEN '1-3 GHz'
                 ELSE '>3 GHz'
               END AS band,
               COUNT(*) AS count,
               ROUND(AVG(strength_dbm), 1) AS avg_dbm
        FROM moth_events
        WHERE valid = 1 AND {where_sql}
        GROUP BY band
        ORDER BY count DESC
        """,
        params,
    ).fetchall())
    conn.close()
    return {"totals": totals, "collections": collections_rows, "band_summary": freq, "selected_collection_ids": ids}


@app.get("/api/frequency-summary")
def frequency_summary(
    collection_id: int | None = None,
    collection_ids: str | None = None,
    min_dbm: float | None = None,
    start_utc: str | None = None,
    end_utc: str | None = None,
    limit: int = Query(default=25, ge=1, le=100),
) -> list[dict[str, Any]]:
    where = ["valid = 1", "frequency_hz IS NOT NULL"]
    params: list[Any] = []
    ids = parse_collection_ids(collection_id=collection_id, collection_ids=collection_ids)
    add_collection_filter(where, params, ids)
    if min_dbm is not None:
        where.append("strength_dbm >= ?")
        params.append(min_dbm)
    add_time_filter(where, params, start_utc, end_utc)
    conn = connect()
    rows = rows_to_dicts(conn.execute(
        f"""
        SELECT ROUND(frequency_hz / 1000000.0, 3) AS frequency_mhz,
               COUNT(*) AS event_count,
               ROUND(AVG(strength_dbm), 1) AS avg_dbm,
               MIN(strength_dbm) AS min_dbm,
               MAX(strength_dbm) AS max_dbm
        FROM moth_events
        WHERE {" AND ".join(where)}
        GROUP BY ROUND(frequency_hz / 1000000.0, 3)
        ORDER BY event_count DESC
        LIMIT ?
        """,
        params + [limit],
    ).fetchall())
    conn.close()
    return rows


@app.get("/api/events")
def events(
    collection_id: int | None = None,
    collection_ids: str | None = None,
    min_dbm: float | None = None,
    target_min_hz: float | None = None,
    target_max_hz: float | None = None,
    start_utc: str | None = None,
    end_utc: str | None = None,
    limit: int = Query(default=5000, ge=1, le=50000),
) -> list[dict[str, Any]]:
    where = ["valid = 1", "lat IS NOT NULL", "lon IS NOT NULL"]
    params: list[Any] = []
    add_event_filters(
        where,
        params,
        collection_id=collection_id,
        collection_ids=collection_ids,
        min_dbm=min_dbm,
        target_min_hz=target_min_hz,
        target_max_hz=target_max_hz,
        start_utc=start_utc,
        end_utc=end_utc,
    )
    sql = "SELECT * FROM moth_events WHERE " + " AND ".join(where) + " ORDER BY event_id DESC LIMIT ?"
    params.append(limit)
    conn = connect()
    rows = rows_to_dicts(conn.execute(sql, params).fetchall())
    conn.close()
    return rows


def point_feature(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [row["lon"], row["lat"]]},
        "properties": {k: v for k, v in row.items() if k not in ("lon", "lat", "raw_row_json")},
    }


@app.get("/api/events.geojson")
def events_geojson(
    collection_id: int | None = None,
    collection_ids: str | None = None,
    min_dbm: float | None = None,
    target_min_hz: float | None = None,
    target_max_hz: float | None = None,
    start_utc: str | None = None,
    end_utc: str | None = None,
    limit: int = Query(default=10000, ge=1, le=50000),
) -> dict[str, Any]:
    return {
        "type": "FeatureCollection",
        "features": [
            point_feature(row)
            for row in events(collection_id, collection_ids, min_dbm, target_min_hz, target_max_hz, start_utc, end_utc, limit)
            if row.get("lat") is not None and row.get("lon") is not None
        ],
    }




def event_is_target_frequency(frequency_hz: Any, target_min_hz: float | None, target_max_hz: float | None) -> bool:
    if target_min_hz is None and target_max_hz is None:
        return True
    if frequency_hz is None:
        return False
    try:
        f = float(frequency_hz)
    except Exception:
        return False
    return (target_min_hz is None or f >= target_min_hz) and (target_max_hz is None or f <= target_max_hz)


def frequency_window_label(target_min_hz: float | None, target_max_hz: float | None) -> str:
    if target_min_hz is None and target_max_hz is None:
        return "Frequency: all logged MOTH detections"
    min_mhz = target_min_hz / 1_000_000.0 if target_min_hz is not None else None
    max_mhz = target_max_hz / 1_000_000.0 if target_max_hz is not None else None
    centre = None
    if min_mhz is not None and max_mhz is not None:
        centre = (min_mhz + max_mhz) / 2.0
    guide = ""
    if centre is not None:
        presets = [
            (1575.42, "MOTH guide L1 / GNSS L1"),
            (1227.60, "MOTH guide L2 / GNSS L2"),
            (1381.05, "MOTH guide L3"),
            (1176.45, "MOTH guide L5 / GNSS L5"),
        ]
        for ref, name in presets:
            if abs(centre - ref) <= 25.0:
                guide = f" ({name})"
                break
    if min_mhz is not None and max_mhz is not None:
        return f"Frequency: {min_mhz:.2f}-{max_mhz:.2f} MHz{guide}"
    if min_mhz is not None:
        return f"Frequency: above {min_mhz:.2f} MHz"
    return f"Frequency: below {max_mhz:.2f} MHz"


def human_filter_lines(
    *,
    radius_m: float | None = None,
    target_min_hz: float | None = None,
    target_max_hz: float | None = None,
    collection_ids: list[int] | None = None,
    start_utc: str | None = None,
    end_utc: str | None = None,
    min_dbm: float | None = None,
) -> list[str]:
    lines = [frequency_window_label(target_min_hz, target_max_hz)]
    if collection_ids:
        lines.append("Scans: " + ", ".join(str(x) for x in collection_ids))
    else:
        lines.append("Scans: all selected/loaded scans")
    if start_utc or end_utc:
        lines.append(f"Time: {start_utc or 'start'} to {end_utc or 'end'}")
    else:
        lines.append("Time: all available data in the selected scans")
    if radius_m is not None:
        lines.append(f"Candidate radius: {round(radius_m)} m")
    if min_dbm is not None:
        lines.append(f"Minimum displayed strength: {min_dbm:g} dBm")
    return lines


def h3_cell_for_event(row: dict[str, Any], resolution: int) -> str | None:
    if resolution in (8, 9, 10):
        existing = row.get(f"h3_r{resolution}")
        if existing:
            return str(existing)
    if row.get("lat") is None or row.get("lon") is None:
        return None
    return latlon_to_cell(float(row["lat"]), float(row["lon"]), resolution)


def query_events_for_map(
    *,
    collection_id: int | None = None,
    collection_ids: str | None = None,
    min_dbm: float | None = None,
    start_utc: str | None = None,
    end_utc: str | None = None,
) -> list[dict[str, Any]]:
    where = ["valid = 1", "lat IS NOT NULL", "lon IS NOT NULL", "strength_dbm IS NOT NULL"]
    params: list[Any] = []
    ids = parse_collection_ids(collection_id=collection_id, collection_ids=collection_ids)
    add_collection_filter(where, params, ids)
    if min_dbm is not None:
        where.append("strength_dbm >= ?")
        params.append(min_dbm)
    add_time_filter(where, params, start_utc, end_utc)
    conn = connect()
    rows = rows_to_dicts(conn.execute(
        f"""
        SELECT event_id, collection_id, timestamp_utc, lat, lon, frequency_hz, strength_dbm,
               h3_r8, h3_r9, h3_r10
        FROM moth_events
        WHERE {" AND ".join(where)}
        """,
        params,
    ).fetchall())
    conn.close()
    return rows


def dt_bucket_summary(events: list[dict[str, Any]], bucket_seconds: int | None = None) -> dict[str, Any]:
    parsed: list[tuple[datetime, float]] = []
    for row in events:
        dt = parse_dt(row.get("timestamp_utc"))
        if dt is None or row.get("strength_dbm") is None:
            continue
        parsed.append((dt, float(row["strength_dbm"])))
    if not parsed:
        return {
            "label": "NO TIMELINE",
            "definition": "Timeline assessment groups target-band detections into time buckets inside the candidate radius. No target-band detections were found, so no time stability can be assessed.",
            "plain_summary": "No target-band detections were available for this candidate under the active filters.",
            "points": [],
            "bucket_minutes": None,
            "coverage_ratio": 0.0,
        }
    first = min(dt for dt, _ in parsed)
    last = max(dt for dt, _ in parsed)
    span_s = max(1.0, (last - first).total_seconds())
    if bucket_seconds is None:
        if span_s <= 6 * 3600:
            bucket_seconds = 10 * 60
        elif span_s <= 24 * 3600:
            bucket_seconds = 30 * 60
        elif span_s <= 4 * 24 * 3600:
            bucket_seconds = 60 * 60
        else:
            bucket_seconds = 4 * 3600
    first_epoch = int(first.timestamp())
    buckets: dict[int, list[float]] = {}
    for dt, strength in parsed:
        offset = int((dt.timestamp() - first_epoch) // bucket_seconds)
        buckets.setdefault(offset, []).append(strength)
    points: list[dict[str, Any]] = []
    for offset in sorted(buckets):
        vals = buckets[offset]
        t = datetime.fromtimestamp(first_epoch + offset * bucket_seconds, tz=timezone.utc)
        points.append({
            "timestamp_utc": iso_no_ms(t),
            "event_count": len(vals),
            "avg_dbm": round(sum(vals) / len(vals), 1),
            "min_dbm": round(min(vals), 1),
            "max_dbm": round(max(vals), 1),
        })
    total_bucket_count = max(1, int(span_s // bucket_seconds) + 1)
    coverage_ratio = len(points) / total_bucket_count
    avg_values = [float(p["avg_dbm"]) for p in points]
    avg_range = max(avg_values) - min(avg_values) if avg_values else 0.0
    best = max(points, key=lambda p: (float(p["avg_dbm"]), int(p["event_count"])))
    weakest = min(points, key=lambda p: (float(p["avg_dbm"]), int(p["event_count"])))
    if len(points) < 3:
        label = "SPARSE TIMELINE"
        plain = "Only a small number of time buckets contain matching detections. Treat the time behaviour as unproven."
    elif coverage_ratio >= 0.55 and avg_range <= 10.0:
        label = "STABLE TIMELINE"
        plain = "Detections are spread through much of the selected period and average signal strength is relatively steady."
    elif coverage_ratio >= 0.30:
        label = "VARIABLE TIMELINE"
        plain = "Detections appear across the selected period but the signal level or event density changes noticeably over time."
    else:
        label = "INTERMITTENT TIMELINE"
        plain = "Detections are concentrated in limited time windows. This may be real intermittency or a result of the MOTH event-based logging method."
    return {
        "label": label,
        "definition": "Timeline assessment = target-band detections near this candidate, grouped into time buckets. It is a stability indicator, not proof of continuous communications coverage, because MOTH logs detected events rather than guaranteed empty-spectrum samples.",
        "plain_summary": plain,
        "bucket_minutes": round(bucket_seconds / 60),
        "first_timestamp_utc": iso_no_ms(first),
        "last_timestamp_utc": iso_no_ms(last),
        "observed_bucket_count": len(points),
        "possible_bucket_count": total_bucket_count,
        "coverage_ratio": round(coverage_ratio, 3),
        "avg_dbm_range": round(avg_range, 1),
        "best_window": best,
        "weakest_window": weakest,
        "points": points[:48],
    }


def top_frequency_summary(events: list[dict[str, Any]], limit: int = 8) -> list[dict[str, Any]]:
    grouped: dict[float, list[float]] = {}
    for row in events:
        if row.get("frequency_hz") is None or row.get("strength_dbm") is None:
            continue
        mhz = round(float(row["frequency_hz"]) / 1_000_000.0, 3)
        grouped.setdefault(mhz, []).append(float(row["strength_dbm"]))
    out = []
    for mhz, vals in grouped.items():
        out.append({"frequency_mhz": mhz, "event_count": len(vals), "avg_dbm": round(sum(vals) / len(vals), 1), "min_dbm": round(min(vals), 1), "max_dbm": round(max(vals), 1)})
    out.sort(key=lambda r: r["event_count"], reverse=True)
    return out[:limit]


@app.get("/api/h3.geojson")
def h3_geojson(
    resolution: int = Query(default=11, ge=8, le=12),
    collection_id: int | None = None,
    collection_ids: str | None = None,
    min_dbm: float | None = None,
    target_min_hz: float | None = None,
    target_max_hz: float | None = None,
    start_utc: str | None = None,
    end_utc: str | None = None,
) -> dict[str, Any]:
    rows = query_events_for_map(
        collection_id=collection_id,
        collection_ids=collection_ids,
        min_dbm=min_dbm,
        start_utc=start_utc,
        end_utc=end_utc,
    )
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not event_is_target_frequency(row.get("frequency_hz"), target_min_hz, target_max_hz):
            continue
        cell = h3_cell_for_event(row, resolution)
        if not cell:
            continue
        g = grouped.setdefault(cell, {
            "h3_cell": cell,
            "event_count": 0,
            "strengths": [],
            "first_timestamp_utc": None,
            "last_timestamp_utc": None,
        })
        g["event_count"] += 1
        g["strengths"].append(float(row["strength_dbm"]))
        ts = row.get("timestamp_utc")
        if ts:
            if g["first_timestamp_utc"] is None or ts < g["first_timestamp_utc"]:
                g["first_timestamp_utc"] = ts
            if g["last_timestamp_utc"] is None or ts > g["last_timestamp_utc"]:
                g["last_timestamp_utc"] = ts

    features = []
    for cell, g in grouped.items():
        vals = g["strengths"]
        boundary = cell_to_boundary_lnglat(cell)
        if not boundary:
            continue
        features.append({
            "type": "Feature",
            "geometry": {"type": "Polygon", "coordinates": [boundary]},
            "properties": {
                "h3_cell": cell,
                "event_count": g["event_count"],
                "avg_dbm": round(sum(vals) / len(vals), 1),
                "min_dbm": round(min(vals), 1),
                "max_dbm": round(max(vals), 1),
                "first_timestamp_utc": g["first_timestamp_utc"],
                "last_timestamp_utc": g["last_timestamp_utc"],
            },
        })
    return {"type": "FeatureCollection", "features": features}


@app.get("/api/antenna-suitability.geojson")
def antenna_suitability_geojson(
    resolution: int = Query(default=11, ge=8, le=12),
    collection_id: int | None = None,
    collection_ids: str | None = None,
    target_min_hz: float | None = None,
    target_max_hz: float | None = None,
    min_dbm: float | None = None,
    start_utc: str | None = None,
    end_utc: str | None = None,
) -> dict[str, Any]:
    rows = query_events_for_map(
        collection_id=collection_id,
        collection_ids=collection_ids,
        min_dbm=min_dbm,
        start_utc=start_utc,
        end_utc=end_utc,
    )
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        cell = h3_cell_for_event(row, resolution)
        if not cell:
            continue
        g = grouped.setdefault(cell, {
            "h3_cell": cell,
            "all_event_count": 0,
            "target_event_count": 0,
            "strong_non_target_count": 0,
            "target_strengths": [],
            "all_strengths": [],
            "first_timestamp_utc": None,
            "last_timestamp_utc": None,
        })
        strength = float(row["strength_dbm"])
        g["all_event_count"] += 1
        g["all_strengths"].append(strength)
        if event_is_target_frequency(row.get("frequency_hz"), target_min_hz, target_max_hz):
            g["target_event_count"] += 1
            g["target_strengths"].append(strength)
        elif strength >= -60.0:
            g["strong_non_target_count"] += 1
        ts = row.get("timestamp_utc")
        if ts:
            if g["first_timestamp_utc"] is None or ts < g["first_timestamp_utc"]:
                g["first_timestamp_utc"] = ts
            if g["last_timestamp_utc"] is None or ts > g["last_timestamp_utc"]:
                g["last_timestamp_utc"] = ts

    max_target_count = max((int(g.get("target_event_count") or 0) for g in grouped.values()), default=1)
    features: list[dict[str, Any]] = []
    for cell, g in grouped.items():
        target_count = int(g.get("target_event_count") or 0)
        if target_count <= 0:
            continue
        all_count = int(g.get("all_event_count") or 0)
        strong_non_target = int(g.get("strong_non_target_count") or 0)
        target_strengths = g["target_strengths"]
        all_strengths = g["all_strengths"]
        avg_dbm = sum(target_strengths) / len(target_strengths) if target_strengths else None
        min_target_dbm_val = min(target_strengths) if target_strengths else None

        avg_strength_score = clamp(((avg_dbm if avg_dbm is not None else -120.0) + 100.0) / 45.0, 0.0, 1.0)
        floor_strength_score = clamp(((min_target_dbm_val if min_target_dbm_val is not None else -120.0) + 105.0) / 45.0, 0.0, 1.0)
        density_score = clamp(target_count / max_target_count, 0.0, 1.0)
        confidence_score = clamp(target_count / 25.0, 0.0, 1.0)
        interference_ratio = strong_non_target / max(all_count, 1)
        low_interference_score = 1.0 - clamp(interference_ratio * 3.0, 0.0, 1.0)

        suitability = (
            0.30 * avg_strength_score
            + 0.20 * floor_strength_score
            + 0.20 * density_score
            + 0.15 * confidence_score
            + 0.15 * low_interference_score
        )
        suitability_score = round(suitability * 100.0, 1)
        boundary = cell_to_boundary_lnglat(cell)
        if not boundary:
            continue
        features.append({
            "type": "Feature",
            "geometry": {"type": "Polygon", "coordinates": [boundary]},
            "properties": {
                "h3_cell": cell,
                "all_event_count": all_count,
                "target_event_count": target_count,
                "strong_non_target_count": strong_non_target,
                "avg_target_dbm": round(avg_dbm, 1) if avg_dbm is not None else None,
                "min_target_dbm": round(min_target_dbm_val, 1) if min_target_dbm_val is not None else None,
                "max_target_dbm": round(max(target_strengths), 1) if target_strengths else None,
                "avg_dbm": round(sum(all_strengths) / len(all_strengths), 1) if all_strengths else None,
                "suitability_score": suitability_score,
                "avg_strength_score": round(avg_strength_score, 3),
                "floor_strength_score": round(floor_strength_score, 3),
                "density_score": round(density_score, 3),
                "confidence_score": round(confidence_score, 3),
                "low_interference_score": round(low_interference_score, 3),
                "interpretation": "GOOD" if suitability_score >= 75 else "CHECK" if suitability_score >= 50 else "POOR",
                "first_timestamp_utc": g["first_timestamp_utc"],
                "last_timestamp_utc": g["last_timestamp_utc"],
            },
        })
    return {"type": "FeatureCollection", "features": features}


@app.get("/api/time-series")
def time_series(
    collection_id: int | None = None,
    collection_ids: str | None = None,
    min_dbm: float | None = None,
    target_min_hz: float | None = None,
    target_max_hz: float | None = None,
    start_utc: str | None = None,
    end_utc: str | None = None,
    candidate_id: int | None = None,
    radius_m: float = Query(default=1500.0, ge=50, le=10000),
    bucket_seconds: int | None = Query(default=None, ge=1, le=86400),
    max_rows: int = Query(default=250000, ge=1000, le=1000000),
) -> dict[str, Any]:
    where = ["valid = 1", "timestamp_utc IS NOT NULL", "strength_dbm IS NOT NULL"]
    candidate: dict[str, Any] | None = None
    if candidate_id is not None:
        where.extend(["lat IS NOT NULL", "lon IS NOT NULL"])
    params: list[Any] = []
    add_event_filters(
        where,
        params,
        collection_id=collection_id,
        collection_ids=collection_ids,
        min_dbm=min_dbm,
        target_min_hz=target_min_hz,
        target_max_hz=target_max_hz,
        start_utc=start_utc,
        end_utc=end_utc,
    )
    conn = connect()
    if candidate_id is not None:
        candidate = rows_to_dicts(conn.execute(
            "SELECT * FROM candidate_sites WHERE site_id = ?",
            (candidate_id,),
        ).fetchall())
        if not candidate:
            conn.close()
            raise HTTPException(status_code=404, detail="Candidate not found")
        candidate = candidate[0]
    select_cols = "timestamp_utc, strength_dbm, lat, lon" if candidate_id is not None else "timestamp_utc, strength_dbm"
    rows = rows_to_dicts(conn.execute(
        f"SELECT {select_cols} FROM moth_events WHERE " + " AND ".join(where) + " ORDER BY timestamp_utc LIMIT ?",
        params + [max_rows],
    ).fetchall())
    conn.close()

    parsed: list[tuple[datetime, float]] = []
    for row in rows:
        dt = parse_dt(row.get("timestamp_utc"))
        if dt is None or row.get("strength_dbm") is None:
            continue
        if candidate is not None:
            if row.get("lat") is None or row.get("lon") is None:
                continue
            distance = haversine_m(float(candidate["lat"]), float(candidate["lon"]), float(row["lat"]), float(row["lon"]))
            if distance > radius_m:
                continue
        parsed.append((dt, float(row["strength_dbm"])))
    if not parsed:
        response: dict[str, Any] = {"bucket_seconds": bucket_seconds or 60, "points": [], "row_count": 0}
        if candidate is not None:
            response.update({"candidate_id": candidate["site_id"], "candidate_name": candidate["name"], "radius_m": radius_m})
        return response

    first = min(dt for dt, _ in parsed)
    last = max(dt for dt, _ in parsed)
    span_s = max(1.0, (last - first).total_seconds())
    if bucket_seconds is None:
        if span_s <= 2 * 3600:
            bucket_seconds = 60
        elif span_s <= 24 * 3600:
            bucket_seconds = 300
        elif span_s <= 7 * 24 * 3600:
            bucket_seconds = 1800
        else:
            bucket_seconds = 3600

    buckets: dict[int, list[float]] = {}
    first_epoch = int(first.timestamp())
    for dt, strength in parsed:
        offset = int((dt.timestamp() - first_epoch) // bucket_seconds)
        buckets.setdefault(offset, []).append(strength)

    points: list[dict[str, Any]] = []
    for offset in sorted(buckets):
        vals = buckets[offset]
        t = datetime.fromtimestamp(first_epoch + offset * bucket_seconds, tz=timezone.utc)
        points.append({
            "timestamp_utc": iso_no_ms(t),
            "event_count": len(vals),
            "avg_dbm": round(sum(vals) / len(vals), 1),
            "min_dbm": round(min(vals), 1),
            "max_dbm": round(max(vals), 1),
        })
    response: dict[str, Any] = {
        "bucket_seconds": bucket_seconds,
        "row_count": len(parsed),
        "first_timestamp_utc": iso_no_ms(first),
        "last_timestamp_utc": iso_no_ms(last),
        "points": points,
    }
    if candidate is not None:
        response.update({"candidate_id": candidate["site_id"], "candidate_name": candidate["name"], "radius_m": radius_m})
    return response


@app.post("/api/candidates")
def add_candidate(candidate: CandidateIn) -> dict[str, Any]:
    site_id = insert_candidate_site(**candidate.model_dump())
    return {"site_id": site_id, **candidate.model_dump()}


@app.post("/api/candidates/import")
def import_candidates(file: Annotated[UploadFile, File()]) -> dict[str, Any]:
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Upload candidate CSV with name,lat,lon columns")
    text = file.file.read().decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(text.splitlines())
    imported = 0
    for row in reader:
        try:
            insert_candidate_site(
                name=row["name"],
                lat=float(row["lat"]),
                lon=float(row["lon"]),
                antenna_height_agl_m=float(row["antenna_height_agl_m"]) if row.get("antenna_height_agl_m") else None,
                practical_score=float(row["practical_score"]) if row.get("practical_score") else 0.5,
                site_notes=row.get("site_notes"),
            )
            imported += 1
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Bad candidate row {row}: {exc}") from exc
    return {"imported": imported}


@app.get("/api/candidates")
def candidates() -> list[dict[str, Any]]:
    conn = connect()
    rows = rows_to_dicts(conn.execute("SELECT * FROM candidate_sites ORDER BY name").fetchall())
    conn.close()
    return rows


@app.get("/api/candidates.geojson")
def candidates_geojson() -> dict[str, Any]:
    return {
        "type": "FeatureCollection",
        "features": [point_feature(row) for row in candidates()],
    }


@app.delete("/api/candidates")
def clear_candidates() -> dict[str, Any]:
    conn = connect()
    with conn:
        cur = conn.execute("DELETE FROM candidate_sites")
    conn.close()
    return {"deleted_candidates": cur.rowcount}


@app.delete("/api/candidates/{site_id}")
def delete_candidate(site_id: int) -> dict[str, Any]:
    conn = connect()
    with conn:
        cur = conn.execute("DELETE FROM candidate_sites WHERE site_id = ?", (site_id,))
    conn.close()
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="Candidate not found")
    return {"deleted_site_id": site_id}


@app.get("/api/candidate-assessment/{site_id}")
def candidate_assessment(
    site_id: str,
    radius_m: float = Query(default=1500.0, ge=50, le=10000),
    target_min_hz: float | None = None,
    target_max_hz: float | None = None,
    min_dbm: float | None = None,
    collection_ids: str | None = None,
    start_utc: str | None = None,
    end_utc: str | None = None,
) -> dict[str, Any]:
    """Plain-English one-page assessment payload for a candidate.

    v0.6.0 adds briefing mode, candidate comparison, confidence overlay, sparse-data warnings, and a one-page PDF export.
    """
    try:
        site_id_int = int(site_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Select a valid candidate pin or candidate score row before requesting an assessment") from exc

    conn = connect()
    candidate_rows = rows_to_dicts(conn.execute(
        "SELECT * FROM candidate_sites WHERE site_id = ?",
        (site_id_int,),
    ).fetchall())
    conn.close()
    if not candidate_rows:
        raise HTTPException(status_code=404, detail="Candidate not found in candidate_sites. Refresh candidates or add the candidate again.")
    candidate_base = candidate_rows[0]

    ids = parse_collection_ids(collection_ids=collection_ids)
    scores = score_candidate_sites(
        radius_m=radius_m,
        target_min_hz=target_min_hz,
        target_max_hz=target_max_hz,
        collection_ids=ids,
        start_utc=start_utc,
        end_utc=end_utc,
        min_dbm=min_dbm,
    )
    match = next((s for s in scores if int(s.get("site_id", -1)) == site_id_int), None)
    if match is None:
        match = {
            "site_id": site_id_int,
            "name": candidate_base.get("name") or f"Candidate {site_id_int}",
            "lat": candidate_base.get("lat"),
            "lon": candidate_base.get("lon"),
            "antenna_height_agl_m": candidate_base.get("antenna_height_agl_m"),
            "practical_score": candidate_base.get("practical_score"),
            "site_notes": candidate_base.get("site_notes"),
            "radius_m": radius_m,
            "score_0_100": 0.0,
            "target_event_count": 0,
            "all_event_count": 0,
            "strong_non_target_count": 0,
            "target_strength_p10_dbm": None,
            "target_strength_median_dbm": None,
            "data_confidence": 0.0,
            "rank": None,
        }

    # Build direct nearby evidence for this candidate. This deliberately repeats the spatial filter so the assessment can show richer evidence than the score table.
    all_rows = query_events_for_map(
        collection_ids=collection_ids,
        min_dbm=min_dbm,
        start_utc=start_utc,
        end_utc=end_utc,
    )
    nearby_all: list[dict[str, Any]] = []
    nearby_target: list[dict[str, Any]] = []
    clat = float(candidate_base["lat"])
    clon = float(candidate_base["lon"])
    for row in all_rows:
        d = haversine_m(clat, clon, float(row["lat"]), float(row["lon"]))
        if d > radius_m:
            continue
        r = dict(row)
        r["distance_m"] = round(d, 1)
        nearby_all.append(r)
        if event_is_target_frequency(row.get("frequency_hz"), target_min_hz, target_max_hz):
            nearby_target.append(r)

    strengths = [float(e["strength_dbm"]) for e in nearby_target if e.get("strength_dbm") is not None]
    p10 = percentile(strengths, 0.10)
    p50 = percentile(strengths, 0.50)
    p90 = percentile(strengths, 0.90)
    avg = sum(strengths) / len(strengths) if strengths else None
    strong_non_target = [
        e for e in nearby_all
        if not event_is_target_frequency(e.get("frequency_hz"), target_min_hz, target_max_hz)
        and e.get("strength_dbm") is not None
        and float(e["strength_dbm"]) >= -60.0
    ]

    score = float(match.get("score_0_100") or 0.0)
    event_count = len(nearby_target)
    all_count = len(nearby_all)
    confidence = float(match.get("data_confidence") or 0.0)
    interference = len(strong_non_target)

    if score >= 75 and confidence >= 0.5:
        overall = "GOOD candidate - strong first-pass evidence"
        action = "Prioritise this site for practical survey, mast/antenna trial, and confirmation against access, security, power and antenna-height constraints."
    elif score >= 50:
        overall = "CHECK candidate - usable but needs validation"
        action = "Keep this site on the shortlist, but compare it against stronger alternatives and collect another pass if possible."
    elif event_count == 0:
        overall = "UNPROVEN candidate - no matching target-band data nearby"
        action = "Do not reject solely on this result. Widen the radius/filter or collect another MOTH pass near the candidate."
    else:
        overall = "POOR candidate - weak first-pass evidence"
        action = "Do not prioritise this site unless practical constraints force its use; collect more data or test alternatives first."

    timeline = dt_bucket_summary(nearby_target)
    top_freqs = top_frequency_summary(nearby_target)

    reasons: list[str] = []
    if event_count <= 0:
        reasons.append("No target-band detections were found inside the selected radius and active filters.")
    else:
        reasons.append(f"{event_count} target-band detections were found within {round(radius_m)} m of the candidate.")
    if all_count > event_count:
        reasons.append(f"{all_count} total RF detections were found nearby; {all_count - event_count} were outside the selected target band.")
    if p10 is not None:
        reasons.append(f"Lower-tail signal strength is {round(p10, 1)} dBm. This conservative value is more useful than a peak reading.")
    else:
        reasons.append("There is no lower-tail dBm value because no target-band detections matched this candidate/filter combination.")
    if p50 is not None:
        reasons.append(f"Median target-band signal strength is {round(p50, 1)} dBm.")
    if interference > 0:
        reasons.append(f"{interference} strong non-target detections were found nearby, so local interference risk should be checked.")
    if confidence < 0.5:
        reasons.append("Data confidence is low. This should be treated as a planning cue, not a final siting decision.")
    else:
        reasons.append("Data confidence is adequate for a first-pass comparison between candidate sites.")

    metrics = {
        "score_0_100": round(score, 1),
        "rank": match.get("rank"),
        "target_event_count": event_count,
        "all_event_count": all_count,
        "non_target_event_count": max(0, all_count - event_count),
        "strong_non_target_count": interference,
        "target_strength_p10_dbm": round(p10, 1) if p10 is not None else None,
        "target_strength_median_dbm": round(p50, 1) if p50 is not None else None,
        "target_strength_p90_dbm": round(p90, 1) if p90 is not None else None,
        "target_strength_avg_dbm": round(avg, 1) if avg is not None else None,
        "target_strength_min_dbm": round(min(strengths), 1) if strengths else None,
        "target_strength_max_dbm": round(max(strengths), 1) if strengths else None,
        "data_confidence": round(confidence, 3),
    }

    return {
        "candidate": match,
        "overall": overall,
        "recommended_action": action,
        "plain_reasons": reasons,
        "metrics": metrics,
        "timeline": timeline,
        "top_frequencies": top_freqs,
        "filters": {
            "radius_m": radius_m,
            "target_min_hz": target_min_hz,
            "target_max_hz": target_max_hz,
            "min_dbm": min_dbm,
            "collection_ids": ids,
            "start_utc": start_utc,
            "end_utc": end_utc,
        },
        "filters_human": human_filter_lines(
            radius_m=radius_m,
            target_min_hz=target_min_hz,
            target_max_hz=target_max_hz,
            collection_ids=ids,
            start_utc=start_utc,
            end_utc=end_utc,
            min_dbm=min_dbm,
        ),
        "limitations": [
            "MOTH data is event-based. No event does not always mean no signal.",
            "This is a first-pass siting assessment. It should be validated with a controlled antenna trial and known airframe or representative signal activity.",
            "Candidate scoring uses received MOTH detections and does not yet include terrain, mast height, cable loss, antenna pattern or authorised transmit-power constraints.",
        ],
        "draft_report_title": f"Candidate assessment - {match.get('name')}",
    }


@app.get("/api/candidate-assessment")
def candidate_assessment_query(
    site_id: int = Query(...),
    radius_m: float = Query(default=1500.0, ge=50, le=10000),
    target_min_hz: float | None = None,
    target_max_hz: float | None = None,
    min_dbm: float | None = None,
    collection_ids: str | None = None,
    start_utc: str | None = None,
    end_utc: str | None = None,
) -> dict[str, Any]:
    return candidate_assessment(
        str(site_id),
        radius_m=radius_m,
        target_min_hz=target_min_hz,
        target_max_hz=target_max_hz,
        min_dbm=min_dbm,
        collection_ids=collection_ids,
        start_utc=start_utc,
        end_utc=end_utc,
    )

@app.get("/api/score-candidates")
def score_candidates(
    radius_m: float = Query(default=1500.0, ge=50, le=10000),
    target_min_hz: float | None = None,
    target_max_hz: float | None = None,
    min_dbm: float | None = None,
    collection_ids: str | None = None,
    start_utc: str | None = None,
    end_utc: str | None = None,
) -> list[dict[str, Any]]:
    ids = parse_collection_ids(collection_ids=collection_ids)
    return score_candidate_sites(
        radius_m=radius_m,
        target_min_hz=target_min_hz,
        target_max_hz=target_max_hz,
        collection_ids=ids,
        start_utc=start_utc,
        end_utc=end_utc,
        min_dbm=min_dbm,
    )

# -----------------------------
# v0.6.0 briefing / confidence / PDF helpers
# -----------------------------

@app.get("/api/confidence.geojson")
def confidence_geojson(
    resolution: int = Query(default=11, ge=8, le=12),
    collection_id: int | None = None,
    collection_ids: str | None = None,
    target_min_hz: float | None = None,
    target_max_hz: float | None = None,
    min_dbm: float | None = None,
    start_utc: str | None = None,
    end_utc: str | None = None,
) -> dict[str, Any]:
    """Separate confidence overlay for briefing mode.

    Confidence is based on the number of target-band detections per hex cell. It deliberately
    does not mean RF quality; it means how much evidence exists in that cell under the active filters.
    """
    rows = query_events_for_map(
        collection_id=collection_id,
        collection_ids=collection_ids,
        min_dbm=min_dbm,
        start_utc=start_utc,
        end_utc=end_utc,
    )
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not event_is_target_frequency(row.get("frequency_hz"), target_min_hz, target_max_hz):
            continue
        cell = h3_cell_for_event(row, resolution)
        if not cell:
            continue
        g = grouped.setdefault(cell, {
            "h3_cell": cell,
            "target_event_count": 0,
            "strengths": [],
            "first_timestamp_utc": None,
            "last_timestamp_utc": None,
        })
        g["target_event_count"] += 1
        if row.get("strength_dbm") is not None:
            g["strengths"].append(float(row["strength_dbm"]))
        ts = row.get("timestamp_utc")
        if ts:
            if g["first_timestamp_utc"] is None or ts < g["first_timestamp_utc"]:
                g["first_timestamp_utc"] = ts
            if g["last_timestamp_utc"] is None or ts > g["last_timestamp_utc"]:
                g["last_timestamp_utc"] = ts

    features: list[dict[str, Any]] = []
    for cell, g in grouped.items():
        count = int(g.get("target_event_count") or 0)
        # Conservative confidence ladder: enough for briefing, not a statistical guarantee.
        confidence_score = clamp(count / 40.0, 0.0, 1.0)
        if count >= 40:
            label = "HIGH CONFIDENCE"
        elif count >= 12:
            label = "MEDIUM CONFIDENCE"
        elif count >= 3:
            label = "LOW CONFIDENCE"
        else:
            label = "VERY LOW CONFIDENCE"
        boundary = cell_to_boundary_lnglat(cell)
        if not boundary:
            continue
        vals = g.get("strengths") or []
        features.append({
            "type": "Feature",
            "geometry": {"type": "Polygon", "coordinates": [boundary]},
            "properties": {
                "h3_cell": cell,
                "target_event_count": count,
                "confidence_score": round(confidence_score, 3),
                "confidence_label": label,
                "avg_dbm": round(sum(vals) / len(vals), 1) if vals else None,
                "first_timestamp_utc": g.get("first_timestamp_utc"),
                "last_timestamp_utc": g.get("last_timestamp_utc"),
                "plain_meaning": "Evidence density only: high confidence means many matching detections were logged here under the active filters.",
            },
        })
    return {"type": "FeatureCollection", "features": features}


@app.get("/api/data-limitations")
def data_limitations(
    radius_m: float = Query(default=1500.0, ge=50, le=10000),
    target_min_hz: float | None = None,
    target_max_hz: float | None = None,
    min_dbm: float | None = None,
    collection_ids: str | None = None,
    start_utc: str | None = None,
    end_utc: str | None = None,
) -> dict[str, Any]:
    """Briefing-friendly warnings for sparse or ambiguous evidence."""
    where = ["valid = 1"]
    params: list[Any] = []
    ids = add_event_filters(
        where,
        params,
        collection_ids=collection_ids,
        min_dbm=min_dbm,
        target_min_hz=target_min_hz,
        target_max_hz=target_max_hz,
        start_utc=start_utc,
        end_utc=end_utc,
    )
    conn = connect()
    totals = dict(conn.execute(
        f"""
        SELECT COUNT(*) AS target_events,
               COUNT(DISTINCT collection_id) AS scan_count,
               MIN(timestamp_utc) AS first_timestamp_utc,
               MAX(timestamp_utc) AS last_timestamp_utc,
               MIN(lat) AS min_lat, MAX(lat) AS max_lat,
               MIN(lon) AS min_lon, MAX(lon) AS max_lon
        FROM moth_events
        WHERE {" AND ".join(where)}
        """,
        params,
    ).fetchone())
    conn.close()
    warnings: list[str] = []
    target_events = int(totals.get("target_events") or 0)
    scan_count = int(totals.get("scan_count") or 0)
    if target_min_hz is None and target_max_hz is None:
        warnings.append("No target frequency window is selected, so the map is mixing all logged detections.")
    if target_events == 0:
        warnings.append("No matching target-band detections are present under the active filters.")
    elif target_events < 50:
        warnings.append(f"Sparse evidence: only {target_events} matching detections are present under the active filters.")
    if scan_count <= 1:
        warnings.append("Only one scan/collection contributes to this view; repeat-pass confidence is limited.")
    if start_utc or end_utc:
        warnings.append("A time window is active, so the map is showing a slice of the survey rather than the full collection.")
    if min_dbm is not None and float(min_dbm) > -120:
        warnings.append("A minimum dBm filter is active; weaker detections are hidden from the current interpretation.")
    if not warnings:
        warnings.append("No major data limitation was detected for the current filter, but field validation is still required before final antenna siting.")
    return {
        "warnings": warnings,
        "target_events": target_events,
        "scan_count": scan_count,
        "selected_collection_ids": ids,
        "first_timestamp_utc": totals.get("first_timestamp_utc"),
        "last_timestamp_utc": totals.get("last_timestamp_utc"),
    }


@app.get("/api/candidate-comparison")
def candidate_comparison(
    radius_m: float = Query(default=1500.0, ge=50, le=10000),
    target_min_hz: float | None = None,
    target_max_hz: float | None = None,
    min_dbm: float | None = None,
    collection_ids: str | None = None,
    start_utc: str | None = None,
    end_utc: str | None = None,
    limit: int = Query(default=3, ge=1, le=10),
) -> dict[str, Any]:
    ids = parse_collection_ids(collection_ids=collection_ids)
    scores = score_candidate_sites(
        radius_m=radius_m,
        target_min_hz=target_min_hz,
        target_max_hz=target_max_hz,
        collection_ids=ids,
        start_utc=start_utc,
        end_utc=end_utc,
        min_dbm=min_dbm,
    )
    rows = sorted(scores, key=lambda r: (r.get("rank") is None, r.get("rank") or 999999))[:limit]
    interpreted: list[dict[str, Any]] = []
    for r in rows:
        score = float(r.get("score_0_100") or 0.0)
        events = int(r.get("target_event_count") or 0)
        conf = float(r.get("data_confidence") or 0.0)
        if score >= 75 and conf >= 0.5:
            decision = "SHORTLIST"
            why = "Strongest first-pass candidate evidence under the active filters."
        elif events <= 0:
            decision = "UNPROVEN"
            why = "No matching target-band evidence was found nearby."
        elif score >= 50:
            decision = "CHECK"
            why = "Potentially usable but needs validation against stronger alternatives."
        else:
            decision = "LOW PRIORITY"
            why = "Weak or sparse first-pass evidence."
        out = dict(r)
        out.update({"decision": decision, "plain_why": why})
        interpreted.append(out)
    return {
        "limit": limit,
        "radius_m": radius_m,
        "filter_summary": human_filter_lines(
            radius_m=radius_m,
            target_min_hz=target_min_hz,
            target_max_hz=target_max_hz,
            collection_ids=ids,
            start_utc=start_utc,
            end_utc=end_utc,
            min_dbm=min_dbm,
        ),
        "candidates": interpreted,
    }


def _pdf_escape(text: Any) -> str:
    s = str(text if text is not None else "")
    s = s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    # Built-in Helvetica is WinAnsi-ish. Replace non-ASCII glyphs safely.
    return s.encode("latin-1", "replace").decode("latin-1")


def _wrap_text(text: Any, width: int = 86) -> list[str]:
    words = str(text if text is not None else "").replace("\n", " ").split()
    lines: list[str] = []
    line = ""
    for word in words:
        if len(line) + len(word) + (1 if line else 0) <= width:
            line = f"{line} {word}".strip()
        else:
            if line:
                lines.append(line)
            line = word[:width]
    if line:
        lines.append(line)
    return lines or [""]


def _simple_one_page_pdf(lines: list[tuple[str, int, str]]) -> bytes:
    """Tiny dependency-free one-page PDF using built-in Helvetica.

    lines entries: (text, font_size, style), style in normal/bold. This is intentionally simple
    so the Pi does not need an extra PDF package.
    """
    width, height = 595, 842
    y = 810
    commands = ["q", "BT"]
    for text, size, style in lines:
        if y < 36:
            break
        font = "/F2" if style == "bold" else "/F1"
        safe = _pdf_escape(text)
        commands.append(f"{font} {size} Tf 1 0 0 1 50 {y} Tm ({safe}) Tj")
        y -= max(size + 4, 13)
    commands.extend(["ET", "Q"])
    content = "\n".join(commands).encode("latin-1", "replace")
    objects: list[bytes] = []
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
    objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width} {height}] /Resources << /Font << /F1 4 0 R /F2 5 0 R >> >> /Contents 6 0 R >>".encode())
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>")
    objects.append(f"<< /Length {len(content)} >>\nstream\n".encode() + content + b"\nendstream")
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out.extend(f"{i} 0 obj\n".encode())
        out.extend(obj)
        out.extend(b"\nendobj\n")
    xref = len(out)
    out.extend(f"xref\n0 {len(objects)+1}\n".encode())
    out.extend(b"0000000000 65535 f \n")
    for off in offsets[1:]:
        out.extend(f"{off:010d} 00000 n \n".encode())
    out.extend(f"trailer\n<< /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return bytes(out)


def _assessment_pdf_lines(payload: dict[str, Any]) -> list[tuple[str, int, str]]:
    c = payload.get("candidate") or {}
    m = payload.get("metrics") or {}
    t = payload.get("timeline") or {}
    lines: list[tuple[str, int, str]] = []
    lines.append(("MOTH Candidate Assessment", 16, "bold"))
    lines.append((f"Candidate: {c.get('name', '')}   Position: {c.get('lat', '')}, {c.get('lon', '')}", 9, "normal"))
    lines.append((f"Generated UTC: {iso_no_ms(datetime.now(timezone.utc))}", 8, "normal"))
    lines.append(("Decision", 12, "bold"))
    for line in _wrap_text(payload.get("overall", ""), 90):
        lines.append((line, 10, "bold"))
    rec = payload.get("recommended_action", "")
    for line in _wrap_text(rec, 92)[:3]:
        lines.append((line, 9, "normal"))
    lines.append(("Evidence snapshot", 12, "bold"))
    snapshot = (
        f"Score {m.get('score_0_100', '')}/100; rank {m.get('rank', '')}; "
        f"target detections {m.get('target_event_count', '')}; all nearby detections {m.get('all_event_count', '')}; "
        f"P10 {m.get('target_strength_p10_dbm', '')} dBm; median {m.get('target_strength_median_dbm', '')} dBm; "
        f"confidence {m.get('data_confidence', '')}."
    )
    for line in _wrap_text(snapshot, 92)[:3]:
        lines.append((line, 9, "normal"))
    lines.append(("Why this matters", 12, "bold"))
    for reason in (payload.get("plain_reasons") or [])[:5]:
        for i, line in enumerate(_wrap_text(reason, 88)[:2]):
            lines.append((("- " if i == 0 else "  ") + line, 8, "normal"))
    lines.append(("Timeline", 12, "bold"))
    tl = f"{t.get('label', '')}: {t.get('plain_summary', '')} Buckets {t.get('observed_bucket_count', '')}/{t.get('possible_bucket_count', '')}; variation {t.get('avg_dbm_range', '')} dB."
    for line in _wrap_text(tl, 92)[:4]:
        lines.append((line, 8, "normal"))
    lines.append(("Filters", 12, "bold"))
    for line in (payload.get("filters_human") or [])[:6]:
        for wrapped in _wrap_text(line, 92)[:2]:
            lines.append(("- " + wrapped, 8, "normal"))
    lines.append(("Limitations", 12, "bold"))
    for lim in (payload.get("limitations") or [])[:3]:
        for i, line in enumerate(_wrap_text(lim, 88)[:2]):
            lines.append((("- " if i == 0 else "  ") + line, 8, "normal"))
    return lines


@app.get("/api/candidate-assessment-pdf/{site_id}")
def candidate_assessment_pdf(
    site_id: int,
    radius_m: float = Query(default=1500.0, ge=50, le=10000),
    target_min_hz: float | None = None,
    target_max_hz: float | None = None,
    min_dbm: float | None = None,
    collection_ids: str | None = None,
    start_utc: str | None = None,
    end_utc: str | None = None,
) -> Response:
    payload = candidate_assessment(
        str(site_id),
        radius_m=radius_m,
        target_min_hz=target_min_hz,
        target_max_hz=target_max_hz,
        min_dbm=min_dbm,
        collection_ids=collection_ids,
        start_utc=start_utc,
        end_utc=end_utc,
    )
    pdf = _simple_one_page_pdf(_assessment_pdf_lines(payload))
    name = str((payload.get("candidate") or {}).get("name") or f"candidate_{site_id}").replace(" ", "_")
    headers = {"Content-Disposition": f"attachment; filename=moth_assessment_{name}.pdf"}
    return Response(content=pdf, media_type="application/pdf", headers=headers)


# ---- MOTH v0.7.4 launch RF analysis endpoints ----
# Appended by moth_v070_launch_rf_analysis_patch_20260512.
# These endpoints are intentionally additive and should not disturb the map UI.

from collections import defaultdict as _moth_dd
from datetime import datetime as _moth_datetime, timezone as _moth_timezone, timedelta as _moth_timedelta
import math as _moth_math
from typing import Any as _MothAny
from fastapi import Query as _MothQuery, HTTPException as _MothHTTPException

_MOTH_ADVANCED_VERSION = "0.7.5"
_MOTH_GNSS_BANDS = {
    "L1": {"center_hz": 1575.42e6, "default_width_mhz": 40.0, "note": "GNSS L1 practical Â±20 MHz display window"},
    "L2": {"center_hz": 1227.60e6, "default_width_mhz": 40.0, "note": "GNSS L2 practical Â±20 MHz display window"},
    "L5": {"center_hz": 1176.45e6, "default_width_mhz": 40.0, "note": "GNSS L5 practical Â±20 MHz display window"},
}


def _moth_adv_iso(dt: _moth_datetime) -> str:
    return dt.astimezone(_moth_timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _moth_adv_parse_dt(value):
    if value is None or value == "":
        return None
    if isinstance(value, _moth_datetime):
        return value.astimezone(_moth_timezone.utc) if value.tzinfo else value.replace(tzinfo=_moth_timezone.utc)
    text = str(value).strip().replace("Z", "+00:00")
    try:
        dt = _moth_datetime.fromisoformat(text)
        return dt.astimezone(_moth_timezone.utc) if dt.tzinfo else dt.replace(tzinfo=_moth_timezone.utc)
    except Exception:
        return None


def _moth_adv_parse_ids(collection_id=None, collection_ids=None):
    ids = []
    if collection_id not in (None, ""):
        ids.append(int(collection_id))
    if collection_ids:
        for piece in str(collection_ids).replace(";", ",").split(","):
            piece = piece.strip()
            if piece:
                ids.append(int(piece))
    return sorted(set(ids))


def _moth_adv_band_edges(center_hz: float, width_mhz: float):
    half = float(width_mhz) * 1_000_000.0 / 2.0
    return center_hz - half, center_hz + half


def _moth_adv_gnss_edges(width_mhz: float):
    return {name: (*_moth_adv_band_edges(info["center_hz"], width_mhz), info["center_hz"]) for name, info in _MOTH_GNSS_BANDS.items()}


def _moth_adv_collection_where(ids, where, params):
    if ids:
        placeholders = ",".join("?" for _ in ids)
        where.append(f"collection_id IN ({placeholders})")
        params.extend(ids)


def _moth_adv_query_events(
    *,
    collection_id=None,
    collection_ids=None,
    start_utc=None,
    end_utc=None,
    min_hz=None,
    max_hz=None,
    min_dbm=None,
    max_rows=160000,
):
    ids = _moth_adv_parse_ids(collection_id, collection_ids)
    where = ["valid = 1", "timestamp_utc IS NOT NULL", "frequency_hz IS NOT NULL", "strength_dbm IS NOT NULL"]
    params = []
    _moth_adv_collection_where(ids, where, params)
    if start_utc:
        where.append("timestamp_utc >= ?")
        params.append(str(start_utc))
    if end_utc:
        where.append("timestamp_utc <= ?")
        params.append(str(end_utc))
    if min_hz is not None:
        where.append("frequency_hz >= ?")
        params.append(float(min_hz))
    if max_hz is not None:
        where.append("frequency_hz <= ?")
        params.append(float(max_hz))
    if min_dbm is not None:
        where.append("strength_dbm >= ?")
        params.append(float(min_dbm))
    params.append(int(max_rows))
    conn = connect()
    try:
        rows = rows_to_dicts(conn.execute(
            "SELECT event_id, collection_id, timestamp_utc, frequency_hz, strength_dbm, lat, lon "
            "FROM moth_events WHERE " + " AND ".join(where) + " ORDER BY timestamp_utc ASC LIMIT ?",
            params,
        ).fetchall())
    finally:
        conn.close()
    return rows, ids


def _moth_adv_bucket_dt(dt: _moth_datetime, bucket_seconds: int):
    epoch = int(dt.timestamp())
    return _moth_datetime.fromtimestamp((epoch // bucket_seconds) * bucket_seconds, tz=_moth_timezone.utc)


def _moth_adv_stats(values):
    vals = [float(v) for v in values if v is not None]
    if not vals:
        return {"count": 0, "avg": None, "min": None, "max": None}
    return {
        "count": len(vals),
        "avg": round(sum(vals) / len(vals), 1),
        "min": round(min(vals), 1),
        "max": round(max(vals), 1),
    }


def _moth_adv_percentile(values, p):
    vals = sorted(float(v) for v in values if v is not None)
    if not vals:
        return None
    if len(vals) == 1:
        return round(vals[0], 1)
    k = (len(vals) - 1) * p
    f = int(_moth_math.floor(k))
    c = int(_moth_math.ceil(k))
    if f == c:
        return round(vals[f], 1)
    return round(vals[f] * (c - k) + vals[c] * (k - f), 1)


def _moth_adv_recommendation_label(score, gnss_count, gnss_spikes):
    score = float(score or 0.0)
    gnss_count = int(gnss_count or 0)
    gnss_spikes = int(gnss_spikes or 0)
    if score >= 75.0 and gnss_spikes == 0:
        return "RECOMMENDED", "Recommended launch timing"
    if score >= 60.0:
        return "BEST VIABLE", "Best viable launch timing - validate before launch"
    if score >= 40.0:
        return "LEAST-BUSY OBSERVED", "Least-busy observed timing - higher RF risk"
    if gnss_count == 0 and gnss_spikes == 0:
        return "QUIET BUT UNPROVEN", "Quiet by MOTH detections - verify operationally"
    return "AVOID IF POSSIBLE", "No clean launch timing found; this is only the least-bad window"


def _moth_adv_operator_brief(windows, spectrum=None):
    if not windows:
        return {
            "headline": "No launch timing can be assessed from the current filters.",
            "recommendation": "Broaden the time/frequency filters or load more MOTH data.",
            "rationale": ["No matching MOTH events or insufficient time span were available."],
            "decision_status": "NO DATA",
        }
    best = windows[0]
    label = best.get("recommendation_type") or _moth_adv_recommendation_label(best.get("score_0_100"), best.get("gnss_event_count"), best.get("gnss_spike_count"))[0]
    rationale = [
        f"Score {best.get('score_0_100')}/100 for {best.get('start_utc')} to {best.get('end_utc')} UTC.",
        f"GNSS-window events: {best.get('gnss_event_count')}; strong GNSS spikes: {best.get('gnss_spike_count')}.",
    ]
    if best.get("connectivity_event_count"):
        rationale.append(f"Connectivity-band evidence is present: {best.get('connectivity_event_count')} events.")
    else:
        rationale.append("No connectivity-band evidence was included unless a connectivity band was supplied.")
    if label == "RECOMMENDED":
        recommendation = "Use this as the preferred RF-clean launch window, subject to operational approval and live checks."
    elif label == "BEST VIABLE":
        recommendation = "This is the best viable RF window found, but it should be validated with live checks before launch."
    elif label == "LEAST-BUSY OBSERVED":
        recommendation = "Only a least-busy timing was found. Use only if operationally necessary and after additional checks."
    else:
        recommendation = "No clean launch window is indicated. Delay or collect more data if possible."
    return {
        "headline": f"{label}: {best.get('start_utc')} to {best.get('end_utc')} UTC",
        "recommendation": recommendation,
        "rationale": rationale,
        "decision_status": label,
        "best_window": best,
        "limitations": [
            "This is based on MOTH event detections, not continuous RF power sampling.",
            "It supports RF planning only; it does not authorise or guarantee UAS launch safety.",
        ],
    }


def _moth_adv_candidate_filter(rows, candidate_id=None, radius_m=1500):
    if candidate_id in (None, ""):
        return rows, None
    conn = connect()
    try:
        site = conn.execute("SELECT * FROM candidate_sites WHERE site_id = ?", (int(candidate_id),)).fetchone()
    finally:
        conn.close()
    if site is None:
        raise _MothHTTPException(status_code=404, detail="Candidate not found")
    site = dict(site)
    # Avoid importing geo at module load; use local haversine if project helper is unavailable.
    def hav(lat1, lon1, lat2, lon2):
        r = 6371000.0
        p1 = _moth_math.radians(float(lat1)); p2 = _moth_math.radians(float(lat2))
        dp = _moth_math.radians(float(lat2) - float(lat1)); dl = _moth_math.radians(float(lon2) - float(lon1))
        a = _moth_math.sin(dp/2)**2 + _moth_math.cos(p1)*_moth_math.cos(p2)*_moth_math.sin(dl/2)**2
        return 2*r*_moth_math.atan2(_moth_math.sqrt(a), _moth_math.sqrt(1-a))
    filtered = []
    for row in rows:
        if row.get("lat") is None or row.get("lon") is None:
            continue
        if hav(site["lat"], site["lon"], row["lat"], row["lon"]) <= float(radius_m):
            filtered.append(row)
    return filtered, site




@app.get("/api/advanced/health")
def moth_advanced_health() -> dict[str, _MothAny]:
    return {"status": "ok", "advanced_version": _MOTH_ADVANCED_VERSION, "page": "/static/launch_analysis.html?v=075"}


@app.get("/api/advanced/gnss-timeline")
def moth_advanced_gnss_timeline(
    collection_id: int | None = None,
    collection_ids: str | None = None,
    start_utc: str | None = None,
    end_utc: str | None = None,
    bin_minutes: int = _MothQuery(default=10, ge=1, le=240),
    width_mhz: float = _MothQuery(default=40.0, ge=1.0, le=100.0),
    candidate_id: int | None = None,
    radius_m: float = _MothQuery(default=1500.0, ge=50.0, le=10000.0),
    min_dbm: float | None = None,
    max_rows: int = _MothQuery(default=160000, ge=1000, le=500000),
) -> dict[str, _MothAny]:
    """L1/L2/L5 binned timeline.

    Treat event density/strong detections in GNSS windows as RF activity/interference pressure,
    not as a direct GNSS receiver-quality measurement.
    """
    edges = _moth_adv_gnss_edges(width_mhz)
    min_hz = min(v[0] for v in edges.values())
    max_hz = max(v[1] for v in edges.values())
    rows, ids = _moth_adv_query_events(
        collection_id=collection_id, collection_ids=collection_ids, start_utc=start_utc, end_utc=end_utc,
        min_hz=min_hz, max_hz=max_hz, min_dbm=min_dbm, max_rows=max_rows,
    )
    rows, candidate = _moth_adv_candidate_filter(rows, candidate_id, radius_m)
    bucket_seconds = int(bin_minutes) * 60
    buckets = _moth_dd(lambda: {"L1": [], "L2": [], "L5": [], "other": []})
    first_dt = None; last_dt = None
    for row in rows:
        dt = _moth_adv_parse_dt(row.get("timestamp_utc"))
        if not dt:
            continue
        first_dt = dt if first_dt is None or dt < first_dt else first_dt
        last_dt = dt if last_dt is None or dt > last_dt else last_dt
        bucket = _moth_adv_iso(_moth_adv_bucket_dt(dt, bucket_seconds))
        f = float(row["frequency_hz"])
        assigned = False
        for name, (lo, hi, _centre) in edges.items():
            if lo <= f <= hi:
                buckets[bucket][name].append(float(row["strength_dbm"]))
                assigned = True
                break
        if not assigned:
            buckets[bucket]["other"].append(float(row["strength_dbm"]))
    points = []
    for bucket in sorted(buckets.keys()):
        item = {"bucket_start_utc": bucket}
        for name in ["L1", "L2", "L5"]:
            st = _moth_adv_stats(buckets[bucket][name])
            item[f"{name}_event_count"] = st["count"]
            item[f"{name}_avg_dbm"] = st["avg"]
            item[f"{name}_max_dbm"] = st["max"]
            item[f"{name}_min_dbm"] = st["min"]
        item["total_gnss_events"] = sum(item[f"{name}_event_count"] for name in ["L1", "L2", "L5"])
        points.append(item)
    return {
        "version": _MOTH_ADVANCED_VERSION,
        "selected_collection_ids": ids,
        "candidate": candidate,
        "radius_m": radius_m if candidate else None,
        "width_mhz": width_mhz,
        "bucket_seconds": bucket_seconds,
        "first_timestamp_utc": _moth_adv_iso(first_dt) if first_dt else None,
        "last_timestamp_utc": _moth_adv_iso(last_dt) if last_dt else None,
        "bands": {name: {"min_hz": lo, "max_hz": hi, "center_hz": centre, "note": _MOTH_GNSS_BANDS[name]["note"]} for name, (lo, hi, centre) in edges.items()},
        "points": points,
        "limitations": [
            "MOTH data is event-based. Low event count suggests less detected RF activity, but does not prove a clean channel.",
            "GNSS L1/L2/L5 activity here is RF survey evidence, not a direct navigation-quality or flight-safety guarantee.",
        ],
    }


@app.get("/api/advanced/spectrum-analysis")
def moth_advanced_spectrum_analysis(
    collection_id: int | None = None,
    collection_ids: str | None = None,
    start_utc: str | None = None,
    end_utc: str | None = None,
    min_hz: float = _MothQuery(default=1_000_000_000.0),
    max_hz: float = _MothQuery(default=1_650_000_000.0),
    freq_bin_mhz: float = _MothQuery(default=10.0, ge=0.5, le=100.0),
    time_bin_minutes: int = _MothQuery(default=60, ge=1, le=1440),
    spike_dbm: float = _MothQuery(default=-60.0),
    max_rows: int = _MothQuery(default=160000, ge=1000, le=500000),
) -> dict[str, _MothAny]:
    rows, ids = _moth_adv_query_events(collection_id=collection_id, collection_ids=collection_ids, start_utc=start_utc, end_utc=end_utc, min_hz=min_hz, max_hz=max_hz, max_rows=max_rows)
    fbin_hz = float(freq_bin_mhz) * 1_000_000.0
    tbin_s = int(time_bin_minutes) * 60
    cells = _moth_dd(list)
    time_buckets = _moth_dd(list)
    freq_buckets = _moth_dd(list)
    hourly = _moth_dd(list)
    for row in rows:
        dt = _moth_adv_parse_dt(row.get("timestamp_utc"))
        if not dt:
            continue
        f = float(row["frequency_hz"]); strength = float(row["strength_dbm"])
        fb = _moth_math.floor(f / fbin_hz) * fbin_hz
        tb = _moth_adv_bucket_dt(dt, tbin_s)
        key = (_moth_adv_iso(tb), round(fb / 1_000_000.0, 3))
        cells[key].append(strength)
        time_buckets[_moth_adv_iso(tb)].append(strength)
        freq_buckets[round(fb / 1_000_000.0, 3)].append(strength)
        hourly[dt.hour].append(strength)
    spectrum_cells = []
    for (bucket, freq_mhz), vals in cells.items():
        st = _moth_adv_stats(vals)
        spectrum_cells.append({
            "bucket_start_utc": bucket,
            "freq_bin_mhz": freq_mhz,
            "event_count": st["count"],
            "avg_dbm": st["avg"],
            "max_dbm": st["max"],
            "spike_count": sum(1 for v in vals if v >= spike_dbm),
        })
    # Baseline anomaly score: strong max, high count, and spike events.
    counts = [c["event_count"] for c in spectrum_cells] or [1]
    max_count = max(counts) or 1
    anomalies = []
    for c in spectrum_cells:
        max_q = 0.0 if c["max_dbm"] is None else max(0.0, min(1.0, (float(c["max_dbm"]) + 90.0) / 40.0))
        count_q = c["event_count"] / max_count
        spike_q = min(1.0, c["spike_count"] / 5.0)
        score = round(100.0 * (0.45 * max_q + 0.35 * count_q + 0.20 * spike_q), 1)
        if score >= 55.0 or c["spike_count"] > 0:
            item = dict(c); item["anomaly_score_0_100"] = score
            item["interpretation"] = "abnormal spike / busy RF cell" if score >= 70 else "review"
            anomalies.append(item)
    anomalies.sort(key=lambda x: x["anomaly_score_0_100"], reverse=True)
    time_summary = []
    for bucket, vals in sorted(time_buckets.items()):
        st = _moth_adv_stats(vals)
        time_summary.append({"bucket_start_utc": bucket, "event_count": st["count"], "avg_dbm": st["avg"], "max_dbm": st["max"], "spike_count": sum(1 for v in vals if v >= spike_dbm)})
    freq_summary = []
    for fmhz, vals in sorted(freq_buckets.items()):
        st = _moth_adv_stats(vals)
        freq_summary.append({"freq_bin_mhz": fmhz, "event_count": st["count"], "avg_dbm": st["avg"], "max_dbm": st["max"], "spike_count": sum(1 for v in vals if v >= spike_dbm)})
    freq_summary.sort(key=lambda x: (x["spike_count"], x["event_count"], x["max_dbm"] or -999), reverse=True)
    patterns = []
    max_hour_count = max((len(v) for v in hourly.values()), default=1)
    for hour in range(24):
        vals = hourly.get(hour, [])
        st = _moth_adv_stats(vals)
        label = "quiet" if st["count"] <= max_hour_count * 0.20 else "busy" if st["count"] >= max_hour_count * 0.70 else "moderate"
        patterns.append({"hour_utc": hour, "event_count": st["count"], "avg_dbm": st["avg"], "max_dbm": st["max"], "pattern_label": label})
    return {
        "version": _MOTH_ADVANCED_VERSION,
        "selected_collection_ids": ids,
        "row_count": len(rows),
        "frequency_range_hz": [min_hz, max_hz],
        "freq_bin_mhz": freq_bin_mhz,
        "time_bin_minutes": time_bin_minutes,
        "spike_dbm": spike_dbm,
        "spectrum_cells": spectrum_cells[:6000],
        "time_summary": time_summary,
        "frequency_summary": freq_summary[:200],
        "anomalies": anomalies[:50],
        "patterns_of_life_hourly": patterns,
        "definition": "Spectrum analysis groups MOTH detections by frequency and time to show busy bands, strong spikes and repeating periods of activity.",
    }


@app.get("/api/advanced/launch-windows")
def moth_advanced_launch_windows(
    collection_id: int | None = None,
    collection_ids: str | None = None,
    start_utc: str | None = None,
    end_utc: str | None = None,
    duration_minutes: int = _MothQuery(default=30, ge=5, le=240),
    step_minutes: int = _MothQuery(default=10, ge=1, le=120),
    width_mhz: float = _MothQuery(default=40.0, ge=1.0, le=100.0),
    spike_dbm: float = _MothQuery(default=-60.0),
    connectivity_min_hz: float | None = None,
    connectivity_max_hz: float | None = None,
    candidate_id: int | None = None,
    radius_m: float = _MothQuery(default=1500.0, ge=50.0, le=10000.0),
    max_rows: int = _MothQuery(default=160000, ge=1000, le=500000),
) -> dict[str, _MothAny]:
    edges = _moth_adv_gnss_edges(width_mhz)
    min_hz = min([v[0] for v in edges.values()] + ([float(connectivity_min_hz)] if connectivity_min_hz else []))
    max_hz = max([v[1] for v in edges.values()] + ([float(connectivity_max_hz)] if connectivity_max_hz else []))
    rows, ids = _moth_adv_query_events(collection_id=collection_id, collection_ids=collection_ids, start_utc=start_utc, end_utc=end_utc, min_hz=min_hz, max_hz=max_hz, max_rows=max_rows)
    rows, candidate = _moth_adv_candidate_filter(rows, candidate_id, radius_m)
    dts = [(_moth_adv_parse_dt(r.get("timestamp_utc")), r) for r in rows]
    dts = [(dt, r) for dt, r in dts if dt]
    if not dts:
        return {"version": _MOTH_ADVANCED_VERSION, "selected_collection_ids": ids, "windows": [], "definition": "No matching MOTH events for current filters."}
    start_dt = _moth_adv_parse_dt(start_utc) or min(dt for dt, _r in dts)
    end_dt = _moth_adv_parse_dt(end_utc) or max(dt for dt, _r in dts)
    dur = _moth_timedelta(minutes=int(duration_minutes))
    step = _moth_timedelta(minutes=int(step_minutes))
    # Optimised sliding-window scan. The older implementation rebuilt each window
    # by scanning all rows, which was slow on the Pi for multi-day CSV sets.
    dts.sort(key=lambda item: item[0])
    windows = []
    cur = start_dt
    left = 0
    right = 0
    n = len(dts)
    while cur + dur <= end_dt + _moth_timedelta(seconds=1):
        nxt = cur + dur
        while left < n and dts[left][0] < cur:
            left += 1
        if right < left:
            right = left
        while right < n and dts[right][0] < nxt:
            right += 1
        gnss_events = []
        connectivity_events = []
        for dt, row in dts[left:right]:
            f = float(row["frequency_hz"]); strength = float(row["strength_dbm"])
            if any(lo <= f <= hi for lo, hi, _centre in edges.values()):
                gnss_events.append(strength)
            if connectivity_min_hz is not None and connectivity_max_hz is not None and float(connectivity_min_hz) <= f <= float(connectivity_max_hz):
                connectivity_events.append(strength)
        gnss_count = len(gnss_events)
        gnss_spikes = sum(1 for v in gnss_events if v >= spike_dbm)
        strong = max(gnss_events) if gnss_events else None
        avg = sum(gnss_events) / len(gnss_events) if gnss_events else None
        # Low GNSS-window event pressure is good. Direct connectivity signal can add a positive hint if supplied.
        noise_penalty = min(70.0, gnss_count * 1.0 + gnss_spikes * 8.0 + (0 if strong is None else max(0.0, strong + 75.0) * 1.2))
        conn_bonus = 0.0
        if connectivity_events:
            conn_p10 = _moth_adv_percentile(connectivity_events, 0.10)
            conn_bonus = max(0.0, min(20.0, ((conn_p10 or -110.0) + 100.0) * 0.5))
        score = max(0.0, min(100.0, 100.0 - noise_penalty + conn_bonus))
        recommendation_type, interpretation = _moth_adv_recommendation_label(score, gnss_count, gnss_spikes)
        reasons = [
            f"GNSS-window event count: {gnss_count}",
            f"Strong GNSS-window spikes â‰¥ {spike_dbm} dBm: {gnss_spikes}",
        ]
        if strong is not None:
            reasons.append(f"Strongest GNSS-window detection: {round(strong, 1)} dBm")
        else:
            reasons.append("No GNSS-window detections in this time window")
        if connectivity_events:
            reasons.append(f"Connectivity-band events present: {len(connectivity_events)}")
        elif connectivity_min_hz is not None and connectivity_max_hz is not None:
            reasons.append("No connectivity-band events observed in this window")
        item = {
            "start_utc": _moth_adv_iso(cur),
            "end_utc": _moth_adv_iso(nxt),
            "score_0_100": round(score, 1),
            "recommendation_type": recommendation_type,
            "interpretation": interpretation,
            "gnss_event_count": gnss_count,
            "gnss_spike_count": gnss_spikes,
            "gnss_avg_dbm": round(avg, 1) if avg is not None else None,
            "gnss_max_dbm": round(strong, 1) if strong is not None else None,
            "connectivity_event_count": len(connectivity_events),
            "connectivity_p10_dbm": _moth_adv_percentile(connectivity_events, 0.10),
            "reasons": reasons,
        }
        item["operator_readout"] = f"{recommendation_type}: {item['start_utc']} to {item['end_utc']} UTC. Score {item['score_0_100']}/100. " + "; ".join(reasons[:3])
        windows.append(item)
        cur += step
    windows.sort(key=lambda x: x["score_0_100"], reverse=True)
    top_windows = windows[:100]
    best = top_windows[0] if top_windows else None
    return {
        "version": _MOTH_ADVANCED_VERSION,
        "selected_collection_ids": ids,
        "candidate": candidate,
        "radius_m": radius_m if candidate else None,
        "duration_minutes": duration_minutes,
        "step_minutes": step_minutes,
        "width_mhz": width_mhz,
        "spike_dbm": spike_dbm,
        "connectivity_band_hz": [connectivity_min_hz, connectivity_max_hz] if connectivity_min_hz is not None and connectivity_max_hz is not None else None,
        "definition": "Launch windows are ranked by low detected RF activity in L1/L2/L5 windows, low strong-spike count, and optional connectivity-band evidence if supplied.",
        "recommended_window": best if best and best.get("recommendation_type") == "RECOMMENDED" else None,
        "best_viable_window": best,
        "operator_brief": _moth_adv_operator_brief(top_windows),
        "best_window_display": "recommended_window if present; otherwise best_viable_window is the top ranked available timing",
        "windows": top_windows,
        "limitations": [
            "This ranks RF cleanliness from MOTH event logs. It does not replace airspace approval, UAS safety procedures, GNSS integrity checks, or operational flight-risk assessment.",
            "A quiet MOTH window means fewer detected events, not guaranteed absence of interference.",
            "If no RECOMMENDED period appears, the top window is the best viable or least-busy timing found by the current filters.",
        ],
    }


@app.get("/api/advanced/operator-brief")
def moth_advanced_operator_brief(
    collection_id: int | None = None,
    collection_ids: str | None = None,
    start_utc: str | None = None,
    end_utc: str | None = None,
    duration_minutes: int = _MothQuery(default=30, ge=5, le=240),
    step_minutes: int = _MothQuery(default=10, ge=1, le=120),
    width_mhz: float = _MothQuery(default=40.0, ge=1.0, le=100.0),
    spike_dbm: float = _MothQuery(default=-60.0),
    connectivity_min_hz: float | None = None,
    connectivity_max_hz: float | None = None,
    candidate_id: int | None = None,
    radius_m: float = _MothQuery(default=1500.0, ge=50.0, le=10000.0),
) -> dict[str, _MothAny]:
    windows = moth_advanced_launch_windows(
        collection_id=collection_id, collection_ids=collection_ids, start_utc=start_utc, end_utc=end_utc,
        duration_minutes=duration_minutes, step_minutes=step_minutes, width_mhz=width_mhz, spike_dbm=spike_dbm,
        connectivity_min_hz=connectivity_min_hz, connectivity_max_hz=connectivity_max_hz,
        candidate_id=candidate_id, radius_m=radius_m,
    )
    brief = windows.get("operator_brief") or _moth_adv_operator_brief(windows.get("windows") or [])
    brief["advanced_version"] = _MOTH_ADVANCED_VERSION
    brief["traceable_metrics"] = {
        "duration_minutes": duration_minutes,
        "step_minutes": step_minutes,
        "width_mhz": width_mhz,
        "spike_dbm": spike_dbm,
        "connectivity_band_hz": windows.get("connectivity_band_hz"),
    }
    brief["ai_handoff"] = "The AI HAT+ 2 can turn this traceable metric bundle into richer prose. The metric score remains deterministic."
    return brief

# ---- MOTH v0.8.0 UX workflow and data-quality endpoints ----
# Additive endpoints for home screen, data quality dashboard, and briefing cards.

from datetime import datetime as _ux_datetime, timezone as _ux_timezone
from collections import defaultdict as _ux_defaultdict
import math as _ux_math
from typing import Any as _UxAny
from fastapi import Query as _UxQuery

_MOTH_UX_VERSION = "0.8.0"
_UX_GNSS_WINDOWS = {
    "L1": (1575.42e6 - 20e6, 1575.42e6 + 20e6, "GNSS L1"),
    "L2": (1227.60e6 - 20e6, 1227.60e6 + 20e6, "GNSS L2"),
    "L3": (1381.05e6 - 20e6, 1381.05e6 + 20e6, "GNSS L3 optional/legacy"),
    "L5": (1176.45e6 - 20e6, 1176.45e6 + 20e6, "GNSS L5"),
}


def _ux_parse_dt(value):
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        dt = _ux_datetime.fromisoformat(text)
        return dt.astimezone(_ux_timezone.utc) if dt.tzinfo else dt.replace(tzinfo=_ux_timezone.utc)
    except Exception:
        return None


def _ux_iso(dt):
    if not dt:
        return None
    return dt.astimezone(_ux_timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _ux_parse_ids(collection_id=None, collection_ids=None):
    ids = []
    if collection_id not in (None, ""):
        ids.append(int(collection_id))
    if collection_ids:
        for piece in str(collection_ids).replace(";", ",").split(","):
            piece = piece.strip()
            if piece:
                ids.append(int(piece))
    return sorted(set(ids))


def _ux_collection_filter(ids, where, params):
    if ids:
        placeholders = ",".join("?" for _ in ids)
        where.append(f"collection_id IN ({placeholders})")
        params.extend(ids)


def _ux_safe_pct(num, den):
    return round((float(num or 0) / float(den or 1)) * 100.0, 1)


def _ux_mhz(value):
    if value is None:
        return None
    return round(float(value) / 1_000_000.0, 4)


def _ux_freq_label(min_hz=None, max_hz=None):
    if min_hz is None and max_hz is None:
        return "All logged frequencies"
    if min_hz is not None and max_hz is not None:
        centre = (float(min_hz) + float(max_hz)) / 2.0
        for name, (lo, hi, _note) in _UX_GNSS_WINDOWS.items():
            if lo <= centre <= hi:
                return f"{name} window: {_ux_mhz(min_hz)}-{_ux_mhz(max_hz)} MHz"
        return f"{_ux_mhz(min_hz)}-{_ux_mhz(max_hz)} MHz"
    if min_hz is not None:
        return f"Above {_ux_mhz(min_hz)} MHz"
    return f"Below {_ux_mhz(max_hz)} MHz"


def _ux_quality_label(score):
    score = float(score or 0)
    if score >= 80:
        return "GOOD"
    if score >= 55:
        return "MEDIUM"
    return "LOW"


def _ux_query_data_quality(ids):
    where = ["1=1"]
    params = []
    _ux_collection_filter(ids, where, params)
    where_sql = " AND ".join(where)
    conn = connect()
    try:
        totals = dict(conn.execute(
            f"""
            SELECT
              COUNT(*) AS total_events,
              SUM(CASE WHEN valid = 1 THEN 1 ELSE 0 END) AS valid_events,
              SUM(CASE WHEN valid = 0 THEN 1 ELSE 0 END) AS invalid_events,
              SUM(CASE WHEN lat IS NULL OR lon IS NULL THEN 1 ELSE 0 END) AS missing_gps_events,
              SUM(CASE WHEN lat = 0 AND lon = 0 THEN 1 ELSE 0 END) AS zero_zero_events,
              SUM(CASE WHEN timestamp_utc IS NULL OR timestamp_utc = '' THEN 1 ELSE 0 END) AS missing_time_events,
              COUNT(DISTINCT collection_id) AS collection_count,
              MIN(timestamp_utc) AS first_timestamp_utc,
              MAX(timestamp_utc) AS last_timestamp_utc,
              MIN(frequency_hz) AS min_frequency_hz,
              MAX(frequency_hz) AS max_frequency_hz,
              MIN(strength_dbm) AS min_dbm,
              MAX(strength_dbm) AS max_dbm,
              ROUND(AVG(strength_dbm), 1) AS avg_dbm
            FROM moth_events
            WHERE {where_sql}
            """,
            params,
        ).fetchone())

        collections = rows_to_dicts(conn.execute(
            f"""
            SELECT
              c.collection_id,
              COALESCE(c.collection_name, 'Collection ' || c.collection_id) AS collection_name,
              COUNT(e.event_id) AS total_events,
              SUM(CASE WHEN e.valid = 1 THEN 1 ELSE 0 END) AS valid_events,
              SUM(CASE WHEN e.valid = 0 THEN 1 ELSE 0 END) AS invalid_events,
              MIN(e.timestamp_utc) AS first_timestamp_utc,
              MAX(e.timestamp_utc) AS last_timestamp_utc,
              MIN(e.frequency_hz) AS min_frequency_hz,
              MAX(e.frequency_hz) AS max_frequency_hz,
              MIN(e.strength_dbm) AS min_dbm,
              MAX(e.strength_dbm) AS max_dbm,
              ROUND(AVG(e.strength_dbm), 1) AS avg_dbm
            FROM moth_collections c
            LEFT JOIN moth_events e ON e.collection_id = c.collection_id
            WHERE {('c.collection_id IN (' + ','.join('?' for _ in ids) + ')') if ids else '1=1'}
            GROUP BY c.collection_id, c.collection_name
            ORDER BY c.collection_id DESC
            """,
            ids,
        ).fetchall())

        # Frequency coverage for GNSS windows.
        coverage = []
        for name, (lo, hi, note) in _UX_GNSS_WINDOWS.items():
            w = ["valid = 1", "frequency_hz BETWEEN ? AND ?"]
            p = [lo, hi]
            _ux_collection_filter(ids, w, p)
            row = dict(conn.execute(
                "SELECT COUNT(*) AS event_count, ROUND(AVG(strength_dbm), 1) AS avg_dbm, MIN(strength_dbm) AS min_dbm, MAX(strength_dbm) AS max_dbm FROM moth_events WHERE " + " AND ".join(w),
                p,
            ).fetchone())
            row.update({"band": name, "window_mhz": f"{_ux_mhz(lo)}-{_ux_mhz(hi)}", "meaning": note})
            coverage.append(row)

        dupes = []
        try:
            dupes = rows_to_dicts(conn.execute(
                """
                SELECT file_hash, COUNT(*) AS count, GROUP_CONCAT(collection_id) AS collection_ids
                FROM moth_collections
                WHERE file_hash IS NOT NULL AND file_hash != ''
                GROUP BY file_hash
                HAVING COUNT(*) > 1
                ORDER BY COUNT(*) DESC
                """
            ).fetchall())
        except Exception:
            dupes = []

        time_rows = rows_to_dicts(conn.execute(
            f"SELECT timestamp_utc FROM moth_events WHERE valid = 1 AND timestamp_utc IS NOT NULL AND timestamp_utc != '' {'AND collection_id IN (' + ','.join('?' for _ in ids) + ')' if ids else ''} ORDER BY timestamp_utc LIMIT 500000",
            ids,
        ).fetchall())
    finally:
        conn.close()

    # Gap analysis in Python to avoid SQLite date-format fragility.
    parsed = [_ux_parse_dt(r.get("timestamp_utc")) for r in time_rows]
    parsed = [d for d in parsed if d]
    gaps = []
    for a, b in zip(parsed, parsed[1:]):
        gap_min = (b - a).total_seconds() / 60.0
        if gap_min >= 30.0:
            gaps.append({"start_utc": _ux_iso(a), "end_utc": _ux_iso(b), "gap_minutes": round(gap_min, 1)})
    gaps = sorted(gaps, key=lambda g: g["gap_minutes"], reverse=True)[:10]

    total = int(totals.get("total_events") or 0)
    valid = int(totals.get("valid_events") or 0)
    missing_gps = int(totals.get("missing_gps_events") or 0) + int(totals.get("zero_zero_events") or 0)
    score = 100.0
    reasons = []
    recommendations = []
    if total == 0:
        score = 0.0
        reasons.append("No MOTH events are loaded for the selected scans.")
        recommendations.append("Upload or select MOTH CSV scans before drawing conclusions.")
    else:
        invalid_pct = 100.0 - _ux_safe_pct(valid, total)
        if invalid_pct > 10:
            score -= min(30.0, invalid_pct)
            reasons.append(f"{invalid_pct:.1f}% of rows are invalid or excluded.")
            recommendations.append("Use cleaned/AOI-filtered CSVs and check parser notes.")
        gps_pct = _ux_safe_pct(missing_gps, total)
        if gps_pct > 5:
            score -= min(25.0, gps_pct)
            reasons.append(f"{gps_pct:.1f}% of rows have missing or unusable GPS coordinates.")
            recommendations.append("Avoid map conclusions where GPS quality is weak.")
        if int(totals.get("collection_count") or 0) < 2:
            score -= 10
            reasons.append("Only one scan/collection is represented.")
            recommendations.append("Collect/import multiple comparable scans where possible.")
        if gaps:
            score -= min(20.0, len(gaps) * 4.0)
            reasons.append(f"{len(gaps)} time gaps of at least 30 minutes were found.")
            recommendations.append("Check whether the gaps matter for the timing or candidate decision.")
        if dupes:
            score -= 10
            reasons.append("Possible duplicate collection file hashes were detected.")
            recommendations.append("Remove duplicate imports if density or event counts look inflated.")
        if valid < 500:
            score -= 15
            reasons.append("The selected data set is sparse.")
            recommendations.append("Collect more MOTH data before relying on rankings.")

    score = max(0.0, min(100.0, round(score, 1)))
    level = _ux_quality_label(score)
    if not reasons:
        reasons.append("Loaded data is broadly usable for first-pass analysis.")
    if not recommendations:
        recommendations.append("Proceed with analysis, then validate preferred decisions with controlled follow-up collection.")

    headline = {
        "GOOD": "Data quality is good enough for first-pass decision support.",
        "MEDIUM": "Data quality is usable but needs caveats.",
        "LOW": "Data quality is weak; treat outputs as exploratory only.",
    }[level]

    return {
        "ux_version": _MOTH_UX_VERSION,
        "score_0_100": score,
        "quality_level": level,
        "headline": headline,
        "reasons": reasons,
        "recommendations": recommendations,
        "totals": totals,
        "valid_percent": _ux_safe_pct(valid, total),
        "collections": collections,
        "gnss_frequency_coverage": coverage,
        "time_gaps": gaps,
        "duplicate_file_hashes": dupes,
        "limitations": [
            "MOTH data is event-based, not continuous calibrated spectrum recording.",
            "Quiet periods mean fewer MOTH detections, not guaranteed absence of RF energy.",
            "Comparable decisions need comparable scan settings, thresholds, antennas and collection geometry.",
        ],
    }


@app.get("/api/ux/health")
def moth_ux_health() -> dict[str, _UxAny]:
    return {
        "status": "ok",
        "ux_version": _MOTH_UX_VERSION,
        "home_page": "/static/home.html?v=080",
        "data_quality_page": "/static/data_quality.html?v=080",
        "briefing_page": "/static/briefing.html?v=080",
    }


@app.get("/api/ux/data-quality")
def moth_ux_data_quality(collection_id: int | None = None, collection_ids: str | None = None) -> dict[str, _UxAny]:
    ids = _ux_parse_ids(collection_id, collection_ids)
    return _ux_query_data_quality(ids)


@app.get("/api/ux/decision-cards")
def moth_ux_decision_cards(
    collection_id: int | None = None,
    collection_ids: str | None = None,
    target_min_hz: float | None = None,
    target_max_hz: float | None = None,
) -> dict[str, _UxAny]:
    ids = _ux_parse_ids(collection_id, collection_ids)
    data_quality = _ux_query_data_quality(ids)

    candidate_card = {
        "status": "NO CANDIDATE",
        "headline": "No candidate scoring available yet.",
        "reason": "Add candidate antenna sites and score them in the main dashboard.",
        "candidate": None,
    }
    scorer = globals().get("score_candidate_sites")
    if scorer:
        try:
            candidates = scorer(target_min_hz=target_min_hz, target_max_hz=target_max_hz, collection_ids=ids)
            if candidates:
                top = candidates[0]
                score = float(top.get("score_0_100") or 0)
                status = "GOOD" if score >= 75 else "CHECK" if score >= 50 else "POOR"
                candidate_card = {
                    "status": status,
                    "headline": f"Top candidate: {top.get('name')} ({score:.1f}/100)",
                    "reason": f"Target events: {top.get('target_event_count')}; lower-tail: {top.get('target_strength_p10_dbm')} dBm; confidence: {top.get('data_confidence')}.",
                    "candidate": top,
                }
        except Exception as exc:
            candidate_card = {"status": "ERROR", "headline": "Candidate scoring could not run.", "reason": str(exc), "candidate": None}

    launch_card = {
        "status": "NOT RUN",
        "headline": "Launch timing has not been assessed here.",
        "reason": "Use the launch dashboard for recommended or best viable timing.",
        "best_window": None,
    }
    launch_fn = globals().get("moth_advanced_launch_windows")
    if launch_fn:
        try:
            launch = launch_fn(collection_ids=collection_ids, duration_minutes=30, step_minutes=30, width_mhz=40.0, spike_dbm=-60.0, max_rows=160000)
            windows = launch.get("windows") or []
            if windows:
                best = windows[0]
                status = best.get("recommendation_type") or "RANKED"
                launch_card = {
                    "status": status,
                    "headline": f"{status}: {best.get('start_utc')} to {best.get('end_utc')} UTC",
                    "reason": f"Score {best.get('score_0_100')}/100; GNSS events {best.get('gnss_event_count')}; strong GNSS spikes {best.get('gnss_spike_count')}.",
                    "best_window": best,
                }
            else:
                launch_card = {"status": "NO DATA", "headline": "No launch windows could be ranked.", "reason": launch.get("definition") or "No matching events.", "best_window": None}
        except Exception as exc:
            launch_card = {"status": "ERROR", "headline": "Launch timing ranking could not run.", "reason": str(exc), "best_window": None}

    return {
        "ux_version": _MOTH_UX_VERSION,
        "filter_summary": [
            "Scans: " + (", ".join(str(i) for i in ids) if ids else "all loaded scans"),
            "Frequency: " + _ux_freq_label(target_min_hz, target_max_hz),
        ],
        "data_quality": {
            "status": data_quality["quality_level"],
            "score_0_100": data_quality["score_0_100"],
            "headline": data_quality["headline"],
            "reason": "; ".join(data_quality["reasons"][:3]),
        },
        "candidate": candidate_card,
        "launch": launch_card,
        "next_actions": [
            "Use Briefing Mode for simple decision cards.",
            "Use Analyst Mode for detailed map, graph and spectrum checks.",
            "Use Data Quality before presenting conclusions.",
        ],
    }


# ---- MOTH v0.9.1 RF Mission Briefing Wizard ----
# Appended by moth_v091_mission_fallback_patch_20260513.
# Additive only: keeps existing dashboards intact and adds a guided mission brief workflow.

from datetime import datetime as _mission_datetime, timezone as _mission_timezone, timedelta as _mission_timedelta
from typing import Any as _MissionAny
import html as _mission_html
from fastapi import Query as _MissionQuery
from fastapi.responses import HTMLResponse as _MissionHTMLResponse

_MOTH_MISSION_VERSION = "0.9.1"


def _mission_now() -> str:
    return _mission_datetime.now(_mission_timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _mission_parse_ids(collection_id=None, collection_ids=None):
    # Prefer the advanced helper when available, because it handles same semantics as launch analysis.
    try:
        return _moth_adv_parse_ids(collection_id, collection_ids)  # type: ignore[name-defined]
    except Exception:
        ids = []
        if collection_id not in (None, ""):
            ids.append(int(collection_id))
        if collection_ids:
            for piece in str(collection_ids).replace(";", ",").split(","):
                piece = piece.strip()
                if piece:
                    ids.append(int(piece))
        return sorted(set(ids))


def _mission_call_data_quality():
    try:
        return ux_data_quality()  # type: ignore[name-defined]
    except Exception:
        conn = connect()
        try:
            events = dict(conn.execute("""
                SELECT COUNT(*) AS total_events,
                       SUM(CASE WHEN valid = 1 THEN 1 ELSE 0 END) AS valid_events,
                       MIN(timestamp_utc) AS first_timestamp_utc,
                       MAX(timestamp_utc) AS last_timestamp_utc,
                       MIN(frequency_hz) AS min_frequency_hz,
                       MAX(frequency_hz) AS max_frequency_hz
                FROM moth_events
            """).fetchone())
            collections = 0
            try:
                collections = int(conn.execute("SELECT COUNT(*) FROM moth_collections").fetchone()[0] or 0)
            except Exception:
                pass
            candidates = 0
            try:
                candidates = int(conn.execute("SELECT COUNT(*) FROM candidate_sites").fetchone()[0] or 0)
            except Exception:
                pass
        finally:
            conn.close()
        total = int(events.get("total_events") or 0)
        valid = int(events.get("valid_events") or 0)
        valid_pct = (valid / total * 100.0) if total else 0.0
        grade = "HIGH" if valid_pct >= 90 and collections >= 3 else "MEDIUM" if valid_pct >= 65 and collections >= 1 else "LOW"
        return {
            "grade": grade,
            "score_0_100": round(valid_pct, 1),
            "summary": f"Data quality {grade}: {valid} valid events across {collections} scan(s).",
            "totals": events,
            "collections": collections,
            "candidate_count": candidates,
            "checks": [],
            "recommendations": ["Use the data quality dashboard for full checks."],
        }


def _mission_parse_dt(value):
    if not value:
        return None
    try:
        return _mission_datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def _mission_iso(dt):
    return dt.astimezone(_mission_timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _mission_all_rf_fallback_windows(*, collection_id=None, collection_ids=None, start_utc=None, end_utc=None, duration_minutes=30, step_minutes=10, spike_dbm=-60.0, max_rows=500000, **_unused):
    ids = _mission_parse_ids(collection_id, collection_ids)
    where = ["valid = 1", "timestamp_utc IS NOT NULL", "strength_dbm IS NOT NULL"]
    params = []
    if ids:
        placeholders = ",".join("?" for _ in ids)
        where.append(f"collection_id IN ({placeholders})")
        params.extend(ids)
    if start_utc:
        where.append("timestamp_utc >= ?")
        params.append(start_utc)
    if end_utc:
        where.append("timestamp_utc <= ?")
        params.append(end_utc)
    conn = connect()
    try:
        rows = rows_to_dicts(conn.execute(
            "SELECT timestamp_utc, strength_dbm FROM moth_events WHERE " + " AND ".join(where) + " ORDER BY timestamp_utc LIMIT ?",
            params + [int(max_rows)],
        ).fetchall())
    finally:
        conn.close()
    points = []
    for r in rows:
        dt = _mission_parse_dt(r.get("timestamp_utc"))
        if dt is None or r.get("strength_dbm") is None:
            continue
        points.append((dt, float(r["strength_dbm"])))
    if not points:
        return {
            "version": _MOTH_MISSION_VERSION,
            "selected_collection_ids": ids,
            "windows": [],
            "definition": "Fallback launch timing could not run because no valid timestamped RF events matched the selected filters.",
            "fallback_used": True,
            "fallback_reason": "No timestamped MOTH events available for all-RF fallback.",
        }
    points.sort(key=lambda x: x[0])
    start_dt = _mission_parse_dt(start_utc) or points[0][0]
    end_dt = _mission_parse_dt(end_utc) or points[-1][0]
    dur = _mission_timedelta(minutes=int(duration_minutes))
    step = _mission_timedelta(minutes=int(step_minutes))
    if end_dt - start_dt < dur:
        dur = max(_mission_timedelta(minutes=5), end_dt - start_dt)
    windows = []
    cur = start_dt
    left = 0
    right = 0
    n = len(points)
    while cur + dur <= end_dt + _mission_timedelta(seconds=1):
        nxt = cur + dur
        while left < n and points[left][0] < cur:
            left += 1
        if right < left:
            right = left
        while right < n and points[right][0] < nxt:
            right += 1
        vals = [v for _dt, v in points[left:right]]
        if vals:
            count = len(vals)
            spikes = sum(1 for v in vals if v >= float(spike_dbm))
            max_dbm = max(vals)
            avg_dbm = sum(vals) / len(vals)
        else:
            count = 0; spikes = 0; max_dbm = None; avg_dbm = None
        windows.append({
            "start_utc": _mission_iso(cur),
            "end_utc": _mission_iso(nxt),
            "all_rf_event_count": count,
            "all_rf_spike_count": spikes,
            "all_rf_max_dbm": round(max_dbm, 1) if max_dbm is not None else None,
            "all_rf_avg_dbm": round(avg_dbm, 1) if avg_dbm is not None else None,
        })
        cur += step
    if not windows:
        return {"version": _MOTH_MISSION_VERSION, "selected_collection_ids": ids, "windows": [], "fallback_used": True, "fallback_reason": "No complete time windows could be formed."}
    max_count = max(w["all_rf_event_count"] for w in windows) or 1
    max_spikes = max(w["all_rf_spike_count"] for w in windows) or 1
    for w in windows:
        count_penalty = 55.0 * (w["all_rf_event_count"] / max_count)
        spike_penalty = 30.0 * (w["all_rf_spike_count"] / max_spikes) if max_spikes else 0.0
        strength_penalty = 0.0 if w["all_rf_max_dbm"] is None else max(0.0, (float(w["all_rf_max_dbm"]) + 75.0) * 0.6)
        score = max(0.0, min(100.0, 100.0 - count_penalty - spike_penalty - strength_penalty))
        w["score_0_100"] = round(score, 1)
        if score >= 70:
            rec = "BEST VIABLE"
            interp = "No GNSS-specific timing was ranked, but this is the cleanest all-RF fallback window in the selected data."
        elif score >= 45:
            rec = "LEAST-BUSY OBSERVED"
            interp = "This is not clean; it is only the least-busy observed all-RF timing from the selected data."
        else:
            rec = "AVOID IF POSSIBLE"
            interp = "All observed windows are RF-busy or spike-prone. Use only if operationally necessary and validated."
        w.update({
            "recommendation_type": rec,
            "interpretation": interp,
            "gnss_event_count": None,
            "gnss_spike_count": None,
            "connectivity_event_count": None,
            "fallback_basis": "all RF detections",
            "operator_readout": f"{rec}: {w['start_utc']} to {w['end_utc']} UTC. Score {w['score_0_100']}/100. All-RF events: {w['all_rf_event_count']}; spikes: {w['all_rf_spike_count']}.",
            "reasons": [
                "GNSS-specific launch windows could not be ranked from the current filters.",
                f"Fallback all-RF event count: {w['all_rf_event_count']}",
                f"Fallback all-RF spike count â‰¥ {spike_dbm} dBm: {w['all_rf_spike_count']}",
            ],
        })
    windows.sort(key=lambda x: x["score_0_100"], reverse=True)
    best = windows[0]
    return {
        "version": _MOTH_MISSION_VERSION,
        "selected_collection_ids": ids,
        "windows": windows[:100],
        "recommended_window": best if best.get("recommendation_type") == "BEST VIABLE" else None,
        "best_viable_window": best,
        "fallback_used": True,
        "fallback_reason": "No GNSS-specific launch windows were ranked; using all-RF least-busy timing fallback.",
        "definition": "Fallback launch timing ranks time windows by lower all-RF event count, fewer strong all-RF spikes and lower maximum dBm. It is not a clean GNSS-window recommendation.",
        "operator_brief": {
            "headline": f"Fallback timing: {best.get('recommendation_type')} {best.get('start_utc')} to {best.get('end_utc')} UTC",
            "decision_status": best.get("recommendation_type"),
            "recommendation": best.get("interpretation"),
            "rationale": best.get("reasons"),
        },
        "limitations": [
            "Fallback timing is based on all RF detections, not proven GNSS-window cleanliness.",
            "Use this only as best available timing when the GNSS-specific launch analysis has no ranked result.",
        ],
    }


def _mission_call_launch_windows(**kwargs):
    try:
        result = moth_advanced_launch_windows(**kwargs)  # type: ignore[name-defined]
        if result.get("windows"):
            return result
        fallback = _mission_all_rf_fallback_windows(**kwargs)
        fallback["advanced_result_without_windows"] = {k: v for k, v in result.items() if k != "windows"}
        return fallback
    except Exception as exc:
        fallback = _mission_all_rf_fallback_windows(**kwargs)
        if fallback.get("windows"):
            fallback["advanced_error"] = str(exc)
            return fallback
        return {
            "version": getattr(globals().get("_MOTH_ADVANCED_VERSION", None), "__str__", lambda: "not available")(),
            "windows": [],
            "operator_brief": {
                "headline": "Launch-window analysis is not available or returned no result.",
                "recommendation": "Check that the launch RF dashboard patch is installed and that matching MOTH data exists.",
                "rationale": [str(exc)] + (fallback.get("limitations") or []),
                "decision_status": "NO DATA",
            },
            "limitations": ["Launch-window analysis endpoint could not be executed."] + (fallback.get("limitations") or []),
        }


def _mission_call_candidate_scores(*, collection_ids=None, target_min_hz=None, target_max_hz=None, radius_m=1500.0, start_utc=None, end_utc=None):
    try:
        # score_candidate_sites is imported by the main app in recent baselines.
        return score_candidate_sites(  # type: ignore[name-defined]
            radius_m=radius_m,
            target_min_hz=target_min_hz,
            target_max_hz=target_max_hz,
            collection_ids=collection_ids or [],
            start_utc=start_utc,
            end_utc=end_utc,
        )
    except Exception:
        try:
            return score_candidates(  # type: ignore[name-defined]
                radius_m=radius_m,
                target_min_hz=target_min_hz,
                target_max_hz=target_max_hz,
                collection_ids=",".join(str(x) for x in (collection_ids or [])) if collection_ids else None,
            )
        except Exception:
            conn = connect()
            try:
                rows = rows_to_dicts(conn.execute("SELECT * FROM candidate_sites ORDER BY name LIMIT 20").fetchall())
            except Exception:
                rows = []
            finally:
                conn.close()
            return [{"site_id": r.get("site_id"), "name": r.get("name"), "score_0_100": None, "data_confidence": 0, "note": "Scoring endpoint unavailable."} for r in rows]


def _mission_readiness_grade(data_quality, launch_windows, candidates):
    dq_grade = str(data_quality.get("grade") or "LOW").upper()
    candidate_count = int(data_quality.get("candidate_count") or 0)
    windows = launch_windows.get("windows") or []
    best = windows[0] if windows else None
    checks = []
    def add(name, status, reason, action):
        checks.append({"name": name, "status": status, "reason": reason, "action": action})
    add("Data quality", "good" if dq_grade == "HIGH" else "check" if dq_grade == "MEDIUM" else "poor", data_quality.get("summary") or "No data quality summary.", "Open Data Quality if this is CHECK or POOR.")
    add("Candidate sites", "good" if candidate_count >= 3 else "check" if candidate_count >= 1 else "poor", f"{candidate_count} candidate site(s) loaded.", "Add at least three candidate sites for fair comparison.")
    if best:
        label = str(best.get("recommendation_type") or "NO DATA")
        add("Launch timing", "good" if label == "RECOMMENDED" else "check" if label in ("BEST VIABLE", "LEAST-BUSY OBSERVED", "QUIET BUT UNPROVEN") else "poor", f"Top launch-window result: {label}.", "Use RECOMMENDED or BEST VIABLE with validation; avoid POOR/NO DATA.")
    else:
        add("Launch timing", "poor", "No launch-window timing could be ranked from current filters.", "Broaden filters, load scans, or check launch analysis page.")
    scored = [c for c in candidates if c.get("score_0_100") is not None]
    if scored:
        best_c = scored[0]
        conf = float(best_c.get("data_confidence") or 0)
        add("Antenna candidate evidence", "good" if conf >= 0.65 else "check" if conf >= 0.25 else "poor", f"Top candidate: {best_c.get('name')} score {best_c.get('score_0_100')}/100, confidence {best_c.get('data_confidence')}.", "Use candidate assessment and graph before briefing.")
    else:
        add("Antenna candidate evidence", "check", "No scored candidate was available.", "Score candidates or add candidate sites.")
    poor = sum(1 for c in checks if c["status"] == "poor")
    check = sum(1 for c in checks if c["status"] == "check")
    if poor:
        grade = "POOR"
    elif check >= 2:
        grade = "CHECK"
    else:
        grade = "GOOD"
    return grade, checks


def _mission_short_operator_brief(*, readiness, checks, launch, candidate, data_quality):
    windows = launch.get("windows") or []
    best_window = windows[0] if windows else None
    candidate_name = candidate.get("name") if candidate else "No candidate selected"
    if best_window:
        launch_line = f"{best_window.get('recommendation_type')}: {best_window.get('start_utc')} to {best_window.get('end_utc')} UTC, score {best_window.get('score_0_100')}/100."
    else:
        launch_line = "No viable launch timing could be ranked from the selected data."
    headline = f"RF Readiness {readiness}: {launch_line}"
    reasons = []
    for c in checks:
        if c.get("status") != "good":
            reasons.append(f"{c.get('name')}: {c.get('reason')}")
    if not reasons:
        reasons = ["Data quality, candidate evidence and launch-window ranking are all acceptable for first-pass briefing."]
    return {
        "headline": headline,
        "candidate_line": f"Antenna candidate focus: {candidate_name}.",
        "rationale": reasons[:5],
        "limitations": [
            "MOTH data is event-based; a quiet period means fewer detections, not guaranteed absence of interference.",
            "This is RF planning support only and does not authorise UAS launch or replace operational safety checks.",
            "Final launch timing still requires airspace approval, weather, aircraft, battery, crew and link/GNSS checks.",
        ],
    }


@app.get("/api/mission/health")
def mission_health() -> dict[str, _MissionAny]:
    return {
        "status": "ok",
        "mission_version": _MOTH_MISSION_VERSION,
        "page": "/static/mission_brief.html?v=091",
        "data_quality_endpoint": "/api/mission/readiness",
        "brief_endpoint": "/api/mission/brief",
    }


@app.get("/api/mission/readiness")
def mission_readiness(
    collection_id: int | None = None,
    collection_ids: str | None = None,
    target_min_hz: float | None = None,
    target_max_hz: float | None = None,
    start_utc: str | None = None,
    end_utc: str | None = None,
    radius_m: float = _MissionQuery(default=1500.0, ge=50.0, le=10000.0),
    duration_minutes: int = _MissionQuery(default=30, ge=5, le=240),
    step_minutes: int = _MissionQuery(default=10, ge=1, le=120),
    width_mhz: float = _MissionQuery(default=40.0, ge=1.0, le=100.0),
    spike_dbm: float = _MissionQuery(default=-60.0),
    connectivity_min_hz: float | None = None,
    connectivity_max_hz: float | None = None,
    max_rows: int = _MissionQuery(default=500000, ge=1000, le=1000000),
) -> dict[str, _MissionAny]:
    ids = _mission_parse_ids(collection_id, collection_ids)
    dq = _mission_call_data_quality()
    launch = _mission_call_launch_windows(
        collection_id=collection_id,
        collection_ids=collection_ids,
        start_utc=start_utc,
        end_utc=end_utc,
        duration_minutes=duration_minutes,
        step_minutes=step_minutes,
        width_mhz=width_mhz,
        spike_dbm=spike_dbm,
        connectivity_min_hz=connectivity_min_hz,
        connectivity_max_hz=connectivity_max_hz,
        radius_m=radius_m,
        max_rows=max_rows,
    )
    candidates = _mission_call_candidate_scores(collection_ids=ids, target_min_hz=target_min_hz, target_max_hz=target_max_hz, radius_m=radius_m, start_utc=start_utc, end_utc=end_utc)
    candidates = sorted(candidates, key=lambda c: (c.get("score_0_100") is not None, float(c.get("score_0_100") or -1)), reverse=True)[:10]
    grade, checks = _mission_readiness_grade(dq, launch, candidates)
    return {
        "mission_version": _MOTH_MISSION_VERSION,
        "generated_utc": _mission_now(),
        "readiness": grade,
        "checks": checks,
        "data_quality": dq,
        "top_candidates": candidates[:3],
        "best_launch_window": (launch.get("windows") or [None])[0],
        "selected_collection_ids": ids,
        "summary": f"RF Readiness {grade}: {len([c for c in checks if c['status']=='good'])} good, {len([c for c in checks if c['status']=='check'])} check, {len([c for c in checks if c['status']=='poor'])} poor.",
    }


@app.get("/api/mission/brief")
def mission_brief(
    collection_id: int | None = None,
    collection_ids: str | None = None,
    target_min_hz: float | None = None,
    target_max_hz: float | None = None,
    start_utc: str | None = None,
    end_utc: str | None = None,
    radius_m: float = _MissionQuery(default=1500.0, ge=50.0, le=10000.0),
    duration_minutes: int = _MissionQuery(default=30, ge=5, le=240),
    step_minutes: int = _MissionQuery(default=10, ge=1, le=120),
    width_mhz: float = _MissionQuery(default=40.0, ge=1.0, le=100.0),
    spike_dbm: float = _MissionQuery(default=-60.0),
    connectivity_min_hz: float | None = None,
    connectivity_max_hz: float | None = None,
    max_rows: int = _MissionQuery(default=500000, ge=1000, le=1000000),
) -> dict[str, _MissionAny]:
    readiness = mission_readiness(
        collection_id=collection_id,
        collection_ids=collection_ids,
        target_min_hz=target_min_hz,
        target_max_hz=target_max_hz,
        start_utc=start_utc,
        end_utc=end_utc,
        radius_m=radius_m,
        duration_minutes=duration_minutes,
        step_minutes=step_minutes,
        width_mhz=width_mhz,
        spike_dbm=spike_dbm,
        connectivity_min_hz=connectivity_min_hz,
        connectivity_max_hz=connectivity_max_hz,
        max_rows=max_rows,
    )
    launch = {"windows": [readiness.get("best_launch_window")] if readiness.get("best_launch_window") else []}
    candidate = (readiness.get("top_candidates") or [None])[0]
    op = _mission_short_operator_brief(readiness=readiness.get("readiness"), checks=readiness.get("checks") or [], launch=launch, candidate=candidate, data_quality=readiness.get("data_quality") or {})
    return {
        "mission_version": _MOTH_MISSION_VERSION,
        "generated_utc": _mission_now(),
        "readiness": readiness,
        "operator_brief": op,
        "report_url": "/api/mission/report.html",
        "ai_handoff": {
            "status": "ready_for_ai_summary",
            "instruction": "AI HAT+ 2 should summarize these traceable metrics; it should not invent or override the deterministic score.",
            "input_bundle_keys": ["readiness", "operator_brief"],
        },
    }


@app.get("/api/ai/operator-brief")
def ai_operator_brief(
    collection_id: int | None = None,
    collection_ids: str | None = None,
    target_min_hz: float | None = None,
    target_max_hz: float | None = None,
    start_utc: str | None = None,
    end_utc: str | None = None,
    radius_m: float = _MissionQuery(default=1500.0, ge=50.0, le=10000.0),
) -> dict[str, _MissionAny]:
    brief = mission_brief(collection_id=collection_id, collection_ids=collection_ids, target_min_hz=target_min_hz, target_max_hz=target_max_hz, start_utc=start_utc, end_utc=end_utc, radius_m=radius_m)
    op = brief.get("operator_brief") or {}
    return {
        "status": "deterministic_text_generated",
        "note": "Local AI summarization is not enabled in this endpoint yet. This text is generated from traceable metrics and is suitable as the AI HAT+ 2 input/output target.",
        "brief_text": "\n".join([op.get("headline", ""), op.get("candidate_line", ""), "Why:"] + [f"- {x}" for x in op.get("rationale", [])] + ["Cautions:"] + [f"- {x}" for x in op.get("limitations", [])]),
        "source_metrics": brief,
    }


def _mission_escape(v):
    return _mission_html.escape("" if v is None else str(v))


@app.get("/api/mission/report.html", response_class=_MissionHTMLResponse)
def mission_report_html(
    collection_id: int | None = None,
    collection_ids: str | None = None,
    target_min_hz: float | None = None,
    target_max_hz: float | None = None,
    start_utc: str | None = None,
    end_utc: str | None = None,
    radius_m: float = _MissionQuery(default=1500.0, ge=50.0, le=10000.0),
    duration_minutes: int = _MissionQuery(default=30, ge=5, le=240),
    step_minutes: int = _MissionQuery(default=10, ge=1, le=120),
    width_mhz: float = _MissionQuery(default=40.0, ge=1.0, le=100.0),
    spike_dbm: float = _MissionQuery(default=-60.0),
    connectivity_min_hz: float | None = None,
    connectivity_max_hz: float | None = None,
):
    data = mission_brief(collection_id=collection_id, collection_ids=collection_ids, target_min_hz=target_min_hz, target_max_hz=target_max_hz, start_utc=start_utc, end_utc=end_utc, radius_m=radius_m, duration_minutes=duration_minutes, step_minutes=step_minutes, width_mhz=width_mhz, spike_dbm=spike_dbm, connectivity_min_hz=connectivity_min_hz, connectivity_max_hz=connectivity_max_hz)
    r = data.get("readiness") or {}
    op = data.get("operator_brief") or {}
    top = r.get("top_candidates") or []
    checks = r.get("checks") or []
    win = r.get("best_launch_window") or {}
    def li(items):
        return "".join(f"<li>{_mission_escape(x)}</li>" for x in items)
    html = f"""
<!doctype html><html><head><meta charset='utf-8'><title>MOTH RF Mission Brief</title>
<style>
body{{font-family:Arial,sans-serif;max-width:900px;margin:24px auto;color:#111}} h1{{color:#0b4f7a}} .card{{border:1px solid #bbb;border-radius:8px;padding:12px;margin:12px 0}} .status{{font-size:24px;font-weight:700}} .good{{color:#167a32}} .check{{color:#b7791f}} .poor{{color:#b42318}} table{{width:100%;border-collapse:collapse}}td,th{{border:1px solid #ccc;padding:6px;text-align:left}} .small{{color:#666;font-size:12px}} @media print{{button{{display:none}}body{{margin:0}}}}
</style></head><body>
<button onclick="window.print()">Print / save as PDF</button>
<h1>MOTH RF Mission Brief</h1>
<p class='small'>Generated UTC: {_mission_escape(data.get('generated_utc'))} | Version {_MOTH_MISSION_VERSION}</p>
<div class='card'><div class='status {_mission_escape(str(r.get('readiness','')).lower())}'>RF Readiness: {_mission_escape(r.get('readiness'))}</div><p>{_mission_escape(r.get('summary'))}</p></div>
<div class='card'><h2>Operator Brief</h2><p><b>{_mission_escape(op.get('headline'))}</b></p><p>{_mission_escape(op.get('candidate_line'))}</p><h3>Why</h3><ul>{li(op.get('rationale') or [])}</ul></div>
<div class='card'><h2>Best Launch Window</h2><table><tr><th>Category</th><td>{_mission_escape(win.get('recommendation_type'))}</td></tr><tr><th>UTC timing</th><td>{_mission_escape(win.get('start_utc'))} to {_mission_escape(win.get('end_utc'))}</td></tr><tr><th>Score</th><td>{_mission_escape(win.get('score_0_100'))}/100</td></tr><tr><th>GNSS events/spikes</th><td>{_mission_escape(win.get('gnss_event_count'))} / {_mission_escape(win.get('gnss_spike_count'))}</td></tr></table></div>
<div class='card'><h2>Top Antenna Candidates</h2><table><tr><th>Rank</th><th>Name</th><th>Score</th><th>Confidence</th></tr>{''.join(f"<tr><td>{_mission_escape(c.get('rank'))}</td><td>{_mission_escape(c.get('name'))}</td><td>{_mission_escape(c.get('score_0_100'))}</td><td>{_mission_escape(c.get('data_confidence'))}</td></tr>" for c in top)}</table></div>
<div class='card'><h2>RF Readiness Checks</h2><table><tr><th>Check</th><th>Status</th><th>Reason</th><th>Action</th></tr>{''.join(f"<tr><td>{_mission_escape(c.get('name'))}</td><td>{_mission_escape(c.get('status'))}</td><td>{_mission_escape(c.get('reason'))}</td><td>{_mission_escape(c.get('action'))}</td></tr>" for c in checks)}</table></div>
<div class='card'><h2>Limitations</h2><ul>{li(op.get('limitations') or [])}</ul></div>
</body></html>
"""
    return _MissionHTMLResponse(html)

# ---- MOTH v0.9.2 time sanity, pattern-of-life and JSP 101 report extensions ----
# Appended by moth_v092_time_pol_jsp_patch_20260513.

from datetime import datetime as _v092_datetime, timezone as _v092_timezone, timedelta as _v092_timedelta
from typing import Any as _v092_Any
import html as _v092_html
from fastapi import Query as _v092_Query
from fastapi.responses import HTMLResponse as _v092_HTMLResponse

try:
    _MOTH_MISSION_VERSION = "0.9.2"  # type: ignore[assignment]
except Exception:
    pass

_V092_VERSION = "0.9.2"
_V092_BAD_PLACE_NAMES = ("Mogadishu", "Somalia")
_V092_GNSS_BANDS = {
    "L1": (1555.42e6, 1595.42e6),
    "L2": (1207.60e6, 1247.60e6),
    "L3": (1361.05e6, 1401.05e6),
    "L5": (1156.45e6, 1196.45e6),
}


def _v092_now() -> str:
    return _v092_datetime.now(_v092_timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _v092_parse_ids(collection_id=None, collection_ids=None) -> list[int]:
    try:
        return _mission_parse_ids(collection_id, collection_ids)  # type: ignore[name-defined]
    except Exception:
        ids: list[int] = []
        if collection_id not in (None, ""):
            ids.append(int(collection_id))
        if collection_ids:
            for piece in str(collection_ids).replace(";", ",").split(","):
                piece = piece.strip()
                if piece:
                    ids.append(int(piece))
        return sorted(set(ids))


def _v092_parse_dt(value):
    if not value:
        return None
    try:
        return _v092_datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(_v092_timezone.utc)
    except Exception:
        return None


def _v092_iso(dt) -> str | None:
    if not dt:
        return None
    return dt.astimezone(_v092_timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _v092_sanitize_text(text) -> str:
    out = "" if text is None else str(text)
    for bad in _V092_BAD_PLACE_NAMES:
        out = out.replace(bad, "AOI")
    return out


def _v092_event_where(*, collection_id=None, collection_ids=None, target_min_hz=None, target_max_hz=None, start_utc=None, end_utc=None, valid_only=True):
    where = []
    params: list[_v092_Any] = []
    if valid_only:
        where.append("valid = 1")
    ids = _v092_parse_ids(collection_id, collection_ids)
    if ids:
        placeholders = ",".join("?" for _ in ids)
        where.append(f"collection_id IN ({placeholders})")
        params.extend(ids)
    if target_min_hz is not None:
        where.append("frequency_hz >= ?")
        params.append(float(target_min_hz))
    if target_max_hz is not None:
        where.append("frequency_hz <= ?")
        params.append(float(target_max_hz))
    if start_utc:
        where.append("timestamp_utc >= ?")
        params.append(str(start_utc))
    if end_utc:
        where.append("timestamp_utc <= ?")
        params.append(str(end_utc))
    return where or ["1=1"], params, ids


def _v092_fetch_events(*, collection_id=None, collection_ids=None, target_min_hz=None, target_max_hz=None, start_utc=None, end_utc=None, max_rows=500000, valid_only=True):
    where, params, ids = _v092_event_where(
        collection_id=collection_id,
        collection_ids=collection_ids,
        target_min_hz=target_min_hz,
        target_max_hz=target_max_hz,
        start_utc=start_utc,
        end_utc=end_utc,
        valid_only=valid_only,
    )
    conn = connect()  # type: ignore[name-defined]
    try:
        rows = rows_to_dicts(conn.execute(  # type: ignore[name-defined]
            "SELECT collection_id, timestamp_utc, frequency_hz, strength_dbm, lat, lon FROM moth_events WHERE " + " AND ".join(where) + " ORDER BY timestamp_utc LIMIT ?",
            params + [int(max_rows)],
        ).fetchall())
    finally:
        conn.close()
    return rows, ids


def _v092_year(dt):
    return dt.year if dt else None


def _v092_is_time_sane(dt, sane_start=None, sane_end=None):
    if dt is None:
        return False
    if sane_start and dt < sane_start:
        return False
    if sane_end and dt > sane_end:
        return False
    # Broad sanity filter for MOTH/GPS resets. User range can narrow further.
    return 2020 <= dt.year <= 2035


def _v092_gnss_label(freq_hz):
    if freq_hz is None:
        return None
    f = float(freq_hz)
    for label, (lo, hi) in _V092_GNSS_BANDS.items():
        if lo <= f <= hi:
            return label
    return None


def _v092_period_key(dt, period: str) -> str:
    if period == "month":
        return dt.strftime("%Y-%m")
    if period == "week":
        iso = dt.isocalendar()
        return f"{iso.year}-W{iso.week:02d}"
    return dt.strftime("%Y-%m-%d")


def _v092_period_label(period: str) -> str:
    return {"day": "Day-on-day", "week": "Week-on-week", "month": "Month-on-month"}.get(period, "Day-on-day")


def _v092_jsp_para(items: list[str]) -> str:
    return "".join(f"<p>{i + 1}. {_v092_html.escape(_v092_sanitize_text(txt))}</p>" for i, txt in enumerate(items))


@app.get("/api/mission/time-sanity")
def mission_time_sanity(
    collection_id: int | None = None,
    collection_ids: str | None = None,
    sane_start_utc: str | None = None,
    sane_end_utc: str | None = None,
    max_rows: int = _v092_Query(default=750000, ge=1000, le=2000000),
) -> dict[str, _v092_Any]:
    rows, ids = _v092_fetch_events(collection_id=collection_id, collection_ids=collection_ids, max_rows=max_rows, valid_only=False)
    user_start = _v092_parse_dt(sane_start_utc)
    user_end = _v092_parse_dt(sane_end_utc)
    total = len(rows)
    no_time = 0
    bad_time = 0
    sane: list[_v092_datetime] = []
    years: dict[str, int] = {}
    for row in rows:
        dt = _v092_parse_dt(row.get("timestamp_utc"))
        if dt is None:
            no_time += 1
            continue
        years[str(dt.year)] = years.get(str(dt.year), 0) + 1
        if _v092_is_time_sane(dt, user_start, user_end):
            sane.append(dt)
        else:
            bad_time += 1
    sane.sort()
    recommended_start = _v092_iso(sane[0]) if sane else None
    recommended_end = _v092_iso(sane[-1]) if sane else None
    sane_count = len(sane)
    confidence = "GOOD" if sane_count >= 100 and bad_time / max(total, 1) < 0.10 else "CHECK" if sane_count else "POOR"
    return {
        "status": "ok",
        "version": _V092_VERSION,
        "selected_collection_ids": ids,
        "total_rows_checked": total,
        "sane_timestamp_rows": sane_count,
        "bad_or_out_of_range_timestamp_rows": bad_time,
        "missing_or_unparseable_timestamp_rows": no_time,
        "confidence": confidence,
        "recommended_start_utc": recommended_start,
        "recommended_end_utc": recommended_end,
        "year_distribution": sorted(({"year": k, "rows": v} for k, v in years.items()), key=lambda x: x["year"]),
        "plain_summary": f"{sane_count} usable timestamped rows found. {bad_time + no_time} rows appear missing, unparseable or outside the sane time range.",
        "operator_action": "Use the recommended UTC range for mission analysis if MOTH/GPS time resets appear in the data.",
    }


@app.get("/api/mission/pattern-of-life")
def mission_pattern_of_life(
    period: str = _v092_Query(default="day", pattern="^(day|week|month)$"),
    collection_id: int | None = None,
    collection_ids: str | None = None,
    target_min_hz: float | None = None,
    target_max_hz: float | None = None,
    start_utc: str | None = None,
    end_utc: str | None = None,
    sane_start_utc: str | None = None,
    sane_end_utc: str | None = None,
    spike_dbm: float = -60.0,
    max_rows: int = _v092_Query(default=750000, ge=1000, le=2000000),
) -> dict[str, _v092_Any]:
    # Use start/end as active analysis filters; sane_start/end can further constrain GPS-time quality.
    rows, ids = _v092_fetch_events(
        collection_id=collection_id,
        collection_ids=collection_ids,
        target_min_hz=target_min_hz,
        target_max_hz=target_max_hz,
        start_utc=start_utc,
        end_utc=end_utc,
        max_rows=max_rows,
        valid_only=True,
    )
    sane_start = _v092_parse_dt(sane_start_utc) or _v092_parse_dt(start_utc)
    sane_end = _v092_parse_dt(sane_end_utc) or _v092_parse_dt(end_utc)
    grouped: dict[str, dict[str, _v092_Any]] = {}
    bad_time = 0
    for row in rows:
        dt = _v092_parse_dt(row.get("timestamp_utc"))
        if not _v092_is_time_sane(dt, sane_start, sane_end):
            bad_time += 1
            continue
        key = _v092_period_key(dt, period)
        strength = float(row.get("strength_dbm") or -999.0)
        freq = row.get("frequency_hz")
        g = grouped.setdefault(key, {"period": key, "event_count": 0, "strength_sum": 0.0, "max_dbm": strength, "min_dbm": strength, "spike_count": 0, "gnss_event_count": 0, "l1_count": 0, "l2_count": 0, "l3_count": 0, "l5_count": 0})
        g["event_count"] += 1
        g["strength_sum"] += strength
        g["max_dbm"] = max(float(g["max_dbm"]), strength)
        g["min_dbm"] = min(float(g["min_dbm"]), strength)
        if strength >= float(spike_dbm):
            g["spike_count"] += 1
        label = _v092_gnss_label(freq)
        if label:
            g["gnss_event_count"] += 1
            g[f"{label.lower()}_count"] += 1
    rows_out = []
    max_events = max((int(g["event_count"]) for g in grouped.values()), default=1)
    max_spikes = max((int(g["spike_count"]) for g in grouped.values()), default=1)
    for g in grouped.values():
        count = max(int(g["event_count"]), 1)
        avg = float(g["strength_sum"]) / count
        event_penalty = 45.0 * (count / max_events)
        spike_penalty = 35.0 * (int(g["spike_count"]) / max_spikes if max_spikes else 0)
        strength_penalty = max(0.0, (float(g["max_dbm"]) + 75.0) * 0.6)
        score = max(0.0, min(100.0, 100.0 - event_penalty - spike_penalty - strength_penalty))
        status = "QUIETER" if score >= 70 else "CHECK" if score >= 45 else "BUSY"
        rows_out.append({
            "period": g["period"],
            "period_type": period,
            "event_count": int(g["event_count"]),
            "avg_dbm": round(avg, 1),
            "max_dbm": round(float(g["max_dbm"]), 1),
            "min_dbm": round(float(g["min_dbm"]), 1),
            "spike_count": int(g["spike_count"]),
            "gnss_event_count": int(g["gnss_event_count"]),
            "l1_count": int(g["l1_count"]),
            "l2_count": int(g["l2_count"]),
            "l3_count": int(g["l3_count"]),
            "l5_count": int(g["l5_count"]),
            "cleanliness_score_0_100": round(score, 1),
            "status": status,
        })
    rows_out.sort(key=lambda r: (r["cleanliness_score_0_100"], -r["event_count"]), reverse=True)
    best = rows_out[:5]
    worst = sorted(rows_out, key=lambda r: (r["cleanliness_score_0_100"], -r["event_count"]))[:5]
    return {
        "status": "ok",
        "version": _V092_VERSION,
        "selected_collection_ids": ids,
        "analysis_type": _v092_period_label(period),
        "period": period,
        "rows_used": sum(r["event_count"] for r in rows_out),
        "bad_or_out_of_range_time_rows_excluded": bad_time,
        "periods": sorted(rows_out, key=lambda r: r["period"]),
        "quietest_periods": best,
        "busiest_periods": worst,
        "definition": "Pattern of life groups MOTH detections by day, week or month to reveal recurring busy and quiet RF periods. It is an observed-pattern tool, not proof of guaranteed future RF conditions.",
        "operator_action": "Use quiet periods to plan collection and launch-window review, then validate against current data before acting.",
    }


def _v092_report_table(rows: list[dict[str, _v092_Any]], cols: list[tuple[str, str]], limit: int = 10) -> str:
    head = "".join(f"<th>{_v092_html.escape(label)}</th>" for _key, label in cols)
    body = ""
    for r in rows[:limit]:
        body += "<tr>" + "".join(f"<td>{_v092_html.escape(_v092_sanitize_text(r.get(key)))}</td>" for key, _label in cols) + "</tr>"
    return f"<table><tr>{head}</tr>{body}</table>"


@app.get("/api/mission/report-jsp101.html", response_class=_v092_HTMLResponse)
def mission_jsp101_report_html(
    collection_id: int | None = None,
    collection_ids: str | None = None,
    target_min_hz: float | None = None,
    target_max_hz: float | None = None,
    start_utc: str | None = None,
    end_utc: str | None = None,
    radius_m: float = _v092_Query(default=1500.0, ge=50.0, le=10000.0),
    duration_minutes: int = _v092_Query(default=30, ge=5, le=240),
    step_minutes: int = _v092_Query(default=10, ge=1, le=120),
    width_mhz: float = _v092_Query(default=40.0, ge=1.0, le=100.0),
    spike_dbm: float = _v092_Query(default=-60.0),
    period: str = _v092_Query(default="day", pattern="^(day|week|month)$"),
    max_rows: int = _v092_Query(default=500000, ge=1000, le=2000000),
):
    try:
        brief = mission_brief(collection_id=collection_id, collection_ids=collection_ids, target_min_hz=target_min_hz, target_max_hz=target_max_hz, start_utc=start_utc, end_utc=end_utc, radius_m=radius_m, duration_minutes=duration_minutes, step_minutes=step_minutes, width_mhz=width_mhz, spike_dbm=spike_dbm, max_rows=max_rows)  # type: ignore[name-defined]
    except Exception as exc:
        brief = {"operator_brief": {"headline": "Mission brief endpoint unavailable", "rationale": [str(exc)], "limitations": ["Check API installation."]}, "readiness": {}}
    patterns = mission_pattern_of_life(period=period, collection_id=collection_id, collection_ids=collection_ids, target_min_hz=target_min_hz, target_max_hz=target_max_hz, start_utc=start_utc, end_utc=end_utc, spike_dbm=spike_dbm, max_rows=max_rows)
    sanity = mission_time_sanity(collection_id=collection_id, collection_ids=collection_ids, sane_start_utc=start_utc, sane_end_utc=end_utc, max_rows=max_rows)
    op = brief.get("operator_brief") or {}
    readiness = brief.get("readiness") or {}
    r = readiness if isinstance(readiness, dict) else {}
    top_candidate = ((r.get("top_candidates") or [None])[0]) if isinstance(r, dict) else None
    launch = r.get("best_launch_window") or {}
    findings = [
        op.get("headline") or "No mission headline available.",
        op.get("candidate_line") or "No candidate focus line available.",
        f"Timestamp sanity: {sanity.get('plain_summary')}",
        f"Pattern-of-life basis: {patterns.get('analysis_type')} with {len(patterns.get('periods') or [])} analysed period(s).",
    ]
    actions = [
        "Use the recommended or best-viable timing only as RF planning support.",
        "If the result is least-busy observed, brief it as constrained rather than clean.",
        "Confirm airspace, weather, aircraft, GNSS and link checks before launch.",
        "If timestamp sanity is CHECK or POOR, set a narrower UTC range and rerun the report.",
    ]
    cols = [("period", "Period"), ("status", "Status"), ("cleanliness_score_0_100", "Score"), ("event_count", "Events"), ("spike_count", "Spikes"), ("gnss_event_count", "GNSS events"), ("max_dbm", "Max dBm")]
    pattern_table = _v092_report_table(patterns.get("quietest_periods") or [], cols, limit=5)
    candidate_html = "No candidate available."
    if top_candidate:
        candidate_html = f"{_v092_html.escape(_v092_sanitize_text(top_candidate.get('name')))}; score {_v092_html.escape(str(top_candidate.get('score_0_100')))}; confidence {_v092_html.escape(str(top_candidate.get('data_confidence')))}."
    launch_html = f"{_v092_html.escape(_v092_sanitize_text(launch.get('recommendation_type')))}; {_v092_html.escape(_v092_sanitize_text(launch.get('start_utc')))} to {_v092_html.escape(_v092_sanitize_text(launch.get('end_utc')))} UTC; score {_v092_html.escape(str(launch.get('score_0_100')))}."
    html = f"""
<!doctype html><html><head><meta charset='utf-8'><title>RF Mission Brief</title>
<style>
@page{{size:A4;margin:18mm}} body{{font-family:Arial,Helvetica,sans-serif;color:#111;margin:0 auto;max-width:920px;line-height:1.35}} h1{{font-size:22px;color:#17365d;border-bottom:2px solid #17365d;padding-bottom:6px}} h2{{font-size:16px;color:#17365d;margin-top:18px}} h3{{font-size:13px;color:#111}} table{{width:100%;border-collapse:collapse;margin:8px 0}} th{{background:#17365d;color:#fff;text-align:left}} td,th{{border:1px solid #bbb;padding:6px;font-size:12px}} p,li{{font-size:12.5px}} .meta{{font-size:11px;color:#555}} .box{{border:1px solid #999;padding:10px;margin:8px 0}} .warn{{border-left:5px solid #b42318}} button{{margin:8px 0;padding:8px 12px}} @media print{{button{{display:none}} body{{max-width:none}}}}
</style></head><body>
<button onclick='window.print()'>Print / save as PDF</button>
<h1>RF Mission Brief</h1>
<p class='meta'>Generated UTC: {_v092_html.escape(_v092_now())} | App mission extension {_V092_VERSION} | Place naming suppressed: AOI/grid/lat-long only.</p>
<div class='box'><h2>1. Purpose</h2><p>1. This brief summarises observed MOTH RF data to support antenna-placement and UAS RF launch-timing planning. It does not authorise flight or guarantee RF, GNSS or link performance.</p></div>
<div class='box'><h2>2. Executive summary</h2>{_v092_jsp_para(findings)}</div>
<div class='box'><h2>3. Assessment</h2><table><tr><th>Item</th><th>Assessment</th></tr><tr><td>RF readiness</td><td>{_v092_html.escape(_v092_sanitize_text(r.get('readiness')))}</td></tr><tr><td>Launch timing</td><td>{launch_html}</td></tr><tr><td>Antenna candidate</td><td>{candidate_html}</td></tr><tr><td>Timestamp sanity</td><td>{_v092_html.escape(_v092_sanitize_text(sanity.get('confidence')))}</td></tr></table></div>
<div class='box'><h2>4. Pattern of life</h2><p>1. The following table shows the quietest observed {_v092_html.escape(period)} period(s) in the selected data.</p>{pattern_table}</div>
<div class='box'><h2>5. Actions and recommendations</h2>{_v092_jsp_para(actions)}</div>
<div class='box warn'><h2>6. Caveats</h2><ul><li>MOTH data is event-based. A quiet period means fewer detections, not guaranteed absence of interference.</li><li>GPS-derived timestamps can be wrong by years; check timestamp sanity before using day/week/month patterns.</li><li>Outputs are decision-support evidence only and require normal operational approval and checks.</li></ul></div>
</body></html>
"""
    return _v092_HTMLResponse(html)

# ---- EEI LANTERN v0.10.0 Flight Safety / GNSS Clearance layer ----
# Additive endpoints: standalone briefing page, constellation-aware GNSS L-band
# clearance scoring, RF-burden GeoJSON, and simple report hooks.

from datetime import datetime as _lantern_datetime, timezone as _lantern_timezone
from typing import Any as _LanternAny
from collections import defaultdict as _lantern_defaultdict
import math as _lantern_math

from fastapi import Query as _LanternQuery
from fastapi.responses import HTMLResponse as _LanternHTMLResponse

_LANTERN_VERSION = "0.10.0"

_LANTERN_STYLE_NOTE = (
    "RF burden layer colour logic: green = quieter/clearer observed, "
    "yellow/amber = validate, orange = busy/caution, red = avoid if possible. "
    "This is intentionally the reverse of a heat/intensity map."
)

# Constellation-aware catalogue used for ranking and display.
# Ranges are practical planning/display windows in MHz, not a certification of receiver masks.
_LANTERN_GNSS_BANDS: list[dict[str, _LanternAny]] = [
    {"id": "gps_l5", "constellation": "GPS", "signal": "L5", "center_mhz": 1176.45, "min_mhz": 1164.0, "max_mhz": 1189.0, "family": "GPS", "layer": "lower"},
    {"id": "galileo_e5a", "constellation": "Galileo", "signal": "E5a", "center_mhz": 1176.45, "min_mhz": 1164.0, "max_mhz": 1189.0, "family": "Galileo", "layer": "lower"},
    {"id": "galileo_e5b", "constellation": "Galileo", "signal": "E5b", "center_mhz": 1207.14, "min_mhz": 1189.0, "max_mhz": 1214.0, "family": "Galileo", "layer": "lower"},
    {"id": "beidou_b2", "constellation": "BeiDou", "signal": "B2", "center_mhz": 1207.14, "min_mhz": 1189.0, "max_mhz": 1214.0, "family": "BeiDou", "layer": "lower"},
    {"id": "glonass_g3", "constellation": "GLONASS", "signal": "G3", "center_mhz": 1202.025, "min_mhz": 1198.0, "max_mhz": 1215.0, "family": "GLONASS", "layer": "lower"},
    {"id": "gps_l2", "constellation": "GPS", "signal": "L2", "center_mhz": 1227.60, "min_mhz": 1215.0, "max_mhz": 1240.0, "family": "GPS", "layer": "lower"},
    {"id": "glonass_g2", "constellation": "GLONASS", "signal": "G2", "center_mhz": 1246.0, "min_mhz": 1237.0, "max_mhz": 1254.0, "family": "GLONASS", "layer": "lower"},
    {"id": "galileo_e6", "constellation": "Galileo", "signal": "E6", "center_mhz": 1278.75, "min_mhz": 1260.0, "max_mhz": 1300.0, "family": "Galileo", "layer": "lower"},
    {"id": "beidou_b3", "constellation": "BeiDou", "signal": "B3", "center_mhz": 1268.52, "min_mhz": 1260.0, "max_mhz": 1300.0, "family": "BeiDou", "layer": "lower"},
    {"id": "galileo_sar", "constellation": "Galileo", "signal": "SAR", "center_mhz": 1544.5, "min_mhz": 1544.0, "max_mhz": 1545.0, "family": "Galileo SAR", "layer": "upper"},
    {"id": "beidou_b1", "constellation": "BeiDou", "signal": "B1", "center_mhz": 1561.098, "min_mhz": 1559.0, "max_mhz": 1563.0, "family": "BeiDou", "layer": "upper"},
    {"id": "gps_l1", "constellation": "GPS", "signal": "L1", "center_mhz": 1575.42, "min_mhz": 1563.0, "max_mhz": 1587.0, "family": "GPS", "layer": "upper"},
    {"id": "galileo_e1", "constellation": "Galileo", "signal": "E1", "center_mhz": 1575.42, "min_mhz": 1559.0, "max_mhz": 1591.0, "family": "Galileo", "layer": "upper"},
    {"id": "beidou_b1_2", "constellation": "BeiDou", "signal": "B1-2", "center_mhz": 1589.742, "min_mhz": 1587.0, "max_mhz": 1591.0, "family": "BeiDou", "layer": "upper"},
    {"id": "glonass_g1", "constellation": "GLONASS", "signal": "G1", "center_mhz": 1602.0, "min_mhz": 1593.0, "max_mhz": 1610.0, "family": "GLONASS", "layer": "upper"},
]

_LANTERN_SERVICE_BLOCKS: list[dict[str, _LanternAny]] = [
    {"id": "lower_arns", "label": "ARNS", "min_mhz": 1164.0, "max_mhz": 1214.0, "layer": "lower", "meaning": "Aviation Radio Navigation Service"},
    {"id": "lower_rnss_1", "label": "RNSS", "min_mhz": 1164.0, "max_mhz": 1215.0, "layer": "lower", "meaning": "Radio Navigation Satellite Service"},
    {"id": "lower_rnss_2", "label": "RNSS", "min_mhz": 1215.0, "max_mhz": 1260.0, "layer": "lower", "meaning": "Radio Navigation Satellite Service"},
    {"id": "lower_rnss_3", "label": "RNSS", "min_mhz": 1260.0, "max_mhz": 1300.0, "layer": "lower", "meaning": "Radio Navigation Satellite Service"},
    {"id": "sar", "label": "SAR", "min_mhz": 1544.0, "max_mhz": 1545.0, "layer": "upper", "meaning": "Search and Rescue downlink area"},
    {"id": "upper_arns", "label": "ARNS", "min_mhz": 1559.0, "max_mhz": 1610.0, "layer": "upper", "meaning": "Aviation Radio Navigation Service"},
    {"id": "upper_rnss", "label": "RNSS", "min_mhz": 1559.0, "max_mhz": 1610.0, "layer": "upper", "meaning": "Radio Navigation Satellite Service"},
]


def _lantern_iso(dt: _lantern_datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_lantern_timezone.utc)
    return dt.astimezone(_lantern_timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _lantern_parse_dt(value: _LanternAny) -> _lantern_datetime | None:
    if not value:
        return None
    try:
        text = str(value).strip().replace("Z", "+00:00")
        dt = _lantern_datetime.fromisoformat(text)
        return dt.astimezone(_lantern_timezone.utc) if dt.tzinfo else dt.replace(tzinfo=_lantern_timezone.utc)
    except Exception:
        return None


def _lantern_clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    try:
        return max(lo, min(hi, float(value)))
    except Exception:
        return lo


def _lantern_parse_ids(collection_id=None, collection_ids=None) -> list[int]:
    try:
        return parse_collection_ids(collection_id=collection_id, collection_ids=collection_ids)  # type: ignore[name-defined]
    except Exception:
        ids: set[int] = set()
        if collection_id not in (None, ""):
            ids.add(int(collection_id))
        if collection_ids:
            for part in str(collection_ids).replace(";", ",").split(","):
                part = part.strip()
                if part:
                    ids.add(int(part))
        return sorted(ids)


def _lantern_band_label(band: dict[str, _LanternAny]) -> str:
    return f"{band.get('constellation')} {band.get('signal')}".strip()


def _lantern_query_lband_events(
    *,
    collection_id=None,
    collection_ids=None,
    start_utc=None,
    end_utc=None,
    min_mhz: float = 1100.0,
    max_mhz: float = 1650.0,
    min_dbm=None,
    max_rows: int = 500000,
) -> tuple[list[dict[str, _LanternAny]], list[int]]:
    ids = _lantern_parse_ids(collection_id=collection_id, collection_ids=collection_ids)
    where = ["valid = 1", "frequency_hz IS NOT NULL", "strength_dbm IS NOT NULL"]
    params: list[_LanternAny] = []
    if ids:
        placeholders = ",".join("?" for _ in ids)
        where.append(f"collection_id IN ({placeholders})")
        params.extend(ids)
    where.append("frequency_hz >= ?")
    params.append(float(min_mhz) * 1_000_000.0)
    where.append("frequency_hz <= ?")
    params.append(float(max_mhz) * 1_000_000.0)
    if start_utc:
        where.append("timestamp_utc >= ?")
        params.append(str(start_utc))
    if end_utc:
        where.append("timestamp_utc <= ?")
        params.append(str(end_utc))
    if min_dbm is not None:
        where.append("strength_dbm >= ?")
        params.append(float(min_dbm))
    params.append(int(max_rows))
    conn = connect()  # type: ignore[name-defined]
    try:
        rows = rows_to_dicts(conn.execute(  # type: ignore[name-defined]
            "SELECT event_id, collection_id, timestamp_utc, frequency_hz, strength_dbm, lat, lon, h3_r8, h3_r9, h3_r10 "
            "FROM moth_events WHERE " + " AND ".join(where) + " ORDER BY timestamp_utc ASC LIMIT ?",
            params,
        ).fetchall())
    finally:
        conn.close()
    return rows, ids


def _lantern_strength_quality(max_dbm: float | None) -> float:
    # -110 dBm and below = weak contribution to burden; -55 dBm and stronger = high burden.
    if max_dbm is None:
        return 0.0
    return _lantern_clamp((float(max_dbm) + 110.0) / 55.0)


def _lantern_burden_score(event_count: int, spike_count: int, max_dbm: float | None, max_event_count: int, max_spike_count: int) -> float:
    count_q = _lantern_clamp(float(event_count) / max(float(max_event_count or 1), 1.0))
    spike_q = _lantern_clamp(float(spike_count) / max(float(max_spike_count or 1), 1.0)) if max_spike_count else 0.0
    strength_q = _lantern_strength_quality(max_dbm)
    score = 100.0 * (0.45 * count_q + 0.35 * spike_q + 0.20 * strength_q)
    return round(_lantern_clamp(score, 0.0, 100.0), 1)


def _lantern_clearance_status(clearance_score: float, event_count: int, total_lband_events: int) -> str:
    if total_lband_events <= 0:
        return "NO DATA"
    if event_count <= 0:
        return "QUIET BY MOTH - VERIFY"
    if clearance_score >= 80.0:
        return "LOW RF BURDEN"
    if clearance_score >= 60.0:
        return "CHECK"
    if clearance_score >= 40.0:
        return "BUSY"
    return "AVOID IF POSSIBLE"


def _lantern_display_colour(clearance_score: float, event_count: int, total_lband_events: int) -> str:
    if total_lband_events <= 0:
        return "#808080"  # no data
    if clearance_score >= 80.0:
        return "#1f7a3a"  # green = clear/quiet observed
    if clearance_score >= 60.0:
        return "#d4a017"  # amber = validate
    if clearance_score >= 40.0:
        return "#d46a1f"  # orange = busy
    return "#b42318"      # red = avoid


def _lantern_overlap_band_labels(min_mhz: float, max_mhz: float) -> list[str]:
    overlaps: list[str] = []
    for band in _LANTERN_GNSS_BANDS:
        if float(band["max_mhz"]) >= float(min_mhz) and float(band["min_mhz"]) <= float(max_mhz):
            overlaps.append(_lantern_band_label(band))
    return overlaps


def _lantern_confidence(total_events: int, collection_count: int, first_dt: _lantern_datetime | None, last_dt: _lantern_datetime | None) -> dict[str, _LanternAny]:
    duration_hours = None
    if first_dt and last_dt and last_dt >= first_dt:
        duration_hours = round((last_dt - first_dt).total_seconds() / 3600.0, 2)
    score = 0.0
    reasons: list[str] = []
    if total_events <= 0:
        reasons.append("No L-band events matched the current filter.")
    else:
        score += min(45.0, total_events / 10.0)
        if total_events >= 500:
            reasons.append("High L-band evidence volume under the current filters.")
        elif total_events >= 50:
            reasons.append("Moderate L-band evidence volume under the current filters.")
        else:
            reasons.append("Sparse L-band evidence under the current filters.")
    if collection_count >= 3:
        score += 35.0
        reasons.append("Multiple scans contribute to this view.")
    elif collection_count == 2:
        score += 25.0
        reasons.append("Two scans contribute to this view.")
    elif collection_count == 1:
        score += 10.0
        reasons.append("Only one scan contributes to this view.")
    if duration_hours is not None:
        if duration_hours >= 6:
            score += 20.0
            reasons.append("The selected data spans several hours.")
        elif duration_hours >= 1:
            score += 12.0
            reasons.append("The selected data spans at least one hour.")
        else:
            score += 5.0
            reasons.append("The selected data is a short time slice.")
    score = round(_lantern_clamp(score, 0.0, 100.0), 1)
    label = "HIGH" if score >= 75 else "MEDIUM" if score >= 45 else "LOW"
    return {"score_0_100": score, "label": label, "reasons": reasons, "duration_hours": duration_hours}


def _lantern_build_clearance_payload(
    *,
    collection_id=None,
    collection_ids=None,
    start_utc=None,
    end_utc=None,
    freq_min_mhz: float = 1100.0,
    freq_max_mhz: float = 1650.0,
    freq_bin_mhz: float = 2.0,
    spike_dbm: float = -60.0,
    min_dbm=None,
    max_rows: int = 500000,
) -> dict[str, _LanternAny]:
    rows, ids = _lantern_query_lband_events(
        collection_id=collection_id,
        collection_ids=collection_ids,
        start_utc=start_utc,
        end_utc=end_utc,
        min_mhz=freq_min_mhz,
        max_mhz=freq_max_mhz,
        min_dbm=min_dbm,
        max_rows=max_rows,
    )
    bin_width = max(0.5, float(freq_bin_mhz))
    span = max(0.5, float(freq_max_mhz) - float(freq_min_mhz))
    bin_count = max(1, int(_lantern_math.ceil(span / bin_width)))
    bins: list[dict[str, _LanternAny]] = []
    for i in range(bin_count):
        lo = float(freq_min_mhz) + i * bin_width
        hi = min(float(freq_max_mhz), lo + bin_width)
        bins.append({
            "freq_bin_mhz": round(lo, 3),
            "freq_center_mhz": round((lo + hi) / 2.0, 3),
            "freq_min_mhz": round(lo, 3),
            "freq_max_mhz": round(hi, 3),
            "event_count": 0,
            "strengths": [],
            "spike_count": 0,
        })

    band_work: dict[str, dict[str, _LanternAny]] = {
        str(b["id"]): {"band": b, "event_count": 0, "strengths": [], "spike_count": 0} for b in _LANTERN_GNSS_BANDS
    }

    first_dt = None
    last_dt = None
    collection_set: set[int] = set()
    for row in rows:
        try:
            f_mhz = float(row.get("frequency_hz")) / 1_000_000.0
            strength = float(row.get("strength_dbm"))
        except Exception:
            continue
        dt = _lantern_parse_dt(row.get("timestamp_utc"))
        if dt:
            first_dt = dt if first_dt is None or dt < first_dt else first_dt
            last_dt = dt if last_dt is None or dt > last_dt else last_dt
        if row.get("collection_id") is not None:
            try:
                collection_set.add(int(row.get("collection_id")))
            except Exception:
                pass
        idx = int(_lantern_math.floor((f_mhz - float(freq_min_mhz)) / bin_width))
        if 0 <= idx < len(bins):
            bins[idx]["event_count"] += 1
            bins[idx]["strengths"].append(strength)
            if strength >= float(spike_dbm):
                bins[idx]["spike_count"] += 1
        for band in _LANTERN_GNSS_BANDS:
            if float(band["min_mhz"]) <= f_mhz <= float(band["max_mhz"]):
                work = band_work[str(band["id"])]
                work["event_count"] += 1
                work["strengths"].append(strength)
                if strength >= float(spike_dbm):
                    work["spike_count"] += 1

    max_bin_count = max((int(b["event_count"]) for b in bins), default=1) or 1
    max_bin_spikes = max((int(b["spike_count"]) for b in bins), default=0)
    frequency_bins: list[dict[str, _LanternAny]] = []
    for b in bins:
        vals = [float(v) for v in b.pop("strengths", [])]
        max_dbm = round(max(vals), 1) if vals else None
        avg_dbm = round(sum(vals) / len(vals), 1) if vals else None
        min_strength = round(min(vals), 1) if vals else None
        burden = _lantern_burden_score(int(b["event_count"]), int(b["spike_count"]), max_dbm, max_bin_count, max_bin_spikes)
        clearance = round(100.0 - burden, 1)
        item = dict(b)
        item.update({
            "avg_dbm": avg_dbm,
            "min_dbm": min_strength,
            "max_dbm": max_dbm,
            "rf_burden_score": burden,
            "clearance_score": clearance,
            "display_color": _lantern_display_colour(clearance, int(b["event_count"]), len(rows)),
            "overlaps": _lantern_overlap_band_labels(float(b["freq_min_mhz"]), float(b["freq_max_mhz"])),
        })
        frequency_bins.append(item)

    max_band_count = max((int(w["event_count"]) for w in band_work.values()), default=1) or 1
    max_band_spikes = max((int(w["spike_count"]) for w in band_work.values()), default=0)
    band_scores: list[dict[str, _LanternAny]] = []
    for work in band_work.values():
        band = dict(work["band"])
        vals = [float(v) for v in work.get("strengths", [])]
        event_count = int(work.get("event_count") or 0)
        spike_count = int(work.get("spike_count") or 0)
        max_dbm = round(max(vals), 1) if vals else None
        avg_dbm = round(sum(vals) / len(vals), 1) if vals else None
        min_strength = round(min(vals), 1) if vals else None
        burden = _lantern_burden_score(event_count, spike_count, max_dbm, max_band_count, max_band_spikes)
        clearance = round(100.0 - burden, 1)
        band.update({
            "label": _lantern_band_label(band),
            "event_count": event_count,
            "spike_count": spike_count,
            "avg_dbm": avg_dbm,
            "min_dbm": min_strength,
            "max_dbm": max_dbm,
            "rf_burden_score": burden,
            "clearance_score": clearance,
            "status": _lantern_clearance_status(clearance, event_count, len(rows)),
            "display_color": _lantern_display_colour(clearance, event_count, len(rows)),
            "plain_reason": (
                "No MOTH detections in this band; verify with live receiver checks."
                if event_count <= 0 and len(rows) > 0 else
                f"{event_count} event(s), {spike_count} strong spike(s), max {max_dbm} dBm."
                if event_count > 0 else
                "No data available under the selected filters."
            ),
        })
        band_scores.append(band)
    band_scores.sort(key=lambda r: (float(r.get("clearance_score") or 0.0), -int(r.get("event_count") or 0), -int(r.get("spike_count") or 0)), reverse=True)
    if band_scores and len(rows) > 0:
        band_scores[0]["status"] = "QUIETEST OBSERVED" if int(band_scores[0].get("event_count") or 0) > 0 else "QUIETEST OBSERVED BY ABSENCE - VERIFY"

    peaks = [b for b in frequency_bins if int(b.get("event_count") or 0) > 0]
    peaks.sort(key=lambda x: (float(x.get("rf_burden_score") or 0.0), int(x.get("spike_count") or 0), float(x.get("max_dbm") or -999.0)), reverse=True)
    peak_traffic = []
    for p in peaks[:12]:
        peak_traffic.append({
            "freq_center_mhz": p.get("freq_center_mhz"),
            "freq_range_mhz": [p.get("freq_min_mhz"), p.get("freq_max_mhz")],
            "event_count": p.get("event_count"),
            "spike_count": p.get("spike_count"),
            "max_dbm": p.get("max_dbm"),
            "rf_burden_score": p.get("rf_burden_score"),
            "overlaps": p.get("overlaps") or [],
        })

    confidence = _lantern_confidence(len(rows), len(collection_set), first_dt, last_dt)
    best = band_scores[0] if band_scores else None
    if len(rows) <= 0:
        readiness_status = "NO DATA"
        readiness_reason = "No L-band detections matched the selected filters. Do not infer RF/GNSS serviceability."
    elif confidence["label"] == "LOW":
        readiness_status = "RF CHECK"
        readiness_reason = "The clearest GNSS option is visible, but evidence confidence is low. Validate before use."
    elif best and float(best.get("clearance_score") or 0) >= 80 and int(best.get("spike_count") or 0) == 0:
        readiness_status = "RF SUPPORTS"
        readiness_reason = "At least one GNSS band shows low observed RF burden under the selected filters."
    elif best and float(best.get("clearance_score") or 0) >= 55:
        readiness_status = "RF CHECK"
        readiness_reason = "A usable-looking option exists, but RF burden or data confidence requires validation."
    else:
        readiness_status = "RF DOES NOT SUPPORT"
        readiness_reason = "All ranked GNSS options are busy, spike-prone, or insufficiently supported."

    return {
        "version": _LANTERN_VERSION,
        "generated_utc": _lantern_iso(_lantern_datetime.now(_lantern_timezone.utc)),
        "selected_collection_ids": ids,
        "selected_window_utc": [start_utc, end_utc],
        "frequency_range_mhz": [float(freq_min_mhz), float(freq_max_mhz)],
        "freq_bin_mhz": float(freq_bin_mhz),
        "spike_dbm": float(spike_dbm),
        "total_lband_events": len(rows),
        "first_timestamp_utc": _lantern_iso(first_dt),
        "last_timestamp_utc": _lantern_iso(last_dt),
        "scan_count": len(collection_set),
        "confidence": confidence,
        "readiness": {
            "status": readiness_status,
            "reason": readiness_reason,
            "best_gnss_option": best,
            "plain_language": (
                f"{readiness_status}: {best.get('label')} is the lowest observed RF-burden option."
                if best else f"{readiness_status}: no GNSS option could be ranked."
            ),
        },
        "gnss_bands": _LANTERN_GNSS_BANDS,
        "service_blocks": _LANTERN_SERVICE_BLOCKS,
        "frequency_bins": frequency_bins,
        "band_scores": band_scores,
        "peak_traffic": peak_traffic,
        "colour_logic": _LANTERN_STYLE_NOTE,
        "limitations": [
            "MOTH data is event-based. No detection does not prove the band was empty.",
            "Lowest observed RF burden does not prove GNSS receiver integrity or flight safety.",
            "Use only with authorised operations, live receiver health checks, satellite count, HDOP/PDOP or OEM quality metrics, and link checks.",
        ],
        "still_required": [
            "Equipment receiver fix status and stability check.",
            "Satellite count by constellation where available.",
            "HDOP/PDOP or OEM GNSS quality metric.",
            "Independent receiver cross-check.",
            "Command/control or telemetry link check.",
            "Airspace, weather, aircraft, battery, crew, and local approval checks.",
        ],
    }


@app.get("/lantern", response_class=_LanternHTMLResponse)
def lantern_page() -> str:
    page = STATIC_DIR / "lantern_flight_safety.html"  # type: ignore[name-defined]
    if page.exists():
        return page.read_text(encoding="utf-8")
    return "<h1>EEI LANTERN</h1><p>lantern_flight_safety.html is not installed in the static folder.</p>"


@app.get("/api/lantern/health")
def lantern_health() -> dict[str, _LanternAny]:
    return {
        "status": "ok",
        "lantern_version": _LANTERN_VERSION,
        "page": "/lantern",
        "static_page": "/static/lantern_flight_safety.html?v=010",
        "clearance_endpoint": "/api/lantern/clearance-spectrum",
        "rf_burden_geojson": "/api/lantern/rf-burden.geojson",
    }


@app.get("/api/lantern/gnss-bands")
def lantern_gnss_bands() -> dict[str, _LanternAny]:
    return {
        "version": _LANTERN_VERSION,
        "gnss_bands": _LANTERN_GNSS_BANDS,
        "service_blocks": _LANTERN_SERVICE_BLOCKS,
        "frequency_range_mhz": [1100.0, 1650.0],
        "note": "Practical display catalogue for LANTERN GNSS L-band clearance view.",
    }


@app.get("/api/lantern/clearance-spectrum")
def lantern_clearance_spectrum(
    collection_id: int | None = None,
    collection_ids: str | None = None,
    start_utc: str | None = None,
    end_utc: str | None = None,
    freq_min_mhz: float = _LanternQuery(default=1100.0, ge=900.0, le=2000.0),
    freq_max_mhz: float = _LanternQuery(default=1650.0, ge=900.0, le=2000.0),
    freq_bin_mhz: float = _LanternQuery(default=2.0, ge=0.5, le=25.0),
    spike_dbm: float = _LanternQuery(default=-60.0),
    min_dbm: float | None = None,
    max_rows: int = _LanternQuery(default=500000, ge=1000, le=2000000),
) -> dict[str, _LanternAny]:
    if float(freq_max_mhz) <= float(freq_min_mhz):
        return {"version": _LANTERN_VERSION, "error": "freq_max_mhz must be greater than freq_min_mhz"}
    return _lantern_build_clearance_payload(
        collection_id=collection_id,
        collection_ids=collection_ids,
        start_utc=start_utc,
        end_utc=end_utc,
        freq_min_mhz=freq_min_mhz,
        freq_max_mhz=freq_max_mhz,
        freq_bin_mhz=freq_bin_mhz,
        spike_dbm=spike_dbm,
        min_dbm=min_dbm,
        max_rows=max_rows,
    )


@app.get("/api/lantern/flight-brief")
def lantern_flight_brief(
    collection_id: int | None = None,
    collection_ids: str | None = None,
    start_utc: str | None = None,
    end_utc: str | None = None,
    duration_minutes: int = _LanternQuery(default=30, ge=5, le=240),
    step_minutes: int = _LanternQuery(default=10, ge=1, le=120),
    width_mhz: float = _LanternQuery(default=40.0, ge=1.0, le=100.0),
    spike_dbm: float = _LanternQuery(default=-60.0),
    max_rows: int = _LanternQuery(default=500000, ge=1000, le=2000000),
) -> dict[str, _LanternAny]:
    clearance = _lantern_build_clearance_payload(
        collection_id=collection_id,
        collection_ids=collection_ids,
        start_utc=start_utc,
        end_utc=end_utc,
        freq_min_mhz=1100.0,
        freq_max_mhz=1650.0,
        freq_bin_mhz=2.0,
        spike_dbm=spike_dbm,
        max_rows=max_rows,
    )
    launch = None
    launch_error = None
    try:
        launch_fn = globals().get("moth_advanced_launch_windows")
        if launch_fn:
            launch = launch_fn(
                collection_id=collection_id,
                collection_ids=collection_ids,
                start_utc=start_utc,
                end_utc=end_utc,
                duration_minutes=duration_minutes,
                step_minutes=step_minutes,
                width_mhz=width_mhz,
                spike_dbm=spike_dbm,
                max_rows=max_rows,
            )
    except Exception as exc:
        launch_error = str(exc)
    best_launch = (launch.get("windows") or [None])[0] if isinstance(launch, dict) else None
    return {
        "version": _LANTERN_VERSION,
        "generated_utc": clearance.get("generated_utc"),
        "readiness": clearance.get("readiness"),
        "confidence": clearance.get("confidence"),
        "best_launch_window": best_launch,
        "launch_window_source": "advanced_launch_windows" if best_launch else None,
        "launch_error": launch_error,
        "best_gnss_option": (clearance.get("band_scores") or [None])[0],
        "constellation_ranking": (clearance.get("band_scores") or [])[:8],
        "peak_traffic": clearance.get("peak_traffic") or [],
        "limitations": clearance.get("limitations") or [],
        "still_required": clearance.get("still_required") or [],
    }


@app.get("/api/lantern/rf-burden.geojson")
def lantern_rf_burden_geojson(
    resolution: int = _LanternQuery(default=11, ge=8, le=12),
    collection_id: int | None = None,
    collection_ids: str | None = None,
    start_utc: str | None = None,
    end_utc: str | None = None,
    freq_min_mhz: float = _LanternQuery(default=1100.0, ge=900.0, le=2000.0),
    freq_max_mhz: float = _LanternQuery(default=1650.0, ge=900.0, le=2000.0),
    spike_dbm: float = _LanternQuery(default=-60.0),
    min_dbm: float | None = None,
    max_rows: int = _LanternQuery(default=500000, ge=1000, le=2000000),
) -> dict[str, _LanternAny]:
    rows, ids = _lantern_query_lband_events(
        collection_id=collection_id,
        collection_ids=collection_ids,
        start_utc=start_utc,
        end_utc=end_utc,
        min_mhz=freq_min_mhz,
        max_mhz=freq_max_mhz,
        min_dbm=min_dbm,
        max_rows=max_rows,
    )
    grouped: dict[str, dict[str, _LanternAny]] = {}
    for row in rows:
        try:
            cell = h3_cell_for_event(row, resolution)  # type: ignore[name-defined]
        except Exception:
            try:
                if row.get("lat") is None or row.get("lon") is None:
                    continue
                cell = latlon_to_cell(float(row["lat"]), float(row["lon"]), resolution)  # type: ignore[name-defined]
            except Exception:
                continue
        if not cell:
            continue
        g = grouped.setdefault(str(cell), {"event_count": 0, "strengths": [], "spike_count": 0, "first_timestamp_utc": None, "last_timestamp_utc": None})
        try:
            strength = float(row.get("strength_dbm"))
        except Exception:
            continue
        g["event_count"] += 1
        g["strengths"].append(strength)
        if strength >= float(spike_dbm):
            g["spike_count"] += 1
        ts = row.get("timestamp_utc")
        if ts:
            if g["first_timestamp_utc"] is None or str(ts) < str(g["first_timestamp_utc"]):
                g["first_timestamp_utc"] = str(ts)
            if g["last_timestamp_utc"] is None or str(ts) > str(g["last_timestamp_utc"]):
                g["last_timestamp_utc"] = str(ts)

    max_event_count = max((int(g["event_count"]) for g in grouped.values()), default=1) or 1
    max_spike_count = max((int(g["spike_count"]) for g in grouped.values()), default=0)
    features: list[dict[str, _LanternAny]] = []
    for cell, g in grouped.items():
        vals = [float(v) for v in g.get("strengths", [])]
        max_dbm = round(max(vals), 1) if vals else None
        avg_dbm = round(sum(vals) / len(vals), 1) if vals else None
        burden = _lantern_burden_score(int(g["event_count"]), int(g["spike_count"]), max_dbm, max_event_count, max_spike_count)
        clearance = round(100.0 - burden, 1)
        try:
            boundary = cell_to_boundary_lnglat(cell)  # type: ignore[name-defined]
        except Exception:
            boundary = None
        if not boundary:
            continue
        features.append({
            "type": "Feature",
            "geometry": {"type": "Polygon", "coordinates": [boundary]},
            "properties": {
                "h3_cell": cell,
                "event_count": int(g["event_count"]),
                "spike_count": int(g["spike_count"]),
                "avg_dbm": avg_dbm,
                "max_dbm": max_dbm,
                "rf_burden_score": burden,
                "clearance_score": clearance,
                "interpretation": _lantern_clearance_status(clearance, int(g["event_count"]), len(rows)),
                "display_color": _lantern_display_colour(clearance, int(g["event_count"]), len(rows)),
                "first_timestamp_utc": g.get("first_timestamp_utc"),
                "last_timestamp_utc": g.get("last_timestamp_utc"),
                "colour_logic": _LANTERN_STYLE_NOTE,
                "plain_meaning": "RF burden polygon: green is quieter/clearer observed; red is busy/noisy/spike-prone.",
            },
        })
    return {
        "type": "FeatureCollection",
        "features": features,
        "version": _LANTERN_VERSION,
        "selected_collection_ids": ids,
        "colour_logic": _LANTERN_STYLE_NOTE,
    }

# ---- end EEI LANTERN v0.10.0 additions ----

# ---- EEI LANTERN v0.10.3 signal interpretation reporting ----
# Additive reporting endpoint. It explains strong dBm detections in operational
# context without changing deterministic clearance scoring.

from datetime import datetime as _lantern_v0103_datetime, timezone as _lantern_v0103_timezone
import math as _lantern_v0103_math

try:
    _LANTERN_V0103_QUERY = _LanternQuery  # type: ignore[name-defined]
except Exception:  # pragma: no cover - defensive for unusual patch order
    from fastapi import Query as _LANTERN_V0103_QUERY

_LANTERN_REPORTING_VERSION = "0.10.3"
_LANTERN_REPORTING_REFERENCE_DBM = -130.0
_LANTERN_REPORTING_REFERENCE_TEXT = (
    "For plain-English reporting, LANTERN treats about -130 dBm as a practical "
    "GNSS L1 received-power reference. A -60 dBm in-band detection is therefore "
    "about 70 dB, or roughly 10,000,000 times by power, above that reference."
)


def _lantern_v0103_ratio_text(delta_db):
    try:
        ratio = 10 ** (float(delta_db) / 10.0)
    except Exception:
        return None
    if ratio >= 1_000_000_000:
        return f"{ratio / 1_000_000_000:.1f} billion times"
    if ratio >= 1_000_000:
        return f"{ratio / 1_000_000:.1f} million times"
    if ratio >= 1_000:
        return f"{ratio / 1_000:.1f} thousand times"
    if ratio >= 10:
        return f"{ratio:.0f} times"
    if ratio >= 1:
        return f"{ratio:.1f} times"
    return f"{ratio:.3f} times"


def _lantern_v0103_power_comparison(max_dbm, reference_dbm=_LANTERN_REPORTING_REFERENCE_DBM):
    if max_dbm is None:
        return {
            "reference_dbm": reference_dbm,
            "delta_db": None,
            "power_ratio": None,
            "plain": "No max dBm value is available for this item.",
        }
    try:
        delta = round(float(max_dbm) - float(reference_dbm), 1)
    except Exception:
        return {
            "reference_dbm": reference_dbm,
            "delta_db": None,
            "power_ratio": None,
            "plain": "The max dBm value could not be interpreted numerically.",
        }
    ratio_text = _lantern_v0103_ratio_text(delta)
    return {
        "reference_dbm": reference_dbm,
        "delta_db": delta,
        "power_ratio": ratio_text,
        "plain": f"{float(max_dbm):.1f} dBm is {delta:.1f} dB, or about {ratio_text}, above the {reference_dbm:.0f} dBm GNSS reference.",
    }


def _lantern_v0103_signal_context(max_dbm, overlaps=None, event_count=0, spike_count=0, spike_dbm=-60.0, *, band_label=None):
    overlaps = overlaps or []
    if isinstance(overlaps, str):
        overlaps = [overlaps]
    near_gnss = bool(overlaps) or bool(band_label)
    label = band_label or (", ".join(str(x) for x in overlaps[:3]) if overlaps else "non-GNSS / unlabelled")
    if max_dbm is None:
        return {
            "concern_level": "NO DATA",
            "headline": "No observed detection level",
            "briefing": "No MOTH detection level is available for this frequency/bin under the selected filters.",
            "operator_action": "Do not infer GPS serviceability from absence of a MOTH detection. Use receiver health checks.",
            "power_comparison": _lantern_v0103_power_comparison(None),
        }
    try:
        max_v = float(max_dbm)
        spike_v = float(spike_dbm)
        events = int(event_count or 0)
        spikes = int(spike_count or 0)
    except Exception:
        return {
            "concern_level": "CHECK",
            "headline": "Signal level needs review",
            "briefing": "The dBm value could not be interpreted numerically.",
            "operator_action": "Review the source row and sensor setup before briefing.",
            "power_comparison": _lantern_v0103_power_comparison(None),
        }

    strong = max_v >= spike_v
    very_strong = max_v >= -50.0
    moderate = max_v >= -75.0
    comparison = _lantern_v0103_power_comparison(max_v)

    if strong and near_gnss:
        return {
            "concern_level": "HIGH",
            "headline": "Strong GNSS-band RF detection",
            "briefing": f"{max_v:.1f} dBm overlaps {label}. Treat as operationally relevant; it could degrade GNSS if it is in-band, wide enough, persistent, or coincides with receiver symptoms.",
            "operator_action": "Check receiver fix, satellite count by constellation, HDOP/PDOP or OEM quality metric, C/N0 if available, persistence, bandwidth and location correlation.",
            "power_comparison": comparison,
        }
    if very_strong and near_gnss:
        return {
            "concern_level": "HIGH",
            "headline": "Very strong GNSS-window detection",
            "briefing": f"{max_v:.1f} dBm is very strong and overlaps {label}. Treat as a serious RF indicator until receiver checks prove stability.",
            "operator_action": "Repeat scan, check for broad/wideband activity, and compare with live equipment GNSS telemetry.",
            "power_comparison": comparison,
        }
    if strong and not near_gnss:
        return {
            "concern_level": "CONTEXT",
            "headline": "Strong RF away from labelled GNSS bands",
            "briefing": f"{max_v:.1f} dBm is strong RF activity, but it is not automatically a GPS concern unless receiver symptoms coincide or front-end overload is suspected.",
            "operator_action": "Brief as local RF activity. Check known emitters and receiver symptoms before treating it as GNSS interference.",
            "power_comparison": comparison,
        }
    if near_gnss and (moderate or spikes > 0 or events > 0):
        return {
            "concern_level": "CHECK",
            "headline": "GNSS-band activity to validate",
            "briefing": f"Activity overlaps {label}, but the strongest value is below the selected strong-spike threshold of {spike_v:.1f} dBm.",
            "operator_action": "Validate with receiver health data and compare against other available GNSS bands.",
            "power_comparison": comparison,
        }
    if moderate:
        return {
            "concern_level": "WATCH",
            "headline": "Moderate RF activity",
            "briefing": f"{max_v:.1f} dBm is visible RF activity outside labelled GNSS bands. It is context for the RF picture, not a GPS conclusion by itself.",
            "operator_action": "Monitor for persistence, recurrence and any correlation with equipment symptoms.",
            "power_comparison": comparison,
        }
    return {
        "concern_level": "LOW",
        "headline": "Low immediate GNSS concern from this detection",
        "briefing": f"{max_v:.1f} dBm is below the selected strong-spike threshold and does not by itself indicate GNSS degradation.",
        "operator_action": "Keep normal receiver checks in place; do not treat quiet MOTH data as proof of a clear band.",
        "power_comparison": comparison,
    }


def _lantern_v0103_concern_weight(level):
    return {"HIGH": 5, "CONTEXT": 4, "CHECK": 3, "WATCH": 2, "LOW": 1, "NO DATA": 0}.get(str(level or "").upper(), 0)


def _lantern_v0103_summarise_reporting(clearance, spike_dbm):
    peak_items = []
    for p in clearance.get("peak_traffic") or []:
        ctx = _lantern_v0103_signal_context(
            p.get("max_dbm"),
            p.get("overlaps") or [],
            p.get("event_count") or 0,
            p.get("spike_count") or 0,
            spike_dbm,
        )
        out = dict(p)
        out.update(ctx)
        peak_items.append(out)

    band_items = []
    for b in clearance.get("band_scores") or []:
        label = b.get("label") or "GNSS band"
        ctx = _lantern_v0103_signal_context(
            b.get("max_dbm"),
            [label],
            b.get("event_count") or 0,
            b.get("spike_count") or 0,
            spike_dbm,
            band_label=label,
        )
        band_items.append({
            "id": b.get("id"),
            "label": label,
            "min_mhz": b.get("min_mhz"),
            "max_mhz": b.get("max_mhz"),
            "event_count": b.get("event_count"),
            "spike_count": b.get("spike_count"),
            "max_dbm": b.get("max_dbm"),
            "clearance_score": b.get("clearance_score"),
            **ctx,
        })

    strong_gnss = [p for p in peak_items if p.get("overlaps") and str(p.get("concern_level")) == "HIGH"]
    strong_non_gnss = [p for p in peak_items if not p.get("overlaps") and str(p.get("concern_level")) in ("CONTEXT", "HIGH")]
    highest = None
    if peak_items:
        highest = sorted(peak_items, key=lambda p: (float(p.get("max_dbm") if p.get("max_dbm") is not None else -999), int(p.get("event_count") or 0)), reverse=True)[0]

    if strong_gnss:
        top = sorted(strong_gnss, key=lambda p: (float(p.get("max_dbm") or -999), int(p.get("event_count") or 0)), reverse=True)[0]
        headline = "Strong GNSS-band RF detected"
        summary = (
            f"{len(strong_gnss)} peak bin(s) at or above {float(spike_dbm):.1f} dBm overlap GNSS-labelled bands. "
            f"Highest: {top.get('freq_center_mhz')} MHz at {top.get('max_dbm')} dBm. Treat as operationally relevant and validate against receiver health."
        )
        status = "HIGH"
    elif strong_non_gnss:
        top = sorted(strong_non_gnss, key=lambda p: (float(p.get("max_dbm") or -999), int(p.get("event_count") or 0)), reverse=True)[0]
        headline = "Strong RF detected outside labelled GNSS bands"
        summary = (
            f"Strong RF is present, with the highest observed peak at {top.get('freq_center_mhz')} MHz / {top.get('max_dbm')} dBm. "
            "This is not automatically a GPS issue unless receiver symptoms or front-end overload are suspected."
        )
        status = "CONTEXT"
    elif int(clearance.get("total_lband_events") or 0) > 0:
        headline = "No strong GNSS-band spike at the selected threshold"
        summary = (
            f"No peak bin at or above {float(spike_dbm):.1f} dBm overlaps the labelled GNSS bands in the current view. "
            "Continue receiver checks because MOTH data is event-based."
        )
        status = "CHECK"
    else:
        headline = "No L-band evidence loaded for interpretation"
        summary = "No matching L-band MOTH detections are available under the selected filters. Do not infer GPS serviceability."
        status = "NO DATA"

    return {
        "status": status,
        "headline": headline,
        "summary": summary,
        "highest_peak": highest,
        "strong_gnss_peak_count": len(strong_gnss),
        "strong_non_gnss_peak_count": len(strong_non_gnss),
        "band_interpretations": band_items,
        "peak_interpretations": peak_items,
    }


@app.get("/api/lantern/reporting-interpretation")
def lantern_reporting_interpretation(
    collection_id: int | None = None,
    collection_ids: str | None = None,
    start_utc: str | None = None,
    end_utc: str | None = None,
    freq_min_mhz: float = _LANTERN_V0103_QUERY(default=1100.0, ge=900.0, le=2000.0),
    freq_max_mhz: float = _LANTERN_V0103_QUERY(default=1650.0, ge=900.0, le=2000.0),
    freq_bin_mhz: float = _LANTERN_V0103_QUERY(default=2.0, ge=0.5, le=25.0),
    spike_dbm: float = _LANTERN_V0103_QUERY(default=-60.0),
    min_dbm: float | None = None,
    max_rows: int = _LANTERN_V0103_QUERY(default=500000, ge=1000, le=2000000),
):
    """Plain-English signal-strength interpretation for pilots, flight safety and reports.

    This endpoint deliberately interprets MOTH detections in context: dBm strength,
    frequency overlap with GNSS bands, persistence/spikes, and required receiver checks.
    It does not change the deterministic RF burden scoring used by the clearance view.
    """
    if "_lantern_build_clearance_payload" not in globals():
        return {"version": _LANTERN_REPORTING_VERSION, "error": "LANTERN v0.10 clearance payload function is not installed."}

    clearance = _lantern_build_clearance_payload(  # type: ignore[name-defined]
        collection_id=collection_id,
        collection_ids=collection_ids,
        start_utc=start_utc,
        end_utc=end_utc,
        freq_min_mhz=freq_min_mhz,
        freq_max_mhz=freq_max_mhz,
        freq_bin_mhz=freq_bin_mhz,
        spike_dbm=spike_dbm,
        min_dbm=min_dbm,
        max_rows=max_rows,
    )
    summary = _lantern_v0103_summarise_reporting(clearance, spike_dbm)
    threshold_delta = round(float(spike_dbm) - _LANTERN_REPORTING_REFERENCE_DBM, 1)
    threshold_ratio = _lantern_v0103_ratio_text(threshold_delta)
    return {
        "version": _LANTERN_REPORTING_VERSION,
        "generated_utc": _lantern_v0103_datetime.now(_lantern_v0103_timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "selected_collection_ids": clearance.get("selected_collection_ids") or [],
        "selected_window_utc": clearance.get("selected_window_utc"),
        "spike_dbm": float(spike_dbm),
        "reference": {
            "gps_l1_mhz": 1575.42,
            "gps_l2_mhz": 1227.60,
            "gps_l5_mhz": 1176.45,
            "approx_gnss_received_power_dbm": _LANTERN_REPORTING_REFERENCE_DBM,
            "spike_threshold_delta_db": threshold_delta,
            "spike_threshold_power_ratio": threshold_ratio,
            "plain": _LANTERN_REPORTING_REFERENCE_TEXT,
        },
        "simple_rule": "A strong MOTH dBm reading becomes a GNSS concern when it is in or near a GNSS band, is wideband or persistent, or correlates with receiver degradation.",
        "bottom_line": "A strong RF detection is not automatically hostile or unsafe. It becomes operationally concerning for GPS/GNSS when frequency, bandwidth, persistence, location correlation or receiver symptoms support that conclusion.",
        "top_level": summary,
        "concern_matrix": [
            {"case": "Strong detection near GPS L1/E1, L2/G2, L5/E5/B2 or other GNSS band", "level": "HIGH", "reporting_wording": "Operationally relevant GNSS-window RF indicator; validate receiver health and persistence."},
            {"case": "Strong detection away from labelled GNSS bands", "level": "CONTEXT", "reporting_wording": "Strong local RF activity, but not automatically a GPS issue without receiver symptoms or overload evidence."},
            {"case": "Broad or recurrent activity across GNSS bands", "level": "HIGH", "reporting_wording": "More concerning than a single narrow spike; compare with fix/satellite/C/N0/HDOP behaviour."},
            {"case": "A single spike with stable receiver performance", "level": "CHECK", "reporting_wording": "Investigate and monitor; a single spike does not by itself prove equipment failure."},
            {"case": "No MOTH detection", "level": "NO PROOF OF CLEAR", "reporting_wording": "Do not infer that the band was empty; MOTH data is event-based."},
        ],
        "next_checks": [
            "Frequency: is the peak inside or close to a GNSS band such as L1/E1, L2/G2 or L5/E5?",
            "Bandwidth: is it a narrow spike, multiple adjacent bins, or broad noise across a GNSS window?",
            "Persistence: is it constant, pulsed, sweeping, recurrent or isolated?",
            "Receiver symptoms: loss of satellites, poor fix, position jump, degraded HDOP/PDOP or C/N0 drop.",
            "Location correlation: does the signal strengthen in a specific area or direction?",
            "Sensor setup: confirm dBm units, antenna type, placement, gain/settings and comparable scan conditions.",
        ],
        "reporting_lines": [
            f"The selected strong-spike threshold is {float(spike_dbm):.1f} dBm.",
            f"At {float(spike_dbm):.1f} dBm, an in-band detection is about {threshold_delta:.1f} dB, or {threshold_ratio}, above the practical {int(_LANTERN_REPORTING_REFERENCE_DBM)} dBm GNSS reference.",
            "Treat strong in-band GNSS detections as operationally relevant, but not as proof of jamming without receiver symptoms, bandwidth/persistence evidence or repeated correlation.",
            "Treat strong out-of-band detections as RF context unless GNSS performance degrades at the same time or receiver front-end overload is suspected.",
        ],
        "limitations": [
            "MOTH detections are event-based observations, not continuous calibrated spectrum recordings.",
            "A quiet MOTH window does not prove absence of RF energy, and a single spike does not prove GPS failure.",
            "Antenna type, placement, gain, scan settings and local geometry can change displayed dBm values.",
            "Final reporting should combine MOTH observations with equipment receiver logs, independent GPS checks and authorised safety/operational approval.",
        ],
    }

# ---- end EEI LANTERN v0.10.3 signal interpretation reporting ----

# ---- EEI LANTERN v0.11.3 J2 Live Article Rotator API ----
# Restores the real J2 Live Report article list, links and live-update feed.
# This block is intentionally additive, but the installer removes earlier broken /api/j2 blocks first.

import csv as _j2_csv
import html as _j2_html
import io as _j2_io
import json as _j2_json
import re as _j2_re
import time as _j2_time
import urllib.parse as _j2_urllib_parse
import urllib.request as _j2_urllib_request
import xml.etree.ElementTree as _j2_ET
from datetime import datetime as _j2_datetime, timezone as _j2_timezone
from email.utils import parsedate_to_datetime as _j2_parsedate_to_datetime
from pathlib import Path as _J2Path
from typing import Any as _J2Any

from fastapi import Query as _J2Query
from fastapi.responses import HTMLResponse as _J2HTMLResponse, Response as _J2Response

_J2_API_VERSION = "0.11.3"
_J2_CACHE_SECONDS = 10 * 60
_J2_DEFAULT_AOI = "Aden Adde / Mogadishu"
_J2_DEFAULT_ROTATE_SECONDS = 12
_J2_DEFAULT_LIVE_REFRESH_MINUTES = 5


def _j2_now() -> str:
    return _j2_datetime.now(_j2_timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _j2_static_dir() -> _J2Path:
    try:
        return STATIC_DIR  # type: ignore[name-defined]
    except Exception:
        return _J2Path(__file__).with_name("static")


def _j2_cache_dir() -> _J2Path:
    try:
        root = _J2Path(UPLOAD_DIR) / "j2_cache"  # type: ignore[name-defined]
    except Exception:
        root = _j2_static_dir().parent / "j2_cache"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _j2_safe_key(text: str) -> str:
    key = _j2_re.sub(r"[^A-Za-z0-9._-]+", "_", (text or "default").strip())[:96]
    return key or "default"


def _j2_news_cache_path(aoi: str) -> _J2Path:
    return _j2_cache_dir() / f"news_{_j2_safe_key(aoi)}.json"


def _j2_report_cache_path() -> _J2Path:
    return _j2_cache_dir() / "last_j2_report.json"


def _j2_read_json(path: _J2Path) -> dict[str, _J2Any] | None:
    try:
        if not path.exists():
            return None
        return _j2_json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _j2_write_json(path: _J2Path, payload: dict[str, _J2Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_j2_json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _j2_clean_text(value: str | None) -> str:
    if value is None:
        return ""
    text = str(value)
    text = _j2_re.sub(r"<[^>]+>", " ", text)
    text = _j2_html.unescape(text)
    text = _j2_re.sub(r"\s+", " ", text).strip()
    return text


def _j2_pub_to_iso(value: str | None) -> str | None:
    text = _j2_clean_text(value)
    if not text:
        return None
    try:
        dt = _j2_parsedate_to_datetime(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_j2_timezone.utc)
        return dt.astimezone(_j2_timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    except Exception:
        return text


def _j2_parse_ids(collection_id: int | None = None, collection_ids: str | None = None) -> list[int]:
    try:
        return parse_collection_ids(collection_id=collection_id, collection_ids=collection_ids)  # type: ignore[name-defined]
    except Exception:
        ids: set[int] = set()
        if collection_id not in (None, ""):
            try:
                ids.add(int(collection_id))
            except Exception:
                pass
        if collection_ids:
            for part in str(collection_ids).replace(";", ",").split(","):
                part = part.strip()
                if not part:
                    continue
                try:
                    ids.add(int(part))
                except Exception:
                    continue
        return sorted(ids)


def _j2_collection_filter(ids: list[int], where: list[str], params: list[_J2Any]) -> None:
    if ids:
        where.append("collection_id IN (" + ",".join("?" for _ in ids) + ")")
        params.extend(ids)


def _j2_freq_case_sql() -> str:
    return """
        CASE
          WHEN frequency_hz BETWEEN 1555420000 AND 1595420000 THEN 'L1'
          WHEN frequency_hz BETWEEN 1207600000 AND 1247600000 THEN 'L2'
          WHEN frequency_hz BETWEEN 1156450000 AND 1196450000 THEN 'L5'
          ELSE 'OTHER'
        END
    """


def _j2_local_metrics(collection_id: int | None = None, collection_ids: str | None = None) -> dict[str, _J2Any]:
    ids = _j2_parse_ids(collection_id=collection_id, collection_ids=collection_ids)
    where = ["1=1"]
    params: list[_J2Any] = []
    _j2_collection_filter(ids, where, params)
    where_sql = " AND ".join(where)
    try:
        conn = connect()  # type: ignore[name-defined]
        try:
            totals = dict(conn.execute(
                f"""
                SELECT
                  COUNT(*) AS total_events,
                  SUM(CASE WHEN valid = 1 THEN 1 ELSE 0 END) AS valid_events,
                  COUNT(DISTINCT collection_id) AS collection_count,
                  MIN(timestamp_utc) AS first_timestamp_utc,
                  MAX(timestamp_utc) AS last_timestamp_utc,
                  MIN(frequency_hz) AS min_frequency_hz,
                  MAX(frequency_hz) AS max_frequency_hz,
                  MIN(strength_dbm) AS min_dbm,
                  MAX(strength_dbm) AS max_dbm,
                  ROUND(AVG(strength_dbm), 1) AS avg_dbm
                FROM moth_events
                WHERE {where_sql}
                """,
                params,
            ).fetchone())
            gnss = rows_to_dicts(conn.execute(  # type: ignore[name-defined]
                f"""
                SELECT {_j2_freq_case_sql()} AS band,
                       COUNT(*) AS event_count,
                       SUM(CASE WHEN strength_dbm >= -60 THEN 1 ELSE 0 END) AS strong_spike_count,
                       ROUND(AVG(strength_dbm), 1) AS avg_dbm,
                       MIN(strength_dbm) AS min_dbm,
                       MAX(strength_dbm) AS max_dbm
                FROM moth_events
                WHERE valid = 1
                  AND frequency_hz IS NOT NULL
                  AND strength_dbm IS NOT NULL
                  AND {where_sql}
                GROUP BY band
                ORDER BY CASE band WHEN 'L1' THEN 1 WHEN 'L2' THEN 2 WHEN 'L5' THEN 3 ELSE 4 END
                """,
                params,
            ).fetchall())
            latest_collections = rows_to_dicts(conn.execute(  # type: ignore[name-defined]
                """
                SELECT collection_id, collection_name, upload_time_utc
                FROM moth_collections
                ORDER BY upload_time_utc DESC, collection_id DESC
                LIMIT 10
                """
            ).fetchall())
        finally:
            conn.close()
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
            "selected_collection_ids": ids,
            "totals": {},
            "gnss_bands": [],
            "latest_collections": [],
            "valid_event_count": 0,
            "total_event_count": 0,
            "gnss_event_count": 0,
            "strong_gnss_spike_count": 0,
            "has_local_data": False,
        }
    by_band = {str(r.get("band")): r for r in gnss}
    for band in ("L1", "L2", "L5", "OTHER"):
        by_band.setdefault(band, {"band": band, "event_count": 0, "strong_spike_count": 0, "avg_dbm": None, "min_dbm": None, "max_dbm": None})
    total = int(totals.get("total_events") or 0)
    valid = int(totals.get("valid_events") or 0)
    strong_gnss = sum(int(by_band[b].get("strong_spike_count") or 0) for b in ("L1", "L2", "L5"))
    gnss_events = sum(int(by_band[b].get("event_count") or 0) for b in ("L1", "L2", "L5"))
    return {
        "ok": True,
        "selected_collection_ids": ids,
        "totals": totals,
        "gnss_bands": [by_band["L1"], by_band["L2"], by_band["L5"], by_band["OTHER"]],
        "latest_collections": latest_collections,
        "valid_event_count": valid,
        "total_event_count": total,
        "gnss_event_count": gnss_events,
        "strong_gnss_spike_count": strong_gnss,
        "has_local_data": valid > 0,
    }


def _j2_source_register(aoi: str = _J2_DEFAULT_AOI) -> list[dict[str, _J2Any]]:
    return [
        {
            "title": "EASA GNSS outages and alterations watch page",
            "source": "EASA",
            "url": "https://www.easa.europa.eu/en/domains/air-operations/global-navigation-satellite-system-outages-and-alterations",
            "published_utc": None,
            "category": "GNSS/RF",
            "confidence": "source-register",
            "summary": "Official aviation safety watch page for GNSS jamming/spoofing affected areas. Treat as current OSINT context, not local receiver proof.",
            "aoi": aoi,
            "live": False,
        },
        {
            "title": "EASA Somalia conflict-zone information bulletin",
            "source": "EASA CZIB",
            "url": "https://www.easa.europa.eu/en/domains/air-operations/czibs/czib-2017-05r19",
            "published_utc": None,
            "category": "Aviation",
            "confidence": "source-register",
            "summary": "Official conflict-zone aviation context relevant to conservative airport-area planning and formal coordination.",
            "aoi": aoi,
            "live": False,
        },
        {
            "title": "GPSJam daily GPS/GNSS interference map",
            "source": "GPSJam",
            "url": "https://gpsjam.org/",
            "published_utc": None,
            "category": "GNSS/RF",
            "confidence": "indicator",
            "summary": "ADS-B-derived GPS/GNSS interference map. Useful daily watch item, but not a direct local MOTH or equipment receiver measurement.",
            "aoi": aoi,
            "live": False,
        },
        {
            "title": "Flightradar24 GPS jamming and interference map methodology",
            "source": "Flightradar24",
            "url": "https://www.flightradar24.com/data/gps-jamming",
            "published_utc": None,
            "category": "GNSS/RF",
            "confidence": "indicator",
            "summary": "ADS-B-derived indicator of possible GNSS interference. Useful for cross-checking, not local equipment serviceability evidence.",
            "aoi": aoi,
            "live": False,
        },
        {
            "title": "Somalia Civil Aviation Authority eAIP - HCMM Aden Adde Intl",
            "source": "SCAA eAIP",
            "url": "https://aip.scaa.gov.so/eAIP/HC-AD-2.HCMM-en-GB.html",
            "published_utc": None,
            "category": "Aviation",
            "confidence": "official-source",
            "summary": "Official aeronautical source for airport facts, constraints and published procedures. Use with current NOTAM/coordination checks.",
            "aoi": aoi,
            "live": False,
        },
        {
            "title": "IFALPA/IFATCA: unlawful communications interference within Mogadishu FIR",
            "source": "IFALPA / IFATCA",
            "url": "https://ifatca.org/unlawful-communication-interference-within-the-mogadishu-fir/",
            "published_utc": "2024-02-29T00:00:00Z",
            "category": "Security",
            "confidence": "background",
            "summary": "Communications-interference context. Relevant to RF awareness and coordination, but not direct proof of GNSS jamming.",
            "aoi": aoi,
            "live": False,
        },
        {
            "title": "FAA GPS/GNSS Interference Resource Guide",
            "source": "FAA",
            "url": "https://www.faa.gov/about/office_org/headquarters_offices/avs/offices/afx/afs/afs400/afs410/GNSS/GPS_GNSS_Interference_Resource_Guide.pdf",
            "published_utc": None,
            "category": "Aviation",
            "confidence": "reference",
            "summary": "Operational reference for recognising, reporting and responding to GPS/GNSS interference.",
            "aoi": aoi,
            "live": False,
        },
    ]


def _j2_query_variants(aoi: str) -> list[str]:
    base = (aoi or _J2_DEFAULT_AOI).strip()
    terms = [
        f'{base} GNSS jamming GPS interference aviation',
        f'{base} GPS jamming spoofing GNSS',
        'Mogadishu FIR GNSS jamming spoofing GPS interference',
        'Somalia GNSS jamming GPS interference aviation',
        'HCSM Mogadishu GNSS jamming spoofing',
    ]
    seen: set[str] = set()
    out: list[str] = []
    for term in terms:
        key = term.lower()
        if key not in seen:
            seen.add(key)
            out.append(term)
    return out


def _j2_fetch_news_rss(query: str, limit: int) -> tuple[list[dict[str, _J2Any]], str | None]:
    rss_url = "https://news.google.com/rss/search?" + _j2_urllib_parse.urlencode({"q": query, "hl": "en-GB", "gl": "GB", "ceid": "GB:en"})
    req = _j2_urllib_request.Request(rss_url, headers={"User-Agent": "EEI-LANTERN-J2/0.11.3"})
    try:
        with _j2_urllib_request.urlopen(req, timeout=7) as resp:
            raw = resp.read(900000)
        root = _j2_ET.fromstring(raw)
        items: list[dict[str, _J2Any]] = []
        for item in root.findall(".//item")[: max(1, int(limit))]:
            title = _j2_clean_text(item.findtext("title"))
            url = _j2_clean_text(item.findtext("link"))
            pub = _j2_pub_to_iso(item.findtext("pubDate"))
            desc = _j2_clean_text(item.findtext("description"))
            source_el = item.find("source")
            source = _j2_clean_text(source_el.text if source_el is not None else "Google News") or "Google News"
            if not title or not url:
                continue
            items.append({
                "title": title,
                "source": source,
                "url": url,
                "published_utc": pub,
                "category": "Live OSINT",
                "confidence": "live-feed",
                "summary": desc[:520],
                "query": query,
                "live": True,
            })
        return items, None
    except Exception as exc:
        return [], str(exc)


def _j2_fetch_live_articles(aoi: str, limit: int) -> tuple[list[dict[str, _J2Any]], list[str]]:
    items: list[dict[str, _J2Any]] = []
    errors: list[str] = []
    for query in _j2_query_variants(aoi):
        got, err = _j2_fetch_news_rss(query, max(3, min(10, int(limit))))
        if err:
            errors.append(f"{query}: {err}")
        items.extend(got)
        if len(items) >= int(limit):
            break
    return items, errors[:5]


def _j2_dedupe_articles(items: list[dict[str, _J2Any]], limit: int = 20) -> list[dict[str, _J2Any]]:
    seen: set[str] = set()
    out: list[dict[str, _J2Any]] = []
    for item in items:
        url = str(item.get("url") or "").strip()
        title = str(item.get("title") or "").strip()
        key = (url or title).lower()
        if not key or key in seen:
            continue
        seen.add(key)
        if "aoi" not in item:
            item["aoi"] = _J2_DEFAULT_AOI
        out.append(item)
        if len(out) >= int(limit):
            break
    return out


def _j2_news_payload(aoi: str, live: bool, limit: int, force: bool = False) -> dict[str, _J2Any]:
    aoi = (aoi or _J2_DEFAULT_AOI).strip() or _J2_DEFAULT_AOI
    limit = max(1, min(int(limit), 50))
    cache_path = _j2_news_cache_path(aoi)
    cached = _j2_read_json(cache_path)
    cache_age = None
    if cached:
        try:
            cache_age = int(_j2_time.time() - float(cached.get("cache_epoch", 0)))
        except Exception:
            cache_age = None
    if cached and not force and (not live or (cache_age is not None and cache_age < _J2_CACHE_SECONDS)):
        cached["cache_used"] = True
        cached["cache_age_seconds"] = cache_age
        return cached

    live_items: list[dict[str, _J2Any]] = []
    live_errors: list[str] = []
    if live:
        live_items, live_errors = _j2_fetch_live_articles(aoi, limit=limit)

    cached_items = cached.get("articles", []) if isinstance(cached, dict) else []
    register_items = _j2_source_register(aoi)
    items = _j2_dedupe_articles(live_items + cached_items + register_items, limit=limit)
    live_count = sum(1 for item in items if item.get("live"))
    payload = {
        "status": "ok",
        "j2_api_version": _J2_API_VERSION,
        "generated_utc": _j2_now(),
        "cache_epoch": _j2_time.time(),
        "aoi": aoi,
        "live_attempted": bool(live),
        "live_count": live_count,
        "live_errors": live_errors,
        "cache_used": False,
        "cache_age_seconds": None,
        "count": len(items),
        "articles": items,
        "source_log": [
            {"source": item.get("source"), "url": item.get("url"), "confidence": item.get("confidence"), "category": item.get("category"), "title": item.get("title")}
            for item in items
        ],
        "message": "Live OSINT article feed refreshed." if live_count else "Live article feed unavailable or empty; using cached/source-register links.",
        "limitations": [
            "OSINT feeds are indicators, not local receiver measurements.",
            "Use MOTH scan data and equipment GNSS telemetry for measured serviceability decisions.",
            "External feeds may be blocked or stale; preserve source links with the briefing pack.",
        ],
    }
    _j2_write_json(cache_path, payload)
    return payload


@app.get("/api/j2/health")  # type: ignore[name-defined]
def api_j2_health() -> dict[str, _J2Any]:
    static = _j2_static_dir()
    return {
        "status": "ok",
        "j2_api_version": _J2_API_VERSION,
        "page": "/static/j2_report.html?v=113",
        "cache_dir": str(_j2_cache_dir()),
        "static_j2_report_html": (static / "j2_report.html").exists(),
        "rotate_seconds": _J2_DEFAULT_ROTATE_SECONDS,
        "live_refresh_minutes": _J2_DEFAULT_LIVE_REFRESH_MINUTES,
        "endpoints": [
            "/api/j2/news",
            "/api/j2/articles",
            "/api/j2/report",
            "/api/j2/cache",
            "/api/j2/report.html",
            "/api/j2/export.csv",
            "/api/mission/report-jsp101.html",
        ],
    }


@app.get("/api/j2/status")  # type: ignore[name-defined]
def api_j2_status() -> dict[str, _J2Any]:
    return api_j2_health()


@app.get("/api/j2/news")  # type: ignore[name-defined]
def api_j2_news(
    aoi: str = _J2_DEFAULT_AOI,
    live: bool = True,
    force: bool = False,
    limit: int = _J2Query(default=20, ge=1, le=50),
) -> dict[str, _J2Any]:
    return _j2_news_payload(aoi=aoi, live=bool(live), limit=int(limit), force=bool(force))


@app.get("/api/j2/articles")  # type: ignore[name-defined]
def api_j2_articles(
    aoi: str = _J2_DEFAULT_AOI,
    live: bool = True,
    force: bool = False,
    limit: int = _J2Query(default=20, ge=1, le=50),
) -> dict[str, _J2Any]:
    return api_j2_news(aoi=aoi, live=live, force=force, limit=limit)


@app.post("/api/j2/news")  # type: ignore[name-defined]
def api_j2_news_post(
    aoi: str = _J2_DEFAULT_AOI,
    live: bool = True,
    force: bool = False,
    limit: int = 20,
) -> dict[str, _J2Any]:
    return _j2_news_payload(aoi=aoi, live=bool(live), limit=int(limit), force=bool(force))


def _j2_assessment_from_metrics(metrics: dict[str, _J2Any], news_payload: dict[str, _J2Any]) -> dict[str, _J2Any]:
    valid = int(metrics.get("valid_event_count") or 0)
    gnss_events = int(metrics.get("gnss_event_count") or 0)
    strong_gnss = int(metrics.get("strong_gnss_spike_count") or 0)
    article_count = int(news_payload.get("count") or 0)
    live_count = int(news_payload.get("live_count") or 0)

    if valid <= 0:
        overall = "NO DATA"
        gnss_rf = "NO DATA"
        confidence = "LOW"
        headline = "No local RF data loaded; J2 view is OSINT/context only."
        judgement = "Do not infer GNSS serviceability from this page until local MOTH RF data and equipment GNSS telemetry are available."
        action = "Load/select MOTH collections, confirm equipment GPS receiver health, then refresh the J2 report."
    elif strong_gnss > 0:
        overall = "CHECK"
        gnss_rf = "ELEVATED"
        confidence = "MEDIUM" if valid >= 500 else "LOW"
        headline = f"Strong GNSS-window RF activity observed: {strong_gnss} event(s) at or above -60 dBm."
        judgement = "Treat the GNSS environment as potentially degraded until receiver health and independent checks remain stable."
        action = "Review L1/L2/L5 bands, check receiver fix/satellite/HDOP-PDOP health, and compare with current OSINT before task execution."
    elif gnss_events > 0:
        overall = "CHECK"
        gnss_rf = "WATCH"
        confidence = "MEDIUM" if valid >= 500 else "LOW"
        headline = "GNSS-window RF activity observed without strong -60 dBm spikes in the selected data."
        judgement = "Lower apparent RF burden is useful, but it is not proof of GNSS receiver integrity."
        action = "Use as RF decision-support only; continue equipment receiver validation and source checks."
    else:
        overall = "WATCH"
        gnss_rf = "LOW OBSERVED"
        confidence = "MEDIUM" if valid >= 500 else "LOW"
        headline = "No GNSS-window activity observed in the selected local data."
        judgement = "This may indicate a quieter observed window, but no detection does not prove the band was clear."
        action = "Maintain receiver validation and repeat checks across the intended task period."

    source_status = "LIVE" if live_count else "SOURCE REGISTER"
    security = "CHECK" if article_count else "NO DATA"
    aviation = "CHECK" if article_count else "NO DATA"
    if live_count <= 0 and article_count:
        confidence = "LOW" if valid <= 0 else confidence

    cards = [
        {"key": "overall", "label": "OVERALL J2", "value": overall, "detail": headline},
        {"key": "gnss_rf", "label": "GNSS/RF", "value": gnss_rf, "detail": f"GNSS events: {gnss_events}; strong GNSS spikes >= -60 dBm: {strong_gnss}."},
        {"key": "security", "label": "SECURITY", "value": security, "detail": "Conflict-zone / RF interference context requires current source checks."},
        {"key": "aviation", "label": "AVIATION", "value": aviation, "detail": "Aviation relevance requires official/source-register checks and local operating approval."},
        {"key": "source", "label": "SOURCE FEED", "value": source_status, "detail": f"Live articles: {live_count}; total links: {article_count}."},
        {"key": "confidence", "label": "CONFIDENCE", "value": confidence, "detail": f"Local valid RF rows: {valid}; source items: {article_count}."},
    ]

    return {
        "overall_j2": overall,
        "gnss_rf": gnss_rf,
        "security": security,
        "aviation": aviation,
        "source_status": source_status,
        "confidence": confidence,
        "headline": headline,
        "current_judgement": judgement,
        "recommended_action": action,
        "cards": cards,
    }


@app.get("/api/j2/report")  # type: ignore[name-defined]
def api_j2_report(
    aoi: str = _J2_DEFAULT_AOI,
    collection_id: int | None = None,
    collection_ids: str | None = None,
    live: bool = True,
    force: bool = False,
    limit: int = _J2Query(default=20, ge=1, le=50),
) -> dict[str, _J2Any]:
    metrics = _j2_local_metrics(collection_id=collection_id, collection_ids=collection_ids)
    news_payload = _j2_news_payload(aoi=aoi, live=bool(live), limit=int(limit), force=bool(force))
    assessment = _j2_assessment_from_metrics(metrics, news_payload)
    sections = {
        "j2_summary": [
            assessment["headline"],
            assessment["current_judgement"],
            assessment["recommended_action"],
        ],
        "gnss_rf": [
            f"Valid local RF rows: {metrics.get('valid_event_count', 0)}.",
            f"GNSS-window events: {metrics.get('gnss_event_count', 0)}.",
            f"Strong GNSS-window detections >= -60 dBm: {metrics.get('strong_gnss_spike_count', 0)}.",
            "Interpret MOTH activity as observed RF evidence, not continuous calibrated spectrum truth.",
        ],
        "threat_actors": [
            "Do not attribute interference to a threat actor unless the cited source explicitly supports it.",
            "Separate local RF measurements from public-source attribution language.",
            "Use the article list/source log for awareness and traceability, not for unsupported attribution.",
        ],
        "aviation": [
            "Check official aeronautical information, current NOTAM/eAIP status and local coordination notes before relying on GPS.",
            "Use OSINT indicators alongside equipment receiver checks and authorised safety case controls.",
            "Public ADS-B derived interference indicators are not direct local receiver measurements.",
        ],
    }
    payload = {
        "status": "ok",
        "j2_api_version": _J2_API_VERSION,
        "generated_utc": _j2_now(),
        "aoi": aoi,
        "selected_collection_ids": metrics.get("selected_collection_ids", []),
        **assessment,
        "metrics": metrics,
        "news": news_payload,
        "articles": news_payload.get("articles", []),
        "sections": sections,
        "jsp101_report_url": "/api/mission/report-jsp101.html",
        "safety_boundary": "Decision-support only. Does not authorise flight, certify GNSS performance, or provide tactical targeting intelligence.",
        "limitations": [
            "MOTH data is event-based. No detection does not prove the band was empty.",
            "OSINT article feeds are external indicators, not local receiver measurements.",
            "Final serviceability requires equipment receiver checks and authorised operating constraints.",
        ],
    }
    _j2_write_json(_j2_report_cache_path(), payload)
    return payload


@app.post("/api/j2/report")  # type: ignore[name-defined]
def api_j2_report_post(
    aoi: str = _J2_DEFAULT_AOI,
    collection_id: int | None = None,
    collection_ids: str | None = None,
    live: bool = True,
    force: bool = False,
    limit: int = 20,
) -> dict[str, _J2Any]:
    return api_j2_report(aoi=aoi, collection_id=collection_id, collection_ids=collection_ids, live=live, force=force, limit=limit)


@app.get("/api/j2/cache")  # type: ignore[name-defined]
def api_j2_cache() -> dict[str, _J2Any]:
    cached = _j2_read_json(_j2_report_cache_path())
    if cached:
        cached["cache_used"] = True
        return cached
    return api_j2_report(live=False)


@app.get("/api/j2/live-report")  # type: ignore[name-defined]
def api_j2_live_report(aoi: str = _J2_DEFAULT_AOI, collection_ids: str | None = None) -> dict[str, _J2Any]:
    return api_j2_report(aoi=aoi, collection_ids=collection_ids, live=True, force=True)


@app.get("/api/j2/report.json")  # type: ignore[name-defined]
def api_j2_report_json(aoi: str = _J2_DEFAULT_AOI, collection_ids: str | None = None) -> dict[str, _J2Any]:
    return api_j2_report(aoi=aoi, collection_ids=collection_ids, live=True, force=False)


@app.get("/api/j2/report.html", response_class=_J2HTMLResponse)  # type: ignore[name-defined]
def api_j2_report_html() -> str:
    data = api_j2_cache()
    def esc(x: _J2Any) -> str:
        return _j2_html.escape("" if x is None else str(x))
    articles = data.get("articles") or []
    rows = "".join(
        f"<tr><td>{esc(a.get('source'))}</td><td><a href='{esc(a.get('url'))}'>{esc(a.get('title'))}</a><br><small>{esc(a.get('summary'))}</small></td><td>{esc(a.get('published_utc'))}</td><td>{esc(a.get('category'))}</td><td>{esc(a.get('confidence'))}</td></tr>"
        for a in articles[:30]
    )
    cards = "".join(
        f"<tr><td>{esc(c.get('label'))}</td><td>{esc(c.get('value'))}</td><td>{esc(c.get('detail'))}</td></tr>"
        for c in data.get("cards", [])
    )
    return f"""
<!doctype html><html><head><meta charset='utf-8'><title>J2 Live Report Export</title>
<style>body{{font-family:Arial,sans-serif;max-width:1100px;margin:24px auto;color:#111}}table{{width:100%;border-collapse:collapse;margin:12px 0}}td,th{{border:1px solid #bbb;padding:6px;text-align:left;vertical-align:top}}th{{background:#17365d;color:#fff}}.warn{{border-left:5px solid #b42318;padding:8px;background:#fff8f8}}small{{color:#555}}@media print{{button{{display:none}}}}</style></head><body>
<button onclick='window.print()'>Print / save PDF</button>
<h1>EEI LANTERN J2 Live Report</h1>
<p>Generated UTC: {esc(data.get('generated_utc'))} | AOI: {esc(data.get('aoi'))}</p>
<h2>Executive summary</h2><p><b>{esc(data.get('headline'))}</b></p><p>{esc(data.get('current_judgement'))}</p><p><b>Recommended action:</b> {esc(data.get('recommended_action'))}</p>
<h2>Cards</h2><table><tr><th>Card</th><th>Value</th><th>Detail</th></tr>{cards}</table>
<h2>Article/source log</h2><table><tr><th>Source</th><th>Article/source</th><th>Published</th><th>Category</th><th>Confidence</th></tr>{rows}</table>
<div class='warn'><h2>Safety boundary</h2><p>{esc(data.get('safety_boundary'))}</p></div>
</body></html>
"""


@app.get("/api/j2/export.csv")  # type: ignore[name-defined]
def api_j2_export_csv() -> _J2Response:
    data = api_j2_cache()
    out = _j2_io.StringIO()
    writer = _j2_csv.writer(out)
    writer.writerow(["generated_utc", "aoi", "section", "source", "title", "published_utc", "category", "confidence", "url", "summary"])
    for item in data.get("articles") or []:
        writer.writerow([
            data.get("generated_utc"),
            data.get("aoi"),
            "article",
            item.get("source"),
            item.get("title"),
            item.get("published_utc"),
            item.get("category"),
            item.get("confidence"),
            item.get("url"),
            item.get("summary"),
        ])
    return _J2Response(out.getvalue(), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=lantern_j2_source_log.csv"})


@app.get("/api/j2/live")  # type: ignore[name-defined]
def api_j2_live_alias(aoi: str = _J2_DEFAULT_AOI, collection_ids: str | None = None) -> dict[str, _J2Any]:
    return api_j2_report(aoi=aoi, collection_ids=collection_ids, live=True, force=True)


@app.get("/api/j2/summary")  # type: ignore[name-defined]
def api_j2_summary_alias(aoi: str = _J2_DEFAULT_AOI, collection_ids: str | None = None) -> dict[str, _J2Any]:
    return api_j2_report(aoi=aoi, collection_ids=collection_ids, live=False, force=False)

# ---- end EEI LANTERN v0.11.3 J2 Live Article Rotator API ----

# ---- EEI LANTERN v0.11.4a J2 Threat Actors endpoint hotfix ----
# Restores /api/j2/threat-actors when the v0.11.4 static J2 page is present but the route was not registered.
# This is deliberately self-contained and uses existing /api/j2/news when available.

import html as _j2hf_html
import re as _j2hf_re
import urllib.parse as _j2hf_urlparse
import urllib.request as _j2hf_urlrequest
import xml.etree.ElementTree as _j2hf_ET
from datetime import datetime as _j2hf_datetime, timezone as _j2hf_timezone
from email.utils import parsedate_to_datetime as _j2hf_parsedate_to_datetime
from typing import Any as _J2HFAny

from fastapi import Query as _J2HFQuery

_J2HF_VERSION = "0.11.4a"
_J2HF_DEFAULT_AOI = "Aden Adde / Mogadishu"


def _j2hf_now() -> str:
    return _j2hf_datetime.now(_j2hf_timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _j2hf_clean_text(value: _J2HFAny) -> str:
    if value is None:
        return ""
    text = str(value)
    text = _j2hf_re.sub(r"<[^>]+>", " ", text)
    text = _j2hf_html.unescape(text)
    return _j2hf_re.sub(r"\s+", " ", text).strip()


def _j2hf_pub_to_iso(value: _J2HFAny) -> str | None:
    text = _j2hf_clean_text(value)
    if not text:
        return None
    try:
        dt = _j2hf_parsedate_to_datetime(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_j2hf_timezone.utc)
        return dt.astimezone(_j2hf_timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    except Exception:
        return text


def _j2hf_source_register(aoi: str = _J2HF_DEFAULT_AOI) -> list[dict[str, _J2HFAny]]:
    return [
        {
            "title": "EASA Somalia conflict-zone information bulletin",
            "source": "EASA CZIB",
            "url": "https://www.easa.europa.eu/en/domains/air-operations/czibs/czib-2017-05r19",
            "published_utc": None,
            "category": "Aviation / Security",
            "confidence": "official-source",
            "summary": "Official conflict-zone aviation context. Use for operating constraints, NOTAM/coordination checks and conservative planning around Somali airspace.",
            "aoi": aoi,
            "live": False,
        },
        {
            "title": "UN Security Council Somalia / Al-Shabaab sanctions material",
            "source": "UN Security Council",
            "url": "https://main.un.org/securitycouncil/en/sanctions/2713",
            "published_utc": None,
            "category": "Threat Actor / Official",
            "confidence": "official-source",
            "summary": "Official sanctions/source family for Somalia and Al-Shabaab context. Use for traceable actor background, not tactical attribution.",
            "aoi": aoi,
            "live": False,
        },
        {
            "title": "US State Department Somalia travel advisory",
            "source": "US Department of State",
            "url": "https://travel.state.gov/content/travel/en/traveladvisories/traveladvisories/somalia-travel-advisory.html",
            "published_utc": None,
            "category": "Threat Actor / Security",
            "confidence": "official-source",
            "summary": "Official public security advisory that separately references Al-Shabaab and ISIS-Somalia risk context. Use as strategic safety context.",
            "aoi": aoi,
            "live": False,
        },
        {
            "title": "EUAA Somalia security situation and main actors",
            "source": "EUAA",
            "url": "https://www.euaa.europa.eu/coi/somalia/2025/security-situation/12-armed-actors-and-relevant-developments/123-updated-list-main-actors",
            "published_utc": None,
            "category": "Threat Actor / Security",
            "confidence": "source-register",
            "summary": "Structured country-of-origin reporting on state and non-state actors, useful for separating actor types and geographic relevance.",
            "aoi": aoi,
            "live": False,
        },
        {
            "title": "Crisis Group - Islamic State-Somalia: Responding to an Evolving Threat",
            "source": "International Crisis Group",
            "url": "https://www.crisisgroup.org/brf/africa/somalia/b201-islamic-state-somalia-responding-evolving-threat",
            "published_utc": None,
            "category": "Threat Actor / ISIS-Somalia",
            "confidence": "source-register",
            "summary": "Analytical source-register item for ISIS-Somalia / Islamic State-Somalia context. Usually more relevant to Puntland/Bari unless current reporting links it to the AOI.",
            "aoi": aoi,
            "live": False,
        },
        {
            "title": "Critical Threats - Al-Shabaab area of operations",
            "source": "Critical Threats",
            "url": "https://www.criticalthreats.org/analysis/al-shabaabs-area-of-operations",
            "published_utc": None,
            "category": "Threat Actor / Al-Shabaab",
            "confidence": "source-register",
            "summary": "Public analytical source for Al-Shabaab operating geography and activity context. Use as a watch item, not a standalone decision source.",
            "aoi": aoi,
            "live": False,
        },
        {
            "title": "Somalia Civil Aviation Authority eAIP - HCMM Aden Adde Intl",
            "source": "SCAA eAIP",
            "url": "https://aip.scaa.gov.so/eAIP/HC-AD-2.HCMM-en-GB.html",
            "published_utc": None,
            "category": "Aviation / Official",
            "confidence": "official-source",
            "summary": "Airport facts, procedures, local operating constraints and official aeronautical context for Aden Adde / HCMM.",
            "aoi": aoi,
            "live": False,
        },
        {
            "title": "IFALPA/IFATCA - Unlawful Communication Interference within the Mogadishu FIR",
            "source": "IFALPA / IFATCA",
            "url": "https://ifatca.org/unlawful-communication-interference-within-the-mogadishu-fir/",
            "published_utc": None,
            "category": "RF / Communications",
            "confidence": "source-register",
            "summary": "Public aviation safety bulletin for unlawful VHF communications interference. Relevant to RF awareness but not proof of GNSS jamming.",
            "aoi": aoi,
            "live": False,
        },
    ]


def _j2hf_query_terms(aoi: str) -> list[str]:
    base = (aoi or _J2HF_DEFAULT_AOI).strip()
    return [
        f"Al-Shabaab Somalia Mogadishu Aden Adde security aviation {base}",
        f"ISIS-Somalia OR Islamic State Somalia Puntland Mogadishu {base}",
        f"Somalia Mogadishu airport security Al-Shabaab ISIS {base}",
        f"Somalia conflict zone aviation Mogadishu FIR Al-Shabaab {base}",
    ]


def _j2hf_google_news_rss_url(query: str) -> str:
    q = _j2hf_urlparse.quote(query)
    return f"https://news.google.com/rss/search?q={q}&hl=en-GB&gl=GB&ceid=GB:en"


def _j2hf_fetch_news_rss(query: str, limit: int = 10) -> list[dict[str, _J2HFAny]]:
    url = _j2hf_google_news_rss_url(query)
    req = _j2hf_urlrequest.Request(url, headers={"User-Agent": "EEI-LANTERN-J2/0.11.4a"})
    try:
        with _j2hf_urlrequest.urlopen(req, timeout=8) as resp:
            raw = resp.read(750_000)
    except Exception:
        return []
    try:
        root = _j2hf_ET.fromstring(raw)
    except Exception:
        return []
    out: list[dict[str, _J2HFAny]] = []
    for item in root.findall(".//item"):
        title = _j2hf_clean_text(item.findtext("title"))
        link = _j2hf_clean_text(item.findtext("link"))
        pub = _j2hf_pub_to_iso(item.findtext("pubDate"))
        source_el = item.find("source")
        source = _j2hf_clean_text(source_el.text if source_el is not None else "Google News")
        desc = _j2hf_clean_text(item.findtext("description"))
        if not title or not link:
            continue
        out.append({
            "title": title,
            "source": source or "Google News",
            "url": link,
            "published_utc": pub,
            "category": _j2hf_category_for_text(title + " " + desc),
            "confidence": "live-news",
            "summary": desc[:360] if desc else "Live news/search result. Verify source details before briefing.",
            "aoi": _J2HF_DEFAULT_AOI,
            "live": True,
        })
        if len(out) >= int(limit):
            break
    return out


def _j2hf_category_for_text(text: str) -> str:
    t = (text or "").lower()
    if any(k in t for k in ["isis", "islamic state", "daesh"]):
        return "Threat Actor / ISIS-Somalia"
    if any(k in t for k in ["al-shabaab", "al shabaab", "shabaab"]):
        return "Threat Actor / Al-Shabaab"
    if any(k in t for k in ["airport", "aviation", "airspace", "mogadishu fir", "advisory", "notam"]):
        return "Aviation / Security"
    if any(k in t for k in ["jamming", "spoofing", "gnss", "gps", "rf", "vhf"]):
        return "GNSS/RF"
    return "Security / Regional"


def _j2hf_existing_news(aoi: str, live: bool, force: bool, limit: int) -> list[dict[str, _J2HFAny]]:
    fn = globals().get("api_j2_news")
    if callable(fn):
        try:
            payload = fn(aoi=aoi, live=live, force=force, limit=limit)  # type: ignore[misc]
            return list(payload.get("articles") or [])
        except TypeError:
            try:
                payload = fn(live=live, force=force, limit=limit)  # type: ignore[misc]
                return list(payload.get("articles") or [])
            except Exception:
                return []
        except Exception:
            return []
    return []


def _j2hf_dedupe(items: list[dict[str, _J2HFAny]], limit: int) -> list[dict[str, _J2HFAny]]:
    seen: set[str] = set()
    out: list[dict[str, _J2HFAny]] = []
    for item in items:
        url = str(item.get("url") or "").strip()
        title = _j2hf_clean_text(item.get("title"))
        key = (url or title).lower()
        if not key or key in seen:
            continue
        seen.add(key)
        item = dict(item)
        item.setdefault("category", _j2hf_category_for_text(title + " " + str(item.get("summary") or "")))
        item.setdefault("confidence", "source-register")
        item.setdefault("live", False)
        out.append(item)
        if len(out) >= int(limit):
            break
    return out


def _j2hf_threat_articles(aoi: str, live: bool, force: bool, limit: int) -> list[dict[str, _J2HFAny]]:
    live_items: list[dict[str, _J2HFAny]] = []
    if live:
        per_query = max(4, min(12, int(limit) // 3))
        for query in _j2hf_query_terms(aoi):
            live_items.extend(_j2hf_fetch_news_rss(query, limit=per_query))
    existing = _j2hf_existing_news(aoi, live, force, max(int(limit), 20))
    threatish_existing = []
    for item in existing:
        text = " ".join(str(item.get(k) or "") for k in ("title", "summary", "category", "source"))
        cat = _j2hf_category_for_text(text)
        if cat.startswith("Threat Actor") or "security" in cat.lower() or "aviation" in cat.lower():
            copied = dict(item)
            copied["category"] = cat if copied.get("category") in (None, "", "Security / Regional") else copied.get("category")
            threatish_existing.append(copied)
    return _j2hf_dedupe(live_items + threatish_existing + _j2hf_source_register(aoi), limit=max(int(limit), 8))


def _j2hf_actor_catalog(aoi: str) -> list[dict[str, _J2HFAny]]:
    return [
        {
            "actor": "Al-Shabaab",
            "type": "Non-state armed group / insurgent-terrorist actor",
            "area_relevance": "Primary south-central Somalia and Mogadishu watch line for airport-area security context.",
            "current_assessment": "Monitor for attack reporting, indirect fire, IED/security incidents, checkpoints, airport-access disruption and wider Mogadishu security changes before task execution.",
            "j2_relevance": "Primary actor watch for Aden Adde / Mogadishu operating context. Relevant to ground movement, permissions, force protection and airport-area restrictions.",
            "aviation_rf_relevance": "Relevant to aviation/security posture. Do not attribute GNSS/RF interference to Al-Shabaab unless a cited source explicitly supports that attribution.",
            "confidence": "SOURCE-LINKED",
        },
        {
            "actor": "ISIS-Somalia / Islamic State Somalia / Daesh-Somalia",
            "type": "Non-state armed group / terrorist actor",
            "area_relevance": "Usually a Puntland/Bari/Golis/Cal Miskaad watch line unless current reporting links activity to Mogadishu or the AOI.",
            "current_assessment": "Track current reporting separately from Al-Shabaab. Do not merge ISIS-Somalia reporting into the Mogadishu airport picture unless the source is geographically relevant.",
            "j2_relevance": "Secondary actor watch. Important for Somalia threat context and external reporting, but must be geographically checked against the AOI.",
            "aviation_rf_relevance": "Indirect security relevance. No automatic RF/GNSS attribution without explicit source support.",
            "confidence": "SOURCE-LINKED",
        },
        {
            "actor": "Somalia Federal Government / regional administrations / AUSSOM and security partners",
            "type": "State/security operating context",
            "area_relevance": "Permissions, checkpoints, cordons, military/security operations, airport access and official operating restrictions.",
            "current_assessment": "Track official coordination notes, SCAA/eAIP/NOTAMs, local approvals and security-force activity affecting the worksite and airport access routes.",
            "j2_relevance": "Not a hostile actor bucket. This is the state/security context required for safe and authorised operating decisions.",
            "aviation_rf_relevance": "Direct relevance to operating permission, local restrictions and safety case controls.",
            "confidence": "OFFICIAL/SOURCE-LINKED",
        },
        {
            "actor": "External state / regional actors",
            "type": "Regional state and proxy-context watch line",
            "area_relevance": "Regional diplomatic, military or security activity may alter operating constraints or aviation/security posture.",
            "current_assessment": "Use only source-linked reporting. Avoid hostile-state attribution unless the source explicitly makes that claim and is appropriate for the briefing level.",
            "j2_relevance": "Contextual watch line for changes in the wider operating environment; not a default explanation for local RF anomalies.",
            "aviation_rf_relevance": "Potential indirect relevance to airspace/security posture. No automatic link to GNSS/RF interference.",
            "confidence": "LOW TO MEDIUM - SOURCE DEPENDENT",
        },
        {
            "actor": "Unknown RF/GNSS interference source",
            "type": "Unattributed technical/risk bucket",
            "area_relevance": "Used when MOTH/GNSS anomalies exist but no source-supported actor attribution exists.",
            "current_assessment": "Keep RF observations separate from actor attribution. Strong GNSS-band detections show RF risk; they do not identify who caused it.",
            "j2_relevance": "Prevents unsupported attribution. Escalate only when technical evidence and source reporting support a specific claim.",
            "aviation_rf_relevance": "Directly relevant to GNSS serviceability checks, receiver validation and RF risk reporting.",
            "confidence": "TECHNICAL OBSERVATION ONLY",
        },
    ]


def _j2hf_payload(aoi: str, live: bool, force: bool, limit: int) -> dict[str, _J2HFAny]:
    articles = _j2hf_threat_articles(aoi, live, force, limit)
    live_count = sum(1 for a in articles if bool(a.get("live")))
    actors = _j2hf_actor_catalog(aoi)
    al_count = sum(1 for a in articles if "shabaab" in str(a.get("title", "") + " " + a.get("summary", "")).lower())
    isis_count = sum(1 for a in articles if any(k in str(a.get("title", "") + " " + a.get("summary", "")).lower() for k in ["isis", "islamic state", "daesh"]))
    official_count = sum(1 for a in articles if "official" in str(a.get("confidence", "")).lower() or str(a.get("source", "")).lower() in {"easa", "easa czib", "un security council", "us department of state", "scaa eaip"})
    confidence = "LIVE" if live_count else "SOURCE-REGISTER"
    return {
        "status": "ok",
        "j2_api_version": _J2HF_VERSION,
        "generated_utc": _j2hf_now(),
        "aoi": aoi,
        "confidence": confidence,
        "live_attempted": bool(live),
        "live_count": live_count,
        "count": len(articles),
        "message": "Live threat actor article feed refreshed." if live_count else "Live threat actor feed unavailable or empty; using source-register links.",
        "actors": actors,
        "articles": articles,
        "source_log": [
            {
                "title": a.get("title"),
                "source": a.get("source"),
                "url": a.get("url"),
                "category": a.get("category"),
                "confidence": a.get("confidence"),
                "published_utc": a.get("published_utc"),
                "live": bool(a.get("live")),
            }
            for a in articles
        ],
        "brief_lines": [
            f"Primary actor watch: Al-Shabaab remains the main Mogadishu / Aden Adde security-context watch line. Source-linked items in feed: {al_count}.",
            f"Secondary actor watch: ISIS-Somalia / Islamic State Somalia is tracked separately and should be geographically checked before applying it to the AOI. Source-linked items in feed: {isis_count}.",
            "State/security context: track SCAA/eAIP/NOTAMs, airport permission, checkpoint/cordon changes, and current security-force operations before task execution.",
            "RF/GNSS attribution boundary: MOTH detections and receiver symptoms can indicate RF risk, but they do not identify an actor without separate source-supported attribution.",
            f"Source basis: {len(articles)} article/source item(s), {live_count} live item(s), {official_count} official/source-register item(s).",
        ],
        "attribution_boundary": "Do not attribute GNSS jamming, spoofing, VHF interference, or strong RF detections to Al-Shabaab, ISIS-Somalia, a state actor, or any other actor unless a cited source explicitly supports that attribution. Keep RF observations and J2 actor assessment separated.",
        "limitations": [
            "This endpoint provides public-source J2 awareness and source links. It is not tactical targeting intelligence.",
            "Live news search can fail because of network, proxy, or provider restrictions; source-register links remain available offline.",
            "Actor activity, airport restrictions and official advisories change quickly; refresh before briefing and verify critical points at source.",
        ],
    }


@app.get("/api/j2/threat-actors")  # type: ignore[name-defined]
def api_j2_threat_actors_hotfix(
    aoi: str = _J2HF_DEFAULT_AOI,
    live: bool = True,
    force: bool = False,
    limit: int = _J2HFQuery(default=30, ge=1, le=80),
) -> dict[str, _J2HFAny]:
    return _j2hf_payload(aoi=aoi, live=bool(live), force=bool(force), limit=int(limit))


@app.get("/api/j2/threat_actors")  # type: ignore[name-defined]
def api_j2_threat_actors_hotfix_alias(
    aoi: str = _J2HF_DEFAULT_AOI,
    live: bool = True,
    force: bool = False,
    limit: int = _J2HFQuery(default=30, ge=1, le=80),
) -> dict[str, _J2HFAny]:
    return api_j2_threat_actors_hotfix(aoi=aoi, live=live, force=force, limit=limit)


@app.get("/api/j2/threat-news")  # type: ignore[name-defined]
def api_j2_threat_news_hotfix(
    aoi: str = _J2HF_DEFAULT_AOI,
    live: bool = True,
    force: bool = False,
    limit: int = _J2HFQuery(default=30, ge=1, le=80),
) -> dict[str, _J2HFAny]:
    payload = api_j2_threat_actors_hotfix(aoi=aoi, live=live, force=force, limit=limit)
    return {
        "status": "ok",
        "j2_api_version": _J2HF_VERSION,
        "generated_utc": payload.get("generated_utc"),
        "aoi": aoi,
        "live_count": payload.get("live_count", 0),
        "count": payload.get("count", 0),
        "articles": payload.get("articles", []),
        "source_log": payload.get("source_log", []),
    }

# ---- end EEI LANTERN v0.11.4a J2 Threat Actors endpoint hotfix ----

# ---- EEI LANTERN v0.11.6 J2 platform status compatibility layer ----
# Purpose: keep the platform status panel working after rollback of the mistaken v0.11.1 /rotator page.
# This does NOT restore the old /rotator route. The active J2 live article rotator remains /static/j2_report.html.

from typing import Any as _J2StatusAny

try:
    _LANTERN_PLATFORM_VERSION = "0.11.6"  # type: ignore[assignment]
except Exception:
    pass


def _j2_status_static_dir():
    try:
        return STATIC_DIR  # type: ignore[name-defined]
    except Exception:
        from pathlib import Path as _Path
        return _Path(__file__).with_name("static")


def _j2_status_route_exists(path: str) -> bool:
    try:
        for route in app.routes:  # type: ignore[name-defined]
            if getattr(route, "path", None) == path:
                return True
    except Exception:
        pass
    return False


def _j2_status_payload() -> dict[str, _J2StatusAny]:
    static = _j2_status_static_dir()
    j2_page = static / "j2_report.html"
    shell_js = static / "platform_shell.js"
    shell_css = static / "platform_shell.css"
    platform_home = static / "platform_home.html"

    route_checks = {
        "j2_news": _j2_status_route_exists("/api/j2/news"),
        "j2_report": _j2_status_route_exists("/api/j2/report"),
        "j2_health": _j2_status_route_exists("/api/j2/health"),
        "j2_threat_actors": _j2_status_route_exists("/api/j2/threat-actors") or _j2_status_route_exists("/api/j2/threat_actors"),
        "legacy_rotator_route_removed": not _j2_status_route_exists("/rotator"),
    }
    files = {
        "j2_report_html": j2_page.exists(),
        "platform_shell_js": shell_js.exists(),
        "platform_shell_css": shell_css.exists(),
        "platform_home_html": platform_home.exists(),
        "rotator_html_required": False,
        "rotator_html_present": (static / "rotator.html").exists(),
    }

    # This endpoint deliberately treats the old /rotator as not required.
    required_ok = bool(files["j2_report_html"]) and bool(route_checks["j2_news"]) and bool(route_checks["j2_report"])
    if not route_checks["j2_health"]:
        # Older J2 live article builds may not have /api/j2/health; do not fail the page for that alone.
        required_ok = required_ok and True

    return {
        "ok": required_ok,
        "status": "ok" if required_ok else "check",
        "platform_version": globals().get("_LANTERN_PLATFORM_VERSION", "0.11.6"),
        "compatibility_endpoint": True,
        "static_dir": str(static),
        "files": files,
        "routes": route_checks,
        "urls": {
            "platform_home": "/app?v=0116",
            "j2_live_report": "/static/j2_report.html?v=115",
            "j2_static": "/static/j2_report.html?v=115",
            "j2_news_api": "/api/j2/news?live=true&force=true",
            "j2_threat_actor_api": "/api/j2/threat-actors?live=true&force=true",
            "legacy_rotator": "not used",
        },
        "note": "Compatibility response for stale platform pages that still call /api/platform/j2-rotator-check. The old /rotator page remains removed; the correct live article rotator is inside /static/j2_report.html.",
    }


@app.get("/api/platform/j2-live-check")  # type: ignore[name-defined]
def lantern_j2_live_check() -> dict[str, _J2StatusAny]:
    return _j2_status_payload()


@app.get("/api/platform/j2-rotator-check")  # type: ignore[name-defined]
def lantern_j2_rotator_check_compat() -> dict[str, _J2StatusAny]:
    return _j2_status_payload()

# ---- end EEI LANTERN v0.11.6 J2 platform status compatibility layer ----

# ---- EEI LANTERN v0.12.0 Navigation Rationalisation layer ----
# Primary navigation = end-state reporting and decision support.
# Secondary stack = engineering / analyst evidence generation.
# Additive UI routing only. Does not change RF scoring, import quality, J2 feed logic, or GNSS calculations.

from datetime import datetime as _V012DateTime, timezone as _V012Timezone
from pathlib import Path as _V012Path
from typing import Any as _V012Any
import importlib.util as _v012_importlib_util
import json as _v012_json
import sys as _v012_sys

from fastapi.responses import HTMLResponse as _V012HTMLResponse

_LANTERN_PLATFORM_VERSION = "0.12.0"
_V012_START_MARKER = "EEI LANTERN v0.12.0 Navigation Rationalisation layer"


def _v012_now() -> str:
    return _V012DateTime.now(_V012Timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _v012_static_dir() -> _V012Path:
    try:
        return STATIC_DIR  # type: ignore[name-defined]
    except Exception:
        return _V012Path(__file__).with_name("static")


def _v012_static_page(name: str, fallback_title: str = "EEI LANTERN") -> str:
    p = _v012_static_dir() / name
    if p.exists():
        return p.read_text(encoding="utf-8", errors="replace")
    return f"""<!doctype html><html><head><meta charset='utf-8'><title>{fallback_title}</title></head><body><h1>{fallback_title}</h1><p>Missing static page: {name}</p><p><a href='/app?v=012'>Return to LANTERN Home</a></p></body></html>"""


def _v012_route_exists(path: str) -> bool:
    try:
        return any(getattr(route, "path", None) == path for route in app.routes)  # type: ignore[name-defined]
    except Exception:
        return False


def _v012_page_exists(url: str) -> bool:
    if not url:
        return False
    if url.startswith("/api/"):
        return True
    path = url.split("?", 1)[0]
    if path in {"/", "/app", "/context", "/reporting", "/engineering", "/system"}:
        return True
    if path.startswith("/reporting/") or path.startswith("/engineering/") or path.startswith("/system/"):
        return True
    if path.startswith("/static/"):
        return (_v012_static_dir() / path.replace("/static/", "", 1)).exists()
    if path == "/lantern":
        return True
    return _v012_route_exists(path)


def _v012_nav_registry() -> dict[str, _V012Any]:
    primary = [
        {"key": "home", "title": "Home", "url": "/app?v=012", "description": "Platform landing page and workflow map."},
        {"key": "context", "title": "Mission Context", "url": "/context?v=012", "description": "AOI, collections, time window and data quality status."},
        {"key": "reporting", "title": "Reporting", "url": "/reporting?v=012", "description": "End-state flight safety, operations, J2 and export pages."},
        {"key": "engineering", "title": "Engineering", "url": "/engineering?v=012", "description": "Technical evidence, RF analysis, map layers and diagnostics."},
        {"key": "system", "title": "System", "url": "/system?v=012", "description": "Health, deployment checks, logs and app map."},
    ]
    sections = {
        "reporting": [
            {"key": "flight-safety", "title": "Flight Safety Brief", "url": "/reporting/flight-safety?v=012", "legacy_url": "/lantern?v=012", "description": "Pilot-facing GNSS/RF burden and clearest observed constellation/band."},
            {"key": "mission-brief", "title": "Mission Operations Brief", "url": "/reporting/mission-brief?v=012", "legacy_url": "/static/mission_brief.html?v=091", "description": "Senior/ops readiness summary, caveats and decision-support brief."},
            {"key": "j2", "title": "J2 Live Report", "url": "/reporting/j2?v=012", "legacy_url": "/static/j2_report.html?v=115", "description": "OSINT articles, threat actors, aviation/security context and source log."},
            {"key": "gnss-serviceability", "title": "GNSS Serviceability", "url": "/reporting/gnss-serviceability?v=012", "description": "GPS/GNSS serviceability decision support and required receiver checks."},
            {"key": "candidate-report", "title": "Candidate Site Report", "url": "/reporting/candidate?v=012", "description": "End-state antenna/candidate report and PDF entry point."},
            {"key": "evidence-log", "title": "Source / Evidence Log", "url": "/reporting/evidence-log?v=012", "description": "Traceable collections, OSINT/source items and evidence register."},
            {"key": "export-pack", "title": "Export Pack", "url": "/reporting/export?v=012", "description": "Mission report, J2 source log, candidate assessment and archive links."},
        ],
        "engineering": [
            {"key": "data-quality", "title": "Data Quality Detail", "url": "/engineering/data-quality?v=012", "legacy_url": "/static/data_quality.html?v=080", "description": "Import quality, scan coverage, reject/flag detail and confidence."},
            {"key": "rf", "title": "RF Analyst", "url": "/engineering/rf?v=012", "legacy_url": "/static/launch_analysis.html?v=075", "description": "Launch windows, L1/L2/L5 timelines, spectrum/spikes and pattern-of-life."},
            {"key": "spectrum", "title": "Spectrum / Spikes", "url": "/engineering/spectrum?v=012", "legacy_url": "/static/launch_analysis.html?v=075#spectrum", "description": "Technical spectrum bins, dBm values, busy frequencies and abnormal spikes."},
            {"key": "map", "title": "Map / H3 Layers", "url": "/engineering/map?v=012", "legacy_url": "/?v=012", "description": "Raw map, H3 layers, RF burden, suitability and confidence overlays."},
            {"key": "candidates", "title": "Candidate Engineering", "url": "/engineering/candidates?v=012", "legacy_url": "/?v=012#candidates", "description": "Candidate scoring, comparison, evidence drill-down and engineering view."},
            {"key": "pattern-of-life", "title": "Pattern of Life", "url": "/engineering/pattern-of-life?v=012", "description": "Recurring quiet/noisy periods by hour, day, week or month."},
            {"key": "imports", "title": "Import Diagnostics", "url": "/engineering/import-diagnostics?v=012", "description": "CSV import, parser status, quality mode and source file notes."},
            {"key": "api-viewer", "title": "API Payload Viewer", "url": "/engineering/api-viewer?v=012", "description": "Developer-facing endpoint/payload inspection without leaving the app."},
        ],
        "system": [
            {"key": "status", "title": "System Status", "url": "/system/status?v=012", "description": "Runtime, database, static page and API status."},
            {"key": "deploy-check", "title": "Deploy Check", "url": "/system/deploy-check?v=012", "legacy_url": "/api/platform/deploy-check", "description": "Runtime dependencies, static files and deployment readiness."},
            {"key": "logs", "title": "Logs", "url": "/system/logs?v=012", "description": "Local stdout/stderr log file pointers and support checks."},
            {"key": "app-map", "title": "App Map", "url": "/system/app-map?v=012", "description": "Current route map, overlap notes and canonical ownership."},
            {"key": "health-json", "title": "Health JSON", "url": "/api/platform/health", "description": "Raw platform health payload."},
        ],
    }
    for item in primary:
        item["available"] = _v012_page_exists(item.get("url", ""))
    for items in sections.values():
        for item in items:
            item["available"] = _v012_page_exists(item.get("url", "")) or _v012_page_exists(item.get("legacy_url", ""))
    groups = [
        {"group": "Home", "key": "home", "items": [primary[0]]},
        {"group": "Mission Context", "key": "context", "items": [primary[1]]},
        {"group": "Reporting", "key": "reporting", "items": sections["reporting"]},
        {"group": "Engineering", "key": "engineering", "items": sections["engineering"]},
        {"group": "System", "key": "system", "items": sections["system"]},
    ]
    return {"primary": primary, "sections": sections, "groups": groups}


def _v012_db_summary() -> dict[str, _V012Any]:
    db_path = None
    try:
        db_path = DB_PATH  # type: ignore[name-defined]
    except Exception:
        pass
    out: dict[str, _V012Any] = {
        "path": str(db_path) if db_path is not None else None,
        "exists": bool(db_path and _V012Path(db_path).exists()),
        "collection_count": None,
        "total_event_count": None,
        "valid_event_count": None,
        "first_timestamp_utc": None,
        "last_timestamp_utc": None,
    }
    try:
        conn = connect()  # type: ignore[name-defined]
        try:
            out["collection_count"] = int(conn.execute("SELECT COUNT(*) FROM moth_collections").fetchone()[0] or 0)
            row = conn.execute("""
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN valid = 1 THEN 1 ELSE 0 END) AS valid,
                       MIN(timestamp_utc) AS first_ts,
                       MAX(timestamp_utc) AS last_ts
                FROM moth_events
            """).fetchone()
            out["total_event_count"] = int(row[0] or 0)
            out["valid_event_count"] = int(row[1] or 0)
            out["first_timestamp_utc"] = row[2]
            out["last_timestamp_utc"] = row[3]
        finally:
            conn.close()
    except Exception as exc:
        out["error"] = str(exc)
    return out


def _v012_quality_summary() -> dict[str, _V012Any]:
    try:
        q = get_latest_quality_summary(DB_PATH)  # type: ignore[name-defined]
        latest = q.get("latest") if isinstance(q, dict) else None
        if latest:
            raw = int(latest.get("raw_rows") or 0)
            rejected = int(latest.get("rejected_rows") or 0)
            flagged = int(latest.get("flagged_rows") or 0)
            if raw <= 0:
                level = "NO DATA"
            elif rejected == 0 and flagged == 0:
                level = "GOOD"
            elif rejected <= max(10, raw * 0.05):
                level = "CHECK"
            else:
                level = "LOW"
            return {"available": True, "level": level, "latest": latest, "message": q.get("message")}
        return {"available": False, "level": "NO DATA", "latest": None, "message": q.get("message") if isinstance(q, dict) else "No quality summary."}
    except Exception as exc:
        return {"available": False, "level": "CHECK", "latest": None, "error": str(exc)}


def _v012_module_ok(name: str) -> bool:
    try:
        return _v012_importlib_util.find_spec(name) is not None
    except Exception:
        return False


def _v012_static_file_checks() -> dict[str, bool]:
    static = _v012_static_dir()
    names = [
        "platform_home.html", "mission_context.html", "reporting_home.html", "engineering_home.html", "system_home.html", "app_map.html",
        "api_viewer.html", "platform_shell.css", "platform_shell.js", "platform_navigation.json", "lantern_flight_safety.html",
        "mission_brief.html", "j2_report.html", "launch_analysis.html", "data_quality.html", "index.html",
    ]
    return {name: (static / name).exists() for name in names}


def _v012_route_payload() -> list[dict[str, _V012Any]]:
    out = []
    try:
        for route in app.routes:  # type: ignore[name-defined]
            path = getattr(route, "path", None)
            methods = sorted(getattr(route, "methods", []) or [])
            name = getattr(route, "name", "")
            if path:
                out.append({"path": path, "methods": methods, "name": name})
    except Exception:
        pass
    return sorted(out, key=lambda r: (r.get("path") or "", ",".join(r.get("methods") or [])))


@app.get("/app", response_class=_V012HTMLResponse)  # type: ignore[name-defined]
def lantern_v012_platform_home() -> str:
    return _v012_static_page("platform_home.html", "EEI LANTERN Platform Home")


@app.get("/context", response_class=_V012HTMLResponse)  # type: ignore[name-defined]
def lantern_v012_context_page() -> str:
    return _v012_static_page("mission_context.html", "EEI LANTERN Mission Context")


@app.get("/reporting", response_class=_V012HTMLResponse)  # type: ignore[name-defined]
def lantern_v012_reporting_home() -> str:
    return _v012_static_page("reporting_home.html", "EEI LANTERN Reporting")


@app.get("/engineering", response_class=_V012HTMLResponse)  # type: ignore[name-defined]
def lantern_v012_engineering_home() -> str:
    return _v012_static_page("engineering_home.html", "EEI LANTERN Engineering")


@app.get("/system", response_class=_V012HTMLResponse)  # type: ignore[name-defined]
def lantern_v012_system_home() -> str:
    return _v012_static_page("system_home.html", "EEI LANTERN System")


@app.get("/reporting/flight-safety", response_class=_V012HTMLResponse)  # type: ignore[name-defined]
def lantern_v012_reporting_flight_safety() -> str:
    return _v012_static_page("lantern_flight_safety.html", "EEI LANTERN Flight Safety Brief")


@app.get("/reporting/mission-brief", response_class=_V012HTMLResponse)  # type: ignore[name-defined]
def lantern_v012_reporting_mission_brief() -> str:
    return _v012_static_page("mission_brief.html", "EEI LANTERN Mission Operations Brief")


@app.get("/reporting/j2", response_class=_V012HTMLResponse)  # type: ignore[name-defined]
def lantern_v012_reporting_j2() -> str:
    return _v012_static_page("j2_report.html", "EEI LANTERN J2 Live Report")


@app.get("/reporting/gnss-serviceability", response_class=_V012HTMLResponse)  # type: ignore[name-defined]
def lantern_v012_reporting_gnss_serviceability() -> str:
    return _v012_static_page("gnss_serviceability.html", "EEI LANTERN GNSS Serviceability")


@app.get("/reporting/candidate", response_class=_V012HTMLResponse)  # type: ignore[name-defined]
def lantern_v012_reporting_candidate() -> str:
    return _v012_static_page("candidate_report.html", "EEI LANTERN Candidate Site Report")


@app.get("/reporting/evidence-log", response_class=_V012HTMLResponse)  # type: ignore[name-defined]
def lantern_v012_reporting_evidence_log() -> str:
    return _v012_static_page("evidence_log.html", "EEI LANTERN Source / Evidence Log")


@app.get("/reporting/export", response_class=_V012HTMLResponse)  # type: ignore[name-defined]
def lantern_v012_reporting_export() -> str:
    return _v012_static_page("export_pack.html", "EEI LANTERN Export Pack")


@app.get("/engineering/data-quality", response_class=_V012HTMLResponse)  # type: ignore[name-defined]
def lantern_v012_engineering_data_quality() -> str:
    return _v012_static_page("data_quality.html", "EEI LANTERN Data Quality Detail")


@app.get("/engineering/rf", response_class=_V012HTMLResponse)  # type: ignore[name-defined]
def lantern_v012_engineering_rf() -> str:
    return _v012_static_page("launch_analysis.html", "EEI LANTERN RF Analyst")


@app.get("/engineering/spectrum", response_class=_V012HTMLResponse)  # type: ignore[name-defined]
def lantern_v012_engineering_spectrum() -> str:
    return _v012_static_page("launch_analysis.html", "EEI LANTERN Spectrum / Spikes")


@app.get("/engineering/map", response_class=_V012HTMLResponse)  # type: ignore[name-defined]
def lantern_v012_engineering_map() -> str:
    return _v012_static_page("index.html", "EEI LANTERN Map / H3 Layers")


@app.get("/engineering/candidates", response_class=_V012HTMLResponse)  # type: ignore[name-defined]
def lantern_v012_engineering_candidates() -> str:
    return _v012_static_page("index.html", "EEI LANTERN Candidate Engineering")


@app.get("/engineering/pattern-of-life", response_class=_V012HTMLResponse)  # type: ignore[name-defined]
def lantern_v012_engineering_pattern_of_life() -> str:
    return _v012_static_page("pattern_of_life.html", "EEI LANTERN Pattern of Life")


@app.get("/engineering/import-diagnostics", response_class=_V012HTMLResponse)  # type: ignore[name-defined]
def lantern_v012_engineering_import_diagnostics() -> str:
    return _v012_static_page("import_diagnostics.html", "EEI LANTERN Import Diagnostics")


@app.get("/engineering/api-viewer", response_class=_V012HTMLResponse)  # type: ignore[name-defined]
def lantern_v012_engineering_api_viewer() -> str:
    return _v012_static_page("api_viewer.html", "EEI LANTERN API Payload Viewer")


@app.get("/system/status", response_class=_V012HTMLResponse)  # type: ignore[name-defined]
def lantern_v012_system_status() -> str:
    return _v012_static_page("system_home.html", "EEI LANTERN System Status")


@app.get("/system/deploy-check", response_class=_V012HTMLResponse)  # type: ignore[name-defined]
def lantern_v012_system_deploy_check() -> str:
    return _v012_static_page("system_home.html", "EEI LANTERN Deploy Check")


@app.get("/system/logs", response_class=_V012HTMLResponse)  # type: ignore[name-defined]
def lantern_v012_system_logs() -> str:
    return _v012_static_page("system_home.html", "EEI LANTERN Logs")


@app.get("/system/app-map", response_class=_V012HTMLResponse)  # type: ignore[name-defined]
def lantern_v012_system_app_map() -> str:
    return _v012_static_page("app_map.html", "EEI LANTERN App Map")


@app.get("/api/platform/navigation")  # type: ignore[name-defined]
def lantern_v012_platform_navigation() -> dict[str, _V012Any]:
    registry = _v012_nav_registry()
    return {
        "status": "ok",
        "platform_version": _LANTERN_PLATFORM_VERSION,
        "generated_utc": _v012_now(),
        **registry,
        "notes": [
            "Primary navigation is now Home, Mission Context, Reporting, Engineering, System.",
            "Reporting owns end-state decision/reporting outputs.",
            "Engineering owns technical evidence generation, diagnostics and analyst tooling.",
            "Legacy URLs remain available as compatibility entry points.",
        ],
    }


@app.get("/api/platform/mission-context")  # type: ignore[name-defined]
def lantern_v012_platform_mission_context() -> dict[str, _V012Any]:
    db = _v012_db_summary()
    quality = _v012_quality_summary()
    return {
        "status": "ok",
        "platform_version": _LANTERN_PLATFORM_VERSION,
        "generated_utc": _v012_now(),
        "aoi": "Aden Adde / Mogadishu",
        "collections": {
            "count": db.get("collection_count"),
            "selected": "all loaded unless page filter overrides",
            "total_events": db.get("total_event_count"),
            "valid_events": db.get("valid_event_count"),
        },
        "time_window": {
            "first_timestamp_utc": db.get("first_timestamp_utc"),
            "last_timestamp_utc": db.get("last_timestamp_utc"),
            "selected": "all available unless page filter overrides",
        },
        "data_quality": quality,
        "rf_threshold": "-60 dBm strong-spike/reporting reference unless page filter overrides",
        "gnss_mode": "GPS/Galileo/GLONASS/BeiDou L-band clearance view where supported",
        "interpretation_boundary": "LANTERN is decision support. MOTH detections are event-based; no detection does not prove a band is empty.",
    }


@app.get("/api/platform/health")  # type: ignore[name-defined]
def lantern_v012_platform_health() -> dict[str, _V012Any]:
    return {
        "status": "ok",
        "platform_version": _LANTERN_PLATFORM_VERSION,
        "generated_utc": _v012_now(),
        "static_dir": str(_v012_static_dir()),
        "static_files": _v012_static_file_checks(),
        "database": _v012_db_summary(),
        "mission_context": "/api/platform/mission-context",
        "navigation_url": "/api/platform/navigation",
        "home_url": "/app?v=012",
        "route_count": len(_v012_route_payload()),
    }


@app.get("/api/platform/app-map")  # type: ignore[name-defined]
def lantern_v012_platform_app_map() -> dict[str, _V012Any]:
    registry = _v012_nav_registry()
    overlaps = [
        {"area": "GNSS/RF", "pages": ["Flight Safety Brief", "RF Analyst"], "decision": "Keep. Reporting summarizes; Engineering explains."},
        {"area": "Mission readiness", "pages": ["Mission Operations Brief", "Flight Safety Brief"], "decision": "Keep as shared evidence. Mission Brief should consume the Flight Safety summary."},
        {"area": "J2/security", "pages": ["Mission Operations Brief", "J2 Live Report"], "decision": "Keep. J2 owns source detail; Mission Brief only shows summary."},
        {"area": "Candidate scoring", "pages": ["Candidate Site Report", "Map / Candidate Engineering"], "decision": "Keep. Engineering creates evidence; Reporting packages the end-state recommendation."},
        {"area": "Map colours", "pages": ["Antenna suitability", "RF burden/quietness"], "decision": "Keep separate legends. Green means different things depending on layer type."},
        {"area": "Old /rotator", "pages": ["J2 Live Report"], "decision": "Do not restore /rotator. Article rotation belongs inside J2 Live Report."},
    ]
    return {"status": "ok", "platform_version": _LANTERN_PLATFORM_VERSION, "generated_utc": _v012_now(), "navigation": registry, "overlaps": overlaps, "routes": _v012_route_payload()}


@app.get("/api/platform/deploy-check")  # type: ignore[name-defined]
def lantern_v012_platform_deploy_check() -> dict[str, _V012Any]:
    static = _v012_static_dir()
    db = _v012_db_summary()
    checks: list[dict[str, _V012Any]] = []
    checks.append({"name": "Python", "status": "pass", "detail": _v012_sys.version.split()[0]})
    checks.append({"name": "Database path", "status": "pass" if db.get("exists") else "warn", "detail": db.get("path") or "DB_PATH unavailable"})
    for name, exists in _v012_static_file_checks().items():
        required = name in {"platform_home.html", "mission_context.html", "reporting_home.html", "engineering_home.html", "system_home.html", "platform_shell.css", "platform_shell.js"}
        status = "pass" if exists else "fail" if required else "warn"
        checks.append({"name": f"Static file: {name}", "status": status, "detail": str(static / name)})
    for mod in ["fastapi", "uvicorn", "pandas", "h3", "orjson", "pydantic"]:
        checks.append({"name": f"Python module: {mod}", "status": "pass" if _v012_module_ok(mod) else "fail", "detail": "available" if _v012_module_ok(mod) else "missing"})
    for mod in ["webview", "PyInstaller"]:
        checks.append({"name": f"Optional packaging module: {mod}", "status": "pass" if _v012_module_ok(mod) else "warn", "detail": "available" if _v012_module_ok(mod) else "optional; install for desktop packaging"})
    route_checks = [
        ("/app", _v012_route_exists("/app")),
        ("/context", _v012_route_exists("/context")),
        ("/reporting", _v012_route_exists("/reporting")),
        ("/engineering", _v012_route_exists("/engineering")),
        ("/system", _v012_route_exists("/system")),
        ("/api/platform/navigation", _v012_route_exists("/api/platform/navigation")),
        ("/api/platform/mission-context", _v012_route_exists("/api/platform/mission-context")),
    ]
    for path, exists in route_checks:
        checks.append({"name": f"Route: {path}", "status": "pass" if exists else "fail", "detail": "registered" if exists else "missing"})
    fail_count = sum(1 for c in checks if c.get("status") == "fail")
    warn_count = sum(1 for c in checks if c.get("status") == "warn")
    return {
        "status": "ready" if fail_count == 0 else "not_ready",
        "platform_version": _LANTERN_PLATFORM_VERSION,
        "generated_utc": _v012_now(),
        "fail_count": fail_count,
        "warn_count": warn_count,
        "checks": checks,
        "deployment_guidance": [
            "Use /app?v=012 as the only platform home.",
            "Use Reporting for end-state outputs.",
            "Use Engineering for technical evidence and diagnostics.",
            "Use Start_LANTERN_Local.ps1 or LANTERN_App.ps1 after deploy-check has no failures.",
        ],
    }


@app.get("/api/platform/routes")  # type: ignore[name-defined]
def lantern_v012_platform_routes() -> dict[str, _V012Any]:
    return {"status": "ok", "platform_version": _LANTERN_PLATFORM_VERSION, "generated_utc": _v012_now(), "routes": _v012_route_payload()}


# Compatibility check retained for stale pages that still call the old J2 rotator status endpoint.
def _v012_j2_live_payload() -> dict[str, _V012Any]:
    static = _v012_static_dir()
    return {
        "ok": (static / "j2_report.html").exists() and (_v012_route_exists("/api/j2/news") or _v012_route_exists("/api/j2/report")),
        "status": "ok",
        "platform_version": _LANTERN_PLATFORM_VERSION,
        "compatibility_endpoint": True,
        "files": {
            "j2_report_html": (static / "j2_report.html").exists(),
            "rotator_html_required": False,
            "rotator_html_present": (static / "rotator.html").exists(),
        },
        "routes": {
            "j2_news": _v012_route_exists("/api/j2/news"),
            "j2_report": _v012_route_exists("/api/j2/report"),
            "j2_threat_actors": _v012_route_exists("/api/j2/threat-actors") or _v012_route_exists("/api/j2/threat_actors"),
            "legacy_rotator_route_removed": not _v012_route_exists("/rotator"),
        },
        "urls": {"j2_live_report": "/reporting/j2?v=012", "legacy_rotator": "not used"},
        "note": "The display /rotator route is not used. The correct live article rotator is inside J2 Live Report.",
    }


@app.get("/api/platform/j2-live-check")  # type: ignore[name-defined]
def lantern_v012_j2_live_check() -> dict[str, _V012Any]:
    return _v012_j2_live_payload()


@app.get("/api/platform/j2-rotator-check")  # type: ignore[name-defined]
def lantern_v012_j2_rotator_check_compat() -> dict[str, _V012Any]:
    return _v012_j2_live_payload()

# ---- end EEI LANTERN v0.12.0 Navigation Rationalisation layer ----

