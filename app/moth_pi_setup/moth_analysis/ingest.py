from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .db import connect, init_db
from .parser import ParseResult, parse_csv
from .config import PARSER_VERSION


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def insert_collection_from_csv(
    csv_path: Path,
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
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    init_db(db_path) if db_path else init_db()
    result = parse_csv(csv_path)
    name = collection_name or csv_path.stem

    conn = connect(db_path) if db_path else connect()
    with conn:
        cur = conn.execute(
            """
            INSERT INTO moth_collections (
                collection_name, device_serial, firmware_version, hardware_version, source_type,
                scan_mode, detection_threshold_db, white_list_enabled, antenna_height_agl_m,
                antenna_notes, operator_notes, file_name, file_hash, upload_time_utc,
                collection_start_utc, collection_end_utc, row_count, valid_event_count,
                invalid_event_count, parser_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name, device_serial, firmware_version, hardware_version, source_type,
                scan_mode, detection_threshold_db, 1 if white_list_enabled else 0, antenna_height_agl_m,
                antenna_notes, operator_notes, csv_path.name, result.file_hash, utc_now(),
                result.collection_start_utc, result.collection_end_utc, result.row_count,
                result.valid_event_count, result.invalid_event_count, PARSER_VERSION,
            ),
        )
        collection_id = int(cur.lastrowid)
        conn.executemany(
            """
            INSERT INTO moth_events (
                collection_id, timestamp_utc, lat, lon, altitude_msl_m, satellites_seen,
                frequency_hz, signal_type, strength_dbm, age_s, scan_range_id,
                h3_r8, h3_r9, h3_r10, valid, validation_notes, raw_row_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    collection_id, e.timestamp_utc, e.lat, e.lon, e.altitude_msl_m, e.satellites_seen,
                    e.frequency_hz, e.signal_type, e.strength_dbm, e.age_s, e.scan_range_id,
                    e.h3_r8, e.h3_r9, e.h3_r10, e.valid, e.validation_notes, e.raw_row_json,
                )
                for e in result.rows
            ],
        )
    conn.close()
    return {
        "collection_id": collection_id,
        "collection_name": name,
        "file_name": csv_path.name,
        "file_hash": result.file_hash,
        "row_count": result.row_count,
        "valid_event_count": result.valid_event_count,
        "invalid_event_count": result.invalid_event_count,
        "collection_start_utc": result.collection_start_utc,
        "collection_end_utc": result.collection_end_utc,
    }


def insert_candidate_site(
    *,
    name: str,
    lat: float,
    lon: float,
    antenna_height_agl_m: float | None = None,
    practical_score: float = 0.5,
    site_notes: str | None = None,
    db_path: Path | str | None = None,
) -> int:
    init_db(db_path) if db_path else init_db()
    conn = connect(db_path) if db_path else connect()
    with conn:
        cur = conn.execute(
            """
            INSERT INTO candidate_sites (name, lat, lon, antenna_height_agl_m, practical_score, site_notes, created_utc)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                lat = excluded.lat,
                lon = excluded.lon,
                antenna_height_agl_m = excluded.antenna_height_agl_m,
                practical_score = excluded.practical_score,
                site_notes = excluded.site_notes
            """,
            (name, lat, lon, antenna_height_agl_m, practical_score, site_notes, utc_now()),
        )
        if cur.lastrowid:
            site_id = int(cur.lastrowid)
        else:
            site_id = int(conn.execute("SELECT site_id FROM candidate_sites WHERE name = ?", (name,)).fetchone()[0])
    conn.close()
    return site_id
