from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .config import PARSER_VERSION
from .db import connect, init_db, rows_to_dicts
from .h3tools import latlon_to_cell
from .parser import parse_frequency_hz, validate_event

ADAPTER_VERSION = "0.1.0"
MOTH_SDK_SOURCE_TYPE = "moth_sdk_mavlink"
RF_EXPLORER_SOURCE_TYPE = "rf_explorer_reserved"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sensor_capabilities() -> dict[str, Any]:
    return {
        "adapter_version": ADAPTER_VERSION,
        "csv_ingest_preserved": True,
        "supported_now": [
            {
                "sensor_kind": "moth_sdk",
                "source_type": MOTH_SDK_SOURCE_TYPE,
                "transport": "usb_serial_mavlink_v2",
                "default_baud_rate": 921600,
                "raw_archive": True,
                "normalized_messages": ["RF_SIGNAL", "RF_MEASUREMENT", "MOTH_TABLE_LOG"],
                "context_messages": ["GPS_RAW_INT", "BATTERY_STATUS", "MOTH_INFO", "MOTH_BATTERY_INFO", "PARAM_VALUE", "STATUSTEXT"],
                "notes": [
                    "Raw SDK/MAVLink messages are preserved before normalization.",
                    "RF events feed the existing moth_events table so maps, timelines, scoring and reports continue to work.",
                    "CSV upload/import remains independent and unchanged.",
                ],
            }
        ],
        "reserved_future": [
            {
                "sensor_kind": "rf_explorer",
                "source_type": RF_EXPLORER_SOURCE_TYPE,
                "status": "reserved_not_implemented",
                "notes": [
                    "Schema is sensor-generic so RF Explorer can use the same raw-message and session model later.",
                    "Parser details should wait for the exact RF Explorer export/API format.",
                ],
            }
        ],
    }


def _as_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    f = _as_float(value)
    if f is None:
        return None
    return int(round(f))


def _fields(payload: dict[str, Any]) -> dict[str, Any]:
    fields = payload.get("fields")
    return fields if isinstance(fields, dict) else payload


def _message_type(payload: dict[str, Any]) -> str:
    return str(payload.get("message_type") or payload.get("type") or payload.get("mavpackettype") or "UNKNOWN").upper()


