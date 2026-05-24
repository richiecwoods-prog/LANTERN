"""
MOTH v0.8.0 decision-workflow analysis helpers.

Purpose:
    Provide deterministic metrics for the v0.8.0 UX increment:
    - data quality dashboard
    - launch-window decision card
    - candidate antenna-site decision card
    - explain-this-result text
    - one-page report templates

Design rule:
    The API calculates scores. Any AI component may explain the score, but must
    not invent or change score values.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
import csv
import hashlib
import math
import statistics


GNSS_BANDS_HZ: Dict[str, float] = {
    "L1": 1_575_420_000.0,
    "L2": 1_227_600_000.0,
    "L3": 1_381_050_000.0,
    "L5": 1_176_450_000.0,
}

DEFAULT_GNSS_WINDOW_MHZ = 20.0
DEFAULT_SPIKE_THRESHOLD_DBM = -60.0

TIMESTAMP_COLUMNS = (
    "timestamp",
    "time",
    "datetime",
    "date_time",
    "utc",
    "created_at",
    "logged_at",
)
LAT_COLUMNS = ("lat", "latitude", "gps_lat", "y")
LON_COLUMNS = ("lon", "lng", "longitude", "gps_lon", "gps_lng", "x")
FREQ_COLUMNS = (
    "frequency_hz",
    "freq_hz",
    "frequency",
    "freq",
    "center_frequency_hz",
    "center_frequency",
    "mhz",
    "freq_mhz",
)
DBM_COLUMNS = (
    "dbm",
    "signal_dbm",
    "strength_dbm",
    "rssi",
    "power_dbm",
    "level_dbm",
    "power",
    "level",
)


@dataclass(frozen=True)
class MothRecord:
    timestamp: Optional[datetime]
    latitude: Optional[float]
    longitude: Optional[float]
    frequency_hz: Optional[float]
    dbm: Optional[float]
    source_file: str = ""
    row_number: int = 0

    def to_public_dict(self) -> Dict[str, Any]:
        item = asdict(self)
        item["timestamp"] = self.timestamp.isoformat() if self.timestamp else None
        return item


@dataclass(frozen=True)
class CandidateSite:
    name: str
    latitude: float
    longitude: float


def _clean_key(key: str) -> str:
    return key.strip().lower().replace(" ", "_").replace("-", "_")


def _first(row: Mapping[str, Any], keys: Sequence[str]) -> Any:
    normalised = {_clean_key(str(k)): v for k, v in row.items()}
    for key in keys:
        if key in normalised and normalised[key] not in (None, ""):
            return normalised[key]
    return None


def _to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        if math.isfinite(float(value)):
            return float(value)
        return None
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        parsed = float(text)
        if math.isfinite(parsed):
            return parsed
    except ValueError:
        return None
    return None


def _parse_timestamp(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None

    # Numeric Unix timestamps are occasionally present in CSV exports.
    try:
        numeric = float(text)
        if numeric > 10_000_000_000:  # milliseconds
            return datetime.fromtimestamp(numeric / 1000.0, tz=timezone.utc)
        if numeric > 1_000_000_000:  # seconds
            return datetime.fromtimestamp(numeric, tz=timezone.utc)
    except ValueError:
        pass

    candidates = [text]
    if text.endswith("Z"):
        candidates.append(text[:-1] + "+00:00")
    if " " in text and "T" not in text:
        candidates.append(text.replace(" ", "T"))

    formats = (
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
        "%d/%m/%Y %H:%M:%S",
        "%m/%d/%Y %H:%M:%S",
    )

    for candidate in candidates:
        try:
            parsed = datetime.fromisoformat(candidate)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except ValueError:
            pass
        for fmt in formats:
            try:
                parsed = datetime.strptime(candidate, fmt)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed.astimezone(timezone.utc)
            except ValueError:
                continue
    return None


def _parse_frequency_hz(row: Mapping[str, Any]) -> Optional[float]:
    value = _first(row, FREQ_COLUMNS)
    parsed = _to_float(value)
    if parsed is None:
        return None
    # If value looks like MHz, convert to Hz. Otherwise leave as Hz.
    # MOTH target range is 5 MHz to 6 GHz.
    if parsed < 100_000:
        parsed *= 1_000_000.0
    return parsed


def parse_csv_file(path: str | Path) -> List[MothRecord]:
    path = Path(path)
    records: List[MothRecord] = []
    with path.open("r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for idx, row in enumerate(reader, start=2):
            records.append(
                MothRecord(
                    timestamp=_parse_timestamp(_first(row, TIMESTAMP_COLUMNS)),
                    latitude=_to_float(_first(row, LAT_COLUMNS)),
                    longitude=_to_float(_first(row, LON_COLUMNS)),
                    frequency_hz=_parse_frequency_hz(row),
                    dbm=_to_float(_first(row, DBM_COLUMNS)),
                    source_file=path.name,
                    row_number=idx,
                )
            )
    return records


def parse_csv_files(paths: Sequence[str | Path]) -> List[MothRecord]:
    records: List[MothRecord] = []
    for path in paths:
        records.extend(parse_csv_file(path))
    return records


def record_from_mapping(row: Mapping[str, Any], source_file: str = "inline", row_number: int = 0) -> MothRecord:
    return MothRecord(
        timestamp=_parse_timestamp(_first(row, TIMESTAMP_COLUMNS)),
        latitude=_to_float(_first(row, LAT_COLUMNS)),
        longitude=_to_float(_first(row, LON_COLUMNS)),
        frequency_hz=_parse_frequency_hz(row),
        dbm=_to_float(_first(row, DBM_COLUMNS)),
        source_file=source_file,
        row_number=row_number,
    )


def records_from_mappings(rows: Iterable[Mapping[str, Any]], source_file: str = "inline") -> List[MothRecord]:
    return [record_from_mapping(row, source_file=source_file, row_number=i) for i, row in enumerate(rows, start=1)]


def valid_gps(record: MothRecord) -> bool:
    if record.latitude is None or record.longitude is None:
        return False
    if not (-90.0 <= record.latitude <= 90.0 and -180.0 <= record.longitude <= 180.0):
        return False
    if abs(record.latitude) < 1e-9 and abs(record.longitude) < 1e-9:
        return False
    return True


def in_frequency_range(record: MothRecord, min_hz: float = 5_000_000.0, max_hz: float = 6_000_000_000.0) -> bool:
    return record.frequency_hz is not None and min_hz <= record.frequency_hz <= max_hz


def band_for_frequency(frequency_hz: Optional[float], window_mhz: float = DEFAULT_GNSS_WINDOW_MHZ) -> Optional[str]:
    if frequency_hz is None:
        return None
    width_hz = window_mhz * 1_000_000.0
    for name, centre in GNSS_BANDS_HZ.items():
        if abs(frequency_hz - centre) <= width_hz:
            return name
    return None


def filter_bands(records: Sequence[MothRecord], bands: Sequence[str], window_mhz: float = DEFAULT_GNSS_WINDOW_MHZ) -> List[MothRecord]:
    selected = {band.upper() for band in bands}
    return [record for record in records if band_for_frequency(record.frequency_hz, window_mhz) in selected]


def _record_fingerprint(record: MothRecord) -> str:
    raw = f"{record.timestamp}|{record.latitude}|{record.longitude}|{record.frequency_hz}|{record.dbm}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _category_from_score(score: float) -> str:
    if score >= 80:
        return "HIGH"
    if score >= 55:
        return "MEDIUM"
    if score >= 30:
        return "LOW"
    return "NO DATA"


def calculate_data_quality(records: Sequence[MothRecord], expected_bands: Sequence[str] = ("L1", "L2", "L5")) -> Dict[str, Any]:
    total = len(records)
    if total == 0:
        return {
            "category": "NO DATA",
            "score": 0,
            "reason": "No records were available for analysis.",
            "recommendation": "Upload at least one MOTH/LAMP CSV before scoring candidates or launch windows.",
            "checks": [],
            "metrics": {"row_count": 0},
        }

    gps_valid = sum(1 for r in records if valid_gps(r))
    zero_zero = sum(1 for r in records if r.latitude is not None and r.longitude is not None and abs(r.latitude) < 1e-9 and abs(r.longitude) < 1e-9)
    timestamps = sorted(r.timestamp for r in records if r.timestamp is not None)
    timestamp_valid = len(timestamps)
    freq_valid = sum(1 for r in records if in_frequency_range(r))
    sources = {r.source_file for r in records if r.source_file}

    span_minutes = 0.0
    gaps_minutes: List[float] = []
    if len(timestamps) >= 2:
        span_minutes = (timestamps[-1] - timestamps[0]).total_seconds() / 60.0
        for previous, current in zip(timestamps, timestamps[1:]):
            gap = (current - previous).total_seconds() / 60.0
            if gap > 45.0:
                gaps_minutes.append(round(gap, 1))

    fingerprints: Dict[str, int] = {}
    for record in records:
        fp = _record_fingerprint(record)
        fingerprints[fp] = fingerprints.get(fp, 0) + 1
    duplicate_rows = sum(count - 1 for count in fingerprints.values() if count > 1)

    covered_bands = sorted({band for band in (band_for_frequency(r.frequency_hz) for r in records) if band})
    missing_bands = sorted(set(b.upper() for b in expected_bands) - set(covered_bands))

    gps_pct = gps_valid / total * 100.0
    timestamp_pct = timestamp_valid / total * 100.0
    freq_pct = freq_valid / total * 100.0

    score = 100.0
    if gps_pct < 95:
        score -= min(35.0, (95 - gps_pct) * 0.7)
    if zero_zero:
        score -= min(20.0, zero_zero / total * 100.0)
    if timestamp_pct < 98:
        score -= min(25.0, (98 - timestamp_pct) * 0.6)
    if freq_pct < 98:
        score -= min(25.0, (98 - freq_pct) * 0.6)
    if span_minutes < 30:
        score -= 25.0
    elif span_minutes < 120:
        score -= 10.0
    if len(sources) < 2:
        score -= 8.0
    if len(gaps_minutes) > 0:
        score -= min(15.0, len(gaps_minutes) * 3.0)
    if duplicate_rows > 0:
        score -= min(15.0, duplicate_rows / total * 100.0)
    if missing_bands:
        score -= min(20.0, len(missing_bands) * 7.0)

    score = max(0.0, min(100.0, score))
    category = _category_from_score(score)

    reasons: List[str] = []
    if gps_pct >= 95:
        reasons.append("good GPS coverage")
    else:
        reasons.append(f"GPS coverage is only {gps_pct:.1f}%")
    if span_minutes >= 120:
        reasons.append("adequate time span")
    else:
        reasons.append("limited time span")
    if missing_bands:
        reasons.append("missing expected band coverage: " + ", ".join(missing_bands))
    else:
        reasons.append("expected GNSS-band coverage present")
    if duplicate_rows:
        reasons.append(f"{duplicate_rows} duplicate rows detected")
    if gaps_minutes:
        reasons.append(f"{len(gaps_minutes)} time gaps over 45 minutes")

    if category == "HIGH":
        recommendation = "Proceed to candidate or launch-window validation, then confirm with controlled field checks."
    elif category == "MEDIUM":
        recommendation = "Use results cautiously and collect one additional targeted scan before briefing as ready."
    elif category == "LOW":
        recommendation = "Do not rely on this dataset for a decision until GPS, timing, duplicates or band coverage are corrected."
    else:
        recommendation = "Upload clean scans before using the decision cards."

    checks = [
        {"check": "Valid GPS percentage", "value": f"{gps_pct:.1f}%", "status": "PASS" if gps_pct >= 95 else "CHECK", "why": "Bad GPS makes map output unreliable."},
        {"check": "0,0 coordinate rows", "value": zero_zero, "status": "PASS" if zero_zero == 0 else "FAIL", "why": "0,0 rows must be excluded."},
        {"check": "Time span covered", "value": f"{span_minutes:.1f} min", "status": "PASS" if span_minutes >= 120 else "CHECK", "why": "Short scans may mislead."},
        {"check": "Gaps in time", "value": len(gaps_minutes), "status": "PASS" if len(gaps_minutes) == 0 else "CHECK", "why": "Missing periods hide patterns."},
        {"check": "Scan count", "value": len(sources), "status": "PASS" if len(sources) >= 2 else "CHECK", "why": "Multiple scans improve confidence."},
        {"check": "Duplicate rows", "value": duplicate_rows, "status": "PASS" if duplicate_rows == 0 else "CHECK", "why": "Duplicates distort density."},
        {"check": "Frequency coverage", "value": ", ".join(covered_bands) if covered_bands else "None", "status": "PASS" if not missing_bands else "CHECK", "why": "Confirms relevant bands were scanned."},
        {"check": "MOTH nominal range rows", "value": f"{freq_pct:.1f}%", "status": "PASS" if freq_pct >= 98 else "CHECK", "why": "Expected record frequencies should fall between 5 MHz and 6 GHz."},
    ]

    return {
        "category": category,
        "score": round(score, 1),
        "reason": "; ".join(reasons) + ".",
        "recommendation": recommendation,
        "checks": checks,
        "metrics": {
            "row_count": total,
            "valid_gps_percent": round(gps_pct, 1),
            "zero_zero_rows": zero_zero,
            "timestamp_valid_percent": round(timestamp_pct, 1),
            "frequency_valid_percent": round(freq_pct, 1),
            "scan_count": len(sources),
            "duplicate_rows": duplicate_rows,
            "time_span_minutes": round(span_minutes, 1),
            "gap_count": len(gaps_minutes),
            "largest_gap_minutes": max(gaps_minutes) if gaps_minutes else 0,
            "covered_bands": covered_bands,
            "missing_bands": missing_bands,
        },
    }


def _floor_to_step(dt: datetime, step_minutes: int) -> datetime:
    dt = dt.astimezone(timezone.utc)
    minute = (dt.minute // step_minutes) * step_minutes
    return dt.replace(minute=minute, second=0, microsecond=0)


def _launch_category(score: float, spike_count: int) -> str:
    if score >= 82 and spike_count == 0:
        return "RECOMMENDED"
    if score >= 62:
        return "BEST VIABLE"
    if score >= 42:
        return "LEAST-BUSY OBSERVED"
    return "AVOID IF POSSIBLE"


def rank_launch_windows(
    records: Sequence[MothRecord],
    bands: Sequence[str] = ("L1", "L2", "L5"),
    window_minutes: int = 30,
    step_minutes: int = 10,
    gnss_window_mhz: float = DEFAULT_GNSS_WINDOW_MHZ,
    spike_threshold_dbm: float = DEFAULT_SPIKE_THRESHOLD_DBM,
    local_utc_offset_hours: int = 3,
    max_windows: int = 20,
) -> Dict[str, Any]:
    selected_bands = [band.upper() for band in bands]
    band_records = [r for r in records if r.timestamp is not None and r.frequency_hz is not None and band_for_frequency(r.frequency_hz, gnss_window_mhz) in selected_bands]
    if not band_records:
        return {
            "decision": {
                "title": "Best launch timing",
                "category": "NO DATA",
                "score": 0,
                "confidence": "Low",
                "utc_window": None,
                "local_window": None,
                "reason": "No matching GNSS-band records were available.",
                "caution": "Do not make a timing decision from this filter set.",
                "next_action": "Select scans with L1/L2/L5 coverage or collect a new scan.",
            },
            "windows": [],
            "series": [],
        }

    start = _floor_to_step(min(r.timestamp for r in band_records if r.timestamp), step_minutes)
    latest = max(r.timestamp for r in band_records if r.timestamp)
    window_delta = timedelta(minutes=window_minutes)
    step_delta = timedelta(minutes=step_minutes)
    windows: List[Dict[str, Any]] = []
    series: List[Dict[str, Any]] = []

    current = start
    dataset_covered_bands = {band_for_frequency(r.frequency_hz, gnss_window_mhz) for r in band_records}
    missing_dataset_bands = sorted(set(selected_bands) - {b for b in dataset_covered_bands if b})

    while current <= latest:
        end = current + window_delta
        inside = [r for r in band_records if r.timestamp is not None and current <= r.timestamp < end]
        counts_by_band = {band: 0 for band in selected_bands}
        strongest_by_band: Dict[str, Optional[float]] = {band: None for band in selected_bands}
        for record in inside:
            band = band_for_frequency(record.frequency_hz, gnss_window_mhz)
            if band in counts_by_band:
                counts_by_band[band] += 1
                if record.dbm is not None:
                    strongest_by_band[band] = record.dbm if strongest_by_band[band] is None else max(strongest_by_band[band], record.dbm)

        total_count = len(inside)
        spike_count = sum(1 for r in inside if r.dbm is not None and r.dbm >= spike_threshold_dbm)
        active_band_count = sum(1 for count in counts_by_band.values() if count > 0)
        max_dbm = max([r.dbm for r in inside if r.dbm is not None], default=None)

        # Lower event counts and fewer strong spikes are better. Missing dataset
        # band coverage lowers confidence but does not fabricate activity.
        count_penalty = min(50.0, total_count * 1.8)
        spike_penalty = min(30.0, spike_count * 8.0)
        spread_penalty = max(0.0, active_band_count - 1) * 3.0
        missing_coverage_penalty = min(18.0, len(missing_dataset_bands) * 6.0)
        score = max(0.0, 100.0 - count_penalty - spike_penalty - spread_penalty - missing_coverage_penalty)
        category = _launch_category(score, spike_count)
        confidence = "High" if not missing_dataset_bands and total_count >= 0 and len(records) >= 100 else "Medium" if not missing_dataset_bands else "Low"

        local_start = current + timedelta(hours=local_utc_offset_hours)
        local_end = end + timedelta(hours=local_utc_offset_hours)
        window = {
            "start_utc": current.isoformat(),
            "end_utc": end.isoformat(),
            "utc_window": f"{current.strftime('%H:%M')}-{end.strftime('%H:%M')} UTC",
            "local_window": f"{local_start.strftime('%H:%M')}-{local_end.strftime('%H:%M')} local UTC{local_utc_offset_hours:+d}",
            "category": category,
            "score": round(score, 1),
            "confidence": confidence,
            "event_count": total_count,
            "spike_count": spike_count,
            "max_dbm": max_dbm,
            "counts_by_band": counts_by_band,
            "strongest_by_band": strongest_by_band,
        }
        windows.append(window)
        series.append({"time_utc": current.isoformat(), "counts_by_band": counts_by_band, "event_count": total_count})
        current += step_delta

    windows.sort(key=lambda item: (-item["score"], item["spike_count"], item["event_count"], item["start_utc"]))
    best = windows[0]
    most_affected_band = max(best["counts_by_band"], key=best["counts_by_band"].get) if best["counts_by_band"] else "N/A"
    reason = (
        f"Lowest scored window has {best['event_count']} GNSS-band events, "
        f"{best['spike_count']} spikes at or above {spike_threshold_dbm:g} dBm, "
        f"and most affected band {most_affected_band}."
    )
    caution = "Not a clean window; validate before launch." if best["category"] != "RECOMMENDED" else "Still requires authorised operational approval and normal UAS safety checks."
    next_action = "Review L1/L2/L5 graph, check spikes, and generate launch brief."

    decision = {
        "title": "Best launch timing",
        "category": best["category"],
        "score": best["score"],
        "confidence": best["confidence"],
        "utc_window": best["utc_window"],
        "local_window": best["local_window"],
        "reason": reason,
        "caution": caution,
        "next_action": next_action,
        "metrics": {
            "event_count": best["event_count"],
            "spike_count": best["spike_count"],
            "counts_by_band": best["counts_by_band"],
            "missing_dataset_bands": missing_dataset_bands,
            "spike_threshold_dbm": spike_threshold_dbm,
            "most_affected_band": most_affected_band,
        },
    }
    return {"decision": decision, "windows": windows[:max_windows], "series": series}


def haversine_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    earth_radius_m = 6_371_000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = math.sin(delta_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return earth_radius_m * c


def _percentile(values: Sequence[float], percentile: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    k = (len(ordered) - 1) * (percentile / 100.0)
    floor = math.floor(k)
    ceil = math.ceil(k)
    if floor == ceil:
        return ordered[int(k)]
    return ordered[floor] * (ceil - k) + ordered[ceil] * (k - floor)


def _normalise_dbm(dbm: Optional[float], low: float = -105.0, high: float = -50.0) -> float:
    if dbm is None:
        return 0.0
    return max(0.0, min(1.0, (dbm - low) / (high - low)))


def score_candidate_sites(
    records: Sequence[MothRecord],
    candidates: Sequence[CandidateSite | Mapping[str, Any]],
    target_bands: Sequence[str] = ("L1", "L2", "L5"),
    radius_meters: float = 100.0,
    strong_non_target_threshold_dbm: float = -65.0,
    gnss_window_mhz: float = DEFAULT_GNSS_WINDOW_MHZ,
) -> Dict[str, Any]:
    normalised_candidates: List[CandidateSite] = []
    for item in candidates:
        if isinstance(item, CandidateSite):
            normalised_candidates.append(item)
        else:
            normalised_candidates.append(
                CandidateSite(
                    name=str(item.get("name") or f"Candidate {len(normalised_candidates)+1}"),
                    latitude=float(item.get("latitude", item.get("lat"))),
                    longitude=float(item.get("longitude", item.get("lon", item.get("lng")))),
                )
            )

    if not normalised_candidates:
        return {
            "decision": {
                "title": "Recommended antenna candidate",
                "candidate": None,
                "score": 0,
                "confidence": "Low",
                "reason": "No candidate sites were provided.",
                "next_action": "Click the map to add candidate sites before scoring.",
            },
            "candidates": [],
        }

    selected_bands = {band.upper() for band in target_bands}
    scored: List[Dict[str, Any]] = []
    valid_records = [r for r in records if valid_gps(r) and r.frequency_hz is not None]

    for candidate in normalised_candidates:
        nearby = [r for r in valid_records if haversine_meters(candidate.latitude, candidate.longitude, r.latitude or 0.0, r.longitude or 0.0) <= radius_meters]
        target = [r for r in nearby if band_for_frequency(r.frequency_hz, gnss_window_mhz) in selected_bands]
        non_target_strong = [r for r in nearby if band_for_frequency(r.frequency_hz, gnss_window_mhz) not in selected_bands and r.dbm is not None and r.dbm >= strong_non_target_threshold_dbm]
        dbms = [r.dbm for r in target if r.dbm is not None]
        lower_tail = _percentile(dbms, 10)
        median_dbm = statistics.median(dbms) if dbms else None
        event_score = min(20.0, math.log1p(len(target)) * 5.0)
        lower_tail_score = _normalise_dbm(lower_tail) * 35.0
        median_score = _normalise_dbm(median_dbm) * 15.0
        confidence_score = min(20.0, len(nearby) / 5.0)
        non_target_penalty = min(30.0, len(non_target_strong) * 4.0)
        score = max(0.0, min(100.0, event_score + lower_tail_score + median_score + confidence_score - non_target_penalty + 10.0))
        if len(target) >= 25 and len(nearby) >= 30:
            confidence = "High"
        elif len(target) >= 8 and len(nearby) >= 10:
            confidence = "Medium"
        else:
            confidence = "Low"
        timeline_status = "GOOD" if confidence != "Low" and len(non_target_strong) < 5 else "CHECK" if len(target) else "UNDEFINED"
        scored.append(
            {
                "candidate": candidate.name,
                "latitude": candidate.latitude,
                "longitude": candidate.longitude,
                "score": round(score, 1),
                "confidence": confidence,
                "target_detections": len(target),
                "nearby_detections": len(nearby),
                "lower_tail_dbm": round(lower_tail, 1) if lower_tail is not None else None,
                "median_dbm": round(median_dbm, 1) if median_dbm is not None else None,
                "strong_non_target_events": len(non_target_strong),
                "timeline_status": timeline_status,
            }
        )

    scored.sort(key=lambda item: (-item["score"], item["strong_non_target_events"], -item["target_detections"]))
    for rank, item in enumerate(scored, start=1):
        item["rank"] = rank
    best = scored[0]
    reason = (
        f"Ranked highest with {best['target_detections']} target-band detections, "
        f"lower-tail strength {best['lower_tail_dbm']} dBm, median {best['median_dbm']} dBm, "
        f"and {best['strong_non_target_events']} strong non-target events."
    )
    next_action = "Repeat controlled survey at this point and compare with the next two candidates."
    decision = {
        "title": "Recommended antenna candidate",
        "candidate": best["candidate"],
        "score": best["score"],
        "confidence": best["confidence"],
        "reason": reason,
        "next_action": next_action,
        "metrics": best,
    }
    return {"decision": decision, "candidates": scored}


def explain_result(result: Mapping[str, Any], result_type: str) -> str:
    result_type = result_type.lower().strip()
    if result_type == "launch":
        metrics = result.get("metrics", {}) if isinstance(result.get("metrics", {}), Mapping) else {}
        category = result.get("category", "NO DATA")
        event_count = metrics.get("event_count", "unknown")
        spike_count = metrics.get("spike_count", "unknown")
        threshold = metrics.get("spike_threshold_dbm", DEFAULT_SPIKE_THRESHOLD_DBM)
        most_band = metrics.get("most_affected_band", "N/A")
        missing = metrics.get("missing_dataset_bands") or []
        sentence = (
            f"This window was selected because it produced the strongest deterministic score among available windows: "
            f"{event_count} selected-band events, {spike_count} spikes at or above {threshold:g} dBm, and most affected band {most_band}. "
            f"It is classed as {category}."
        )
        if missing:
            sentence += " Confidence is reduced because dataset coverage is missing: " + ", ".join(missing) + "."
        return sentence

    if result_type == "candidate":
        metrics = result.get("metrics", {}) if isinstance(result.get("metrics", {}), Mapping) else {}
        return (
            f"This candidate ranked highest because it had {metrics.get('target_detections', 'unknown')} target-band detections, "
            f"lower-tail strength {metrics.get('lower_tail_dbm', 'unknown')} dBm, median strength {metrics.get('median_dbm', 'unknown')} dBm, "
            f"and {metrics.get('strong_non_target_events', 'unknown')} strong non-target events. "
            f"Confidence is {result.get('confidence', 'unknown')} and the next action is: {result.get('next_action', 'validate in the field')}"
        )

    if result_type == "data_quality":
        return (
            f"Data quality is {result.get('category', 'NO DATA')} with score {result.get('score', 0)}. "
            f"Reason: {result.get('reason', 'No reason available')} Recommendation: {result.get('recommendation', 'Collect more data.')}"
        )
    return "No deterministic explanation is available for this result type."


def render_candidate_report(decision: Mapping[str, Any]) -> str:
    metrics = decision.get("metrics", {}) if isinstance(decision.get("metrics", {}), Mapping) else {}
    return f"""# Candidate Site Report

