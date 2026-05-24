from __future__ import annotations

import csv
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .h3tools import latlon_to_cell
from .config import PARSER_VERSION

_NORMALISE_RE = re.compile(r"[^a-z0-9]+")
_NUMBER_RE = re.compile(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")


@dataclass
class ParsedEvent:
    timestamp_utc: str | None
    lat: float | None
    lon: float | None
    altitude_msl_m: float | None
    satellites_seen: int | None
    frequency_hz: float | None
    signal_type: str | None
    strength_dbm: float | None
    age_s: float | None
    scan_range_id: str | None
    h3_r8: str | None
    h3_r9: str | None
    h3_r10: str | None
    valid: int
    validation_notes: str
    raw_row_json: str


@dataclass
class ParseResult:
    rows: list[ParsedEvent]
    file_hash: str
    row_count: int
    valid_event_count: int
    invalid_event_count: int
    collection_start_utc: str | None
    collection_end_utc: str | None
    parser_version: str = PARSER_VERSION


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def normalise_key(key: str) -> str:
    key = key.strip().lower()
    key = key.replace("µ", "u")
    return _NORMALISE_RE.sub("_", key).strip("_")


FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "timestamp_utc": (
        "gps_time_utc_iso8601", "timestamp_utc", "utc_timestamp", "utc_time", "time_utc", "date_time_utc", "datetime_utc",
        "timestamp", "time", "date_time", "datetime", "gps_time", "gps_utc",
    ),
    "lat": ("lat", "latitude", "gps_lat", "gps_latitude", "position_lat", "location_lat"),
    "lon": ("lon", "lng", "long", "longitude", "gps_lon", "gps_lng", "gps_longitude", "position_lon", "location_lon"),
    "altitude_msl_m": (
        "altitude_msl_m", "altitude_m", "alt_m", "altitude", "gps_altitude", "gps_altitude_m",
        "msl_altitude", "msl_altitude_m", "height_msl_m",
    ),
    "satellites_seen": (
        "satellites_seen", "satellites", "satellites_in_view", "gps_satellites", "gnss_satellites", "sats", "sat_count",
    ),
    "frequency_hz": (
        "frequency_hz", "freq_hz", "frequency", "freq", "detected_frequency", "signal_frequency", "centre_frequency",
        "center_frequency", "frequency_mhz", "freq_mhz", "frequency_ghz", "freq_ghz",
    ),
    "signal_type": (
        "signal_type", "type", "signal", "band", "classification", "detected_signal", "protocol", "modulation",
    ),
    "strength_dbm": (
        "strength_dbm", "rssi_dbm", "rssi", "dbm", "signal_strength", "signal_strength_dbm", "level_dbm",
        "power_dbm", "rx_power_dbm", "received_power_dbm", "strength",
    ),
    "age_s": ("age_s", "age", "signal_age", "age_seconds"),
    "scan_range_id": ("scan_range_id", "scan_range", "range_id", "range", "scan"),
}


def first_present(row: dict[str, Any], aliases: Iterable[str]) -> Any | None:
    for alias in aliases:
        if alias in row and row[alias] not in (None, ""):
            return row[alias]
    return None


def parse_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    m = _NUMBER_RE.search(text)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def parse_int(value: Any) -> int | None:
    f = parse_float(value)
    if f is None:
        return None
    return int(round(f))


def parse_frequency_hz(value: Any, source_key: str | None = None) -> float | None:
    f = parse_float(value)
    if f is None:
        return None
    text = str(value).strip().lower() if value is not None else ""
    key = (source_key or "").lower()

    if "ghz" in text or key.endswith("_ghz") or key == "freq_ghz" or key == "frequency_ghz":
        return f * 1_000_000_000.0
    if "mhz" in text or key.endswith("_mhz") or key == "freq_mhz" or key == "frequency_mhz":
        return f * 1_000_000.0
    if "khz" in text or key.endswith("_khz"):
        return f * 1_000.0
    if "hz" in text or key.endswith("_hz"):
        return f

    # Heuristic for CSV exports labelled just "Frequency":
    # MOTH covers 5 MHz to 6 GHz. Plain values below 10,000 are likely MHz/GHz, not Hz.
    if 5 <= f <= 6000:
        return f * 1_000_000.0
    if 0.005 <= f < 5:
        return f * 1_000_000_000.0
    return f