def _timestamp_from_time_nsec(value: Any, fallback: str) -> str:
    n = _as_float(value)
    if n is None:
        return fallback
    # MOTH SDK documents this as either Unix epoch ns or time since boot.
    if n > 1_000_000_000_000_000_000 / 10:
        return datetime.fromtimestamp(n / 1_000_000_000.0, tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return fallback


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def _session_row(conn, session_uuid: str):
    return conn.execute(
        "SELECT * FROM external_sensor_sessions WHERE session_uuid = ?",
        (session_uuid,),
    ).fetchone()


def start_sensor_session(
    *,
    sensor_kind: str = "moth_sdk",
    session_uuid: str | None = None,
    collection_name: str | None = None,
    source_type: str | None = None,
    transport: str | None = None,
    device_serial: str | None = None,
    firmware_version: str | None = None,
    hardware_version: str | None = None,
    sdk_version: str | None = None,
    source_port: str | None = None,
    baud_rate: int | None = None,
    notes: str | None = None,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    init_db(db_path) if db_path else init_db()
    session_uuid = session_uuid or str(uuid4())
    sensor_kind = (sensor_kind or "moth_sdk").strip().lower()
    source_type = source_type or (MOTH_SDK_SOURCE_TYPE if sensor_kind == "moth_sdk" else sensor_kind)
    transport = transport or ("usb_serial_mavlink_v2" if sensor_kind == "moth_sdk" else None)
    baud_rate = baud_rate if baud_rate is not None else (921600 if sensor_kind == "moth_sdk" else None)
    now = utc_now()

    conn = connect(db_path) if db_path else connect()
    row = _session_row(conn, session_uuid)
    if row:
        out = dict(row)
        conn.close()
        return out
    with conn:
        cur = conn.execute(
            """
            INSERT INTO moth_collections (
                collection_name, device_serial, firmware_version, hardware_version, source_type,
                scan_mode, file_name, file_hash, upload_time_utc, collection_start_utc,
                row_count, valid_event_count, invalid_event_count, parser_version, operator_notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                collection_name or f"{sensor_kind} live session {now}",
                device_serial,
                firmware_version,
                hardware_version,
                source_type,
                transport,
                None,
                None,
                now,
                now,
                0,
                0,
                0,
                PARSER_VERSION,
                notes,
            ),
        )
        collection_id = int(cur.lastrowid)
        cur = conn.execute(
            """
            INSERT INTO external_sensor_sessions (
                collection_id, session_uuid, sensor_kind, transport, source_type, device_serial,
                firmware_version, hardware_version, sdk_version, adapter_version, source_port,
                baud_rate, started_utc, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                collection_id,
                session_uuid,
                sensor_kind,
                transport,
                source_type,
                device_serial,
                firmware_version,
                hardware_version,
                sdk_version,
                ADAPTER_VERSION,
                source_port,
                baud_rate,
                now,
                notes,
            ),
        )
        sensor_session_id = int(cur.lastrowid)
    conn.close()
    return {
        "sensor_session_id": sensor_session_id,
        "collection_id": collection_id,
        "session_uuid": session_uuid,
        "sensor_kind": sensor_kind,
        "source_type": source_type,
        "started_utc": now,
    }


def _update_session_context(conn, sensor_session_id: int, message_type: str, fields: dict[str, Any], received_utc: str) -> None:
    updates: dict[str, Any] = {"last_message_utc": received_utc}
    if message_type == "GPS_RAW_INT":
        lat = _as_float(fields.get("lat"))
        lon = _as_float(fields.get("lon"))
        alt = _as_float(fields.get("alt"))
        updates["gps_fix_type"] = _as_int(fields.get("fix_type"))
        updates["satellites_seen"] = _as_int(fields.get("satellites_visible"))
        if lat is not None:
            updates["last_lat"] = lat / 10_000_000.0
        if lon is not None:
            updates["last_lon"] = lon / 10_000_000.0
        if alt is not None:
            updates["last_altitude_msl_m"] = alt / 1000.0
    elif message_type in {"BATTERY_STATUS", "MOTH_BATTERY_STATUS"}:
        updates["battery_remaining"] = _as_int(fields.get("battery_remaining"))
        updates["battery_flags"] = _as_int(fields.get("flags"))
    elif message_type == "MOTH_INFO":
        updates["firmware_version"] = fields.get("firmware_version")
        updates["hardware_version"] = fields.get("hardware_version")
        updates["device_serial"] = fields.get("serial_number")

    clean = {k: v for k, v in updates.items() if v is not None}
    if not clean:
        return
    assignments = ", ".join(f"{k} = ?" for k in clean)
    conn.execute(
        f"UPDATE external_sensor_sessions SET {assignments} WHERE sensor_session_id = ?",
        [*clean.values(), sensor_session_id],
    )


def _insert_event(conn, *, collection_id: int, timestamp_utc: str, fields: dict[str, Any], raw_payload: dict[str, Any], raw_message_id: int, session_row: dict[str, Any], message_type: str) -> int:
    frequency = fields.get("frequency", fields.get("center_frequency"))
    frequency_hz = parse_frequency_hz(frequency, "frequency_mhz")
    strength_dbm = _as_float(fields.get("power_level", fields.get("display_strength")))
    lat = session_row.get("last_lat")
    lon = session_row.get("last_lon")
    altitude = session_row.get("last_altitude_msl_m")
    sats = session_row.get("satellites_seen")
    age_s = _as_float(fields.get("age_s", fields.get("age")))
    valid, notes = validate_event(lat, lon, strength_dbm, frequency_hz)
    if raw_message_id:
        notes = ";".join(part for part in [notes, f"raw_message_id={raw_message_id}"] if part)
    h3_r8 = latlon_to_cell(lat, lon, 8) if valid and lat is not None and lon is not None else None
    h3_r9 = latlon_to_cell(lat, lon, 9) if valid and lat is not None and lon is not None else None
    h3_r10 = latlon_to_cell(lat, lon, 10) if valid and lat is not None and lon is not None else None
    conn.execute(
        """
        INSERT INTO moth_events (
            collection_id, timestamp_utc, lat, lon, altitude_msl_m, satellites_seen,
            frequency_hz, signal_type, strength_dbm, age_s, scan_range_id,
            h3_r8, h3_r9, h3_r10, valid, validation_notes, raw_row_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            collection_id,
            timestamp_utc,
            lat,
            lon,
            altitude,
            sats,
            frequency_hz,
            message_type,
            strength_dbm,
            age_s,
            None,
            h3_r8,
            h3_r9,
            h3_r10,
            valid,
            notes,
            _json(raw_payload),
        ),
    )
    return int(valid)


def ingest_sensor_message(
    *,
    session_uuid: str,
    payload: dict[str, Any],
    sensor_kind: str = "moth_sdk",
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    init_db(db_path) if db_path else init_db()
    session = start_sensor_session(sensor_kind=sensor_kind, session_uuid=session_uuid, db_path=db_path)
    received_utc = str(payload.get("received_utc") or utc_now())
    message_type = _message_type(payload)
    fields = _fields(payload)
    conn = connect(db_path) if db_path else connect()
    normalized_event_count = 0
    valid_event_count = 0
    parse_notes: list[str] = []
    with conn:
        row = _session_row(conn, session_uuid)
        if row is None:
            raise ValueError(f"Sensor session was not created: {session_uuid}")
        session_row = dict(row)
        raw_cur = conn.execute(
            """
            INSERT INTO raw_sensor_messages (
                sensor_session_id, received_utc, message_type, message_id, source_system,
                source_component, sequence_number, raw_payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_row["sensor_session_id"],
                received_utc,
                message_type,
                _as_int(payload.get("message_id") or payload.get("msg_id")),
                _as_int(payload.get("source_system")),
                _as_int(payload.get("source_component")),
                _as_int(payload.get("sequence_number") or payload.get("seq")),
                _json(payload),
            ),
        )
        raw_message_id = int(raw_cur.lastrowid)
        _update_session_context(conn, session_row["sensor_session_id"], message_type, fields, received_utc)
        refreshed = dict(_session_row(conn, session_uuid))

        if message_type == "PARAM_VALUE":
            conn.execute(
                """
                INSERT INTO sensor_parameter_snapshots (
                    sensor_session_id, captured_utc, parameter_name, parameter_value, parameter_type, raw_payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    refreshed["sensor_session_id"],
                    received_utc,
                    str(fields.get("param_id") or fields.get("parameter_name") or "unknown"),
                    str(fields.get("param_value")) if fields.get("param_value") is not None else None,
                    str(fields.get("param_type")) if fields.get("param_type") is not None else None,
                    _json(payload),
                ),
            )
        elif message_type in {"RF_SIGNAL", "RF_MEASUREMENT"}:
            ts = _timestamp_from_time_nsec(fields.get("time_nsec"), received_utc)
            valid_event_count += _insert_event(
                conn,
                collection_id=refreshed["collection_id"],
                timestamp_utc=ts,
                fields=fields,
                raw_payload=payload,
                raw_message_id=raw_message_id,
                session_row=refreshed,
                message_type=message_type,
            )
            normalized_event_count += 1
        elif message_type == "MOTH_TABLE_LOG":
            frequencies = fields.get("frequencies") or []
            strengths = fields.get("display_strength") or []
            ages = fields.get("age") or []
            limit = min(_as_int(fields.get("num_points")) or len(frequencies), len(frequencies), len(strengths))
            for i in range(max(0, limit)):
                event_fields = {
                    "frequency": frequencies[i],
                    "display_strength": strengths[i],
                    "age": ages[i] if i < len(ages) else None,
                }
                valid_event_count += _insert_event(
                    conn,
                    collection_id=refreshed["collection_id"],
                    timestamp_utc=received_utc,
                    fields=event_fields,
                    raw_payload={**payload, "table_index": i},
                    raw_message_id=raw_message_id,
                    session_row=refreshed,
                    message_type=message_type,
                )
                normalized_event_count += 1
        else:
            parse_notes.append("stored_raw_only")

        conn.execute(
            """
            UPDATE raw_sensor_messages
            SET normalized_event_count = ?, parse_notes = ?
            WHERE raw_message_id = ?
            """,
            (normalized_event_count, ";".join(parse_notes) if parse_notes else None, raw_message_id),
        )
        conn.execute(
            """
            UPDATE external_sensor_sessions
            SET message_count = message_count + 1,
                rf_event_count = rf_event_count + ?,
                last_message_utc = ?
            WHERE sensor_session_id = ?
            """,
            (normalized_event_count, received_utc, refreshed["sensor_session_id"]),
        )
        conn.execute(
            """
            UPDATE moth_collections
            SET row_count = row_count + ?,
                valid_event_count = valid_event_count + ?,
                invalid_event_count = invalid_event_count + ?,
                collection_end_utc = ?
            WHERE collection_id = ?
            """,
            (normalized_event_count, valid_event_count, normalized_event_count - valid_event_count, received_utc, refreshed["collection_id"]),
        )
    conn.close()
    return {
        "ok": True,
        "session_uuid": session_uuid,
        "collection_id": session["collection_id"],
        "message_type": message_type,
        "normalized_event_count": normalized_event_count,
    }


def list_sensor_sessions(*, limit: int = 20, db_path: Path | str | None = None) -> list[dict[str, Any]]:
    init_db(db_path) if db_path else init_db()
    conn = connect(db_path) if db_path else connect()
    rows = conn.execute(
        """
        SELECT s.*, c.collection_name
        FROM external_sensor_sessions s
        JOIN moth_collections c ON c.collection_id = s.collection_id
        ORDER BY s.sensor_session_id DESC
        LIMIT ?
        """,
        (int(limit),),
    ).fetchall()
    conn.close()
    return rows_to_dicts(rows)


def sensor_status(*, db_path: Path | str | None = None) -> dict[str, Any]:
    init_db(db_path) if db_path else init_db()
    conn = connect(db_path) if db_path else connect()
    sessions = int(conn.execute("SELECT COUNT(*) FROM external_sensor_sessions").fetchone()[0] or 0)
    raw_messages = int(conn.execute("SELECT COUNT(*) FROM raw_sensor_messages").fetchone()[0] or 0)
    normalized_events = int(conn.execute("SELECT COALESCE(SUM(rf_event_count), 0) FROM external_sensor_sessions").fetchone()[0] or 0)
    latest = conn.execute(
        """
        SELECT s.*, c.collection_name
        FROM external_sensor_sessions s
        JOIN moth_collections c ON c.collection_id = s.collection_id
        ORDER BY s.sensor_session_id DESC
        LIMIT 1
        """
    ).fetchone()
    conn.close()
    return {
        "ok": True,
        "adapter_version": ADAPTER_VERSION,
        "csv_ingest_preserved": True,
        "session_count": sessions,
        "raw_message_count": raw_messages,
        "normalized_event_count": normalized_events,
        "latest_session": dict(latest) if latest else None,
    }
