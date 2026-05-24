from __future__ import annotations

import csv
import math
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
    try:
        return insert_collection_from_csv(
            dest,
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
    "L1": {"center_hz": 1575.42e6, "default_width_mhz": 40.0, "note": "GNSS L1 practical ±20 MHz display window"},
    "L2": {"center_hz": 1227.60e6, "default_width_mhz": 40.0, "note": "GNSS L2 practical ±20 MHz display window"},
    "L5": {"center_hz": 1176.45e6, "default_width_mhz": 40.0, "note": "GNSS L5 practical ±20 MHz display window"},
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
            f"Strong GNSS-window spikes ≥ {spike_dbm} dBm: {gnss_spikes}",
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
                f"Fallback all-RF spike count ≥ {spike_dbm} dBm: {w['all_rf_spike_count']}",
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