Candidate name: {decision.get('candidate', 'N/A')}
Score: {decision.get('score', 'N/A')}/100
Confidence: {decision.get('confidence', 'N/A')}

## Why selected or rejected
{decision.get('reason', 'No reason available.')}

## Evidence summary
- Target detections: {metrics.get('target_detections', 'N/A')}
- Lower-tail dBm: {metrics.get('lower_tail_dbm', 'N/A')}
- Median dBm: {metrics.get('median_dbm', 'N/A')}
- Strong non-target events: {metrics.get('strong_non_target_events', 'N/A')}
- Timeline status: {metrics.get('timeline_status', 'N/A')}

## Recommended next action
{decision.get('next_action', 'Repeat controlled survey and validate before operational use.')}

RF planning aid only. Final launch decision requires authorised operational approval and normal UAS safety checks.
"""


def render_launch_report(decision: Mapping[str, Any]) -> str:
    metrics = decision.get("metrics", {}) if isinstance(decision.get("metrics", {}), Mapping) else {}
    counts_by_band = metrics.get("counts_by_band", {})
    if isinstance(counts_by_band, Mapping):
        band_line = ", ".join(f"{band}: {count}" for band, count in counts_by_band.items())
    else:
        band_line = "N/A"
    return f"""# Launch Window Report