def parse_timestamp_utc(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None

    # Unix epoch support.
    if re.fullmatch(r"\d{10}(?:\.\d+)?", text):
        return datetime.fromtimestamp(float(text), tz=timezone.utc).isoformat().replace("+00:00", "Z")
    if re.fullmatch(r"\d{13}", text):
        return datetime.fromtimestamp(float(text) / 1000.0, tz=timezone.utc).isoformat().replace("+00:00", "Z")

    candidates = [
        text,
        text.replace("Z", "+00:00"),
        text.replace(" UTC", "+00:00"),
    ]
    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f",
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%y %H:%M:%S",
        "%Y/%m/%d %H:%M:%S",
    ]
    for candidate in candidates:
        try:
            dt = datetime.fromisoformat(candidate)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        except ValueError:
            pass
    for fmt in formats:
        try:
            dt = datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
            return dt.isoformat().replace("+00:00", "Z")
        except ValueError:
            pass
    return text  # Preserve unparsed timestamp instead of destroying data.


def remap_row(raw: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    normalised = {normalise_key(k): v for k, v in raw.items()}
    source_key_for_field: dict[str, str] = {}
    mapped: dict[str, Any] = {}
    for field, aliases in FIELD_ALIASES.items():
        for alias in aliases:
            if alias in normalised and normalised[alias] not in (None, ""):
                mapped[field] = normalised[alias]
                source_key_for_field[field] = alias
                break
        else:
            mapped[field] = None
    return mapped, source_key_for_field


def validate_event(lat: float | None, lon: float | None, strength_dbm: float | None, frequency_hz: float | None) -> tuple[int, str]:
    notes: list[str] = []
    if lat is None or lon is None:
        notes.append("missing_coordinate")
    else:
        if abs(lat) < 1e-9 and abs(lon) < 1e-9:
            notes.append("no_gps_fix_0_0")
        if not (-90 <= lat <= 90):
            notes.append("invalid_latitude")
        if not (-180 <= lon <= 180):
            notes.append("invalid_longitude")
    if strength_dbm is None:
        notes.append("missing_strength_dbm")
    elif not (-180 <= strength_dbm <= 40):
        notes.append("strength_dbm_out_of_expected_range")
    if frequency_hz is not None and not (0 < frequency_hz < 100_000_000_000):
        notes.append("frequency_hz_out_of_expected_range")
    valid = 0 if notes else 1
    return valid, ";".join(notes)


def parse_csv(path: Path) -> ParseResult:
    file_hash = sha256_file(path)
    events: list[ParsedEvent] = []
    timestamps: list[str] = []

    with path.open("r", newline="", encoding="utf-8-sig", errors="replace") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise ValueError("CSV has no header row")
        for raw in reader:
            mapped, source_keys = remap_row(raw)
            timestamp = parse_timestamp_utc(mapped.get("timestamp_utc"))
            lat = parse_float(mapped.get("lat"))
            lon = parse_float(mapped.get("lon"))
            altitude_msl_m = parse_float(mapped.get("altitude_msl_m"))
            satellites_seen = parse_int(mapped.get("satellites_seen"))
            frequency_hz = parse_frequency_hz(mapped.get("frequency_hz"), source_keys.get("frequency_hz"))
            signal_type = str(mapped.get("signal_type")).strip() if mapped.get("signal_type") not in (None, "") else None
            strength_dbm = parse_float(mapped.get("strength_dbm"))
            age_s = parse_float(mapped.get("age_s"))
            scan_range_id = str(mapped.get("scan_range_id")).strip() if mapped.get("scan_range_id") not in (None, "") else None
            valid, validation_notes = validate_event(lat, lon, strength_dbm, frequency_hz)

            h3_r8 = latlon_to_cell(lat, lon, 8) if valid and lat is not None and lon is not None else None
            h3_r9 = latlon_to_cell(lat, lon, 9) if valid and lat is not None and lon is not None else None
            h3_r10 = latlon_to_cell(lat, lon, 10) if valid and lat is not None and lon is not None else None

            if timestamp:
                timestamps.append(timestamp)

            events.append(ParsedEvent(
                timestamp_utc=timestamp,
                lat=lat,
                lon=lon,
                altitude_msl_m=altitude_msl_m,
                satellites_seen=satellites_seen,
                frequency_hz=frequency_hz,
                signal_type=signal_type,
                strength_dbm=strength_dbm,
                age_s=age_s,
                scan_range_id=scan_range_id,
                h3_r8=h3_r8,
                h3_r9=h3_r9,
                h3_r10=h3_r10,
                valid=valid,
                validation_notes=validation_notes,
                raw_row_json=json.dumps(raw, ensure_ascii=False),
            ))

    valid_count = sum(1 for e in events if e.valid)
    invalid_count = len(events) - valid_count
    timestamps_sorted = sorted(timestamps)
    return ParseResult(
        rows=events,
        file_hash=file_hash,
        row_count=len(events),
        valid_event_count=valid_count,
        invalid_event_count=invalid_count,
        collection_start_utc=timestamps_sorted[0] if timestamps_sorted else None,
        collection_end_utc=timestamps_sorted[-1] if timestamps_sorted else None,
    )