Recommended timing: {decision.get('utc_window', 'N/A')}
Local time conversion: {decision.get('local_window', 'N/A')}
RF score: {decision.get('score', 'N/A')}/100
Category: {decision.get('category', 'N/A')}
Confidence: {decision.get('confidence', 'N/A')}

## L1/L2/L5 status
{band_line}

## Spike status
Spikes at or above threshold: {metrics.get('spike_count', 'N/A')}
Spike threshold dBm: {metrics.get('spike_threshold_dbm', 'N/A')}

## Pattern and caution
{decision.get('reason', 'No reason available.')}
Caution: {decision.get('caution', 'Validate before launch.')}

## Operator checklist
- Confirm scans selected are current and representative.
- Review graph and spike list.
- Confirm UAS airspace, weather, aircraft health, C2 link, GNSS receiver and crew readiness checks.
- Record final approval authority.

RF planning aid only. Final launch decision requires authorised operational approval and normal UAS safety checks.
"""


def _demo_records() -> List[MothRecord]:
    base = datetime(2026, 5, 12, 6, 0, tzinfo=timezone.utc)
    rows: List[MothRecord] = []
    for i in range(36):
        timestamp = base + timedelta(minutes=10 * i)
        for band_name, centre in (("L1", GNSS_BANDS_HZ["L1"]), ("L2", GNSS_BANDS_HZ["L2"]), ("L5", GNSS_BANDS_HZ["L5"])):
            event_count = 1 if 8 <= i <= 14 and band_name != "L2" else 3 if 20 <= i <= 25 else 0
            for event in range(event_count):
                rows.append(
                    MothRecord(
                        timestamp=timestamp,
                        latitude=2.046 + (i % 5) * 0.0002,
                        longitude=45.318 + (event % 4) * 0.0002,
                        frequency_hz=centre + (event - 1) * 500_000,
                        dbm=-82 + event * 4 if i < 20 else -58 + event,
                        source_file="demo.csv",
                        row_number=len(rows) + 1,
                    )
                )
    return rows


if __name__ == "__main__":
    demo = _demo_records()
    print("DATA QUALITY")
    print(calculate_data_quality(demo))
    print("\nLAUNCH")
    launch = rank_launch_windows(demo)
    print(launch["decision"])
    print("\nEXPLAIN")
    print(explain_result(launch["decision"], "launch"))
