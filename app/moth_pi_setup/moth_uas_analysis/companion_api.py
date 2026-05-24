from __future__ import annotations

import math
import os
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from statistics import median
from typing import Any

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

APP_VERSION = "0.7.0"
DB_PATH = Path(os.environ.get("MOTH_DB_PATH", "/home/woodyrone/moth_pi_setup/data/moth.sqlite"))
STATIC_DIR = Path(__file__).with_name("static")

app = FastAPI(title="MOTH UAS RF Analysis Companion", version=APP_VERSION)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# GNSS / navigation bands requested for time graphs.
# Windows are deliberately wider than nominal centre frequencies because the MOTH is a survey/event logger.
GNSS_BANDS = {
    "L1": {"centre_hz": 1575.42e6, "default_width_mhz": 20.0, "purpose": "GNSS navigation / UAS positioning risk band"},
    "L2": {"centre_hz": 1227.60e6, "default_width_mhz": 20.0, "purpose": "GNSS navigation / UAS positioning risk band"},
    "L5": {"centre_hz": 1176.45e6, "default_width_mhz": 20.0, "purpose": "GNSS aviation/navigation risk band"},
    # Kept optional because the user asked about L3; many workflows will not need it.
    "L3": {"centre_hz": 1381.05e6, "default_width_mhz": 20.0, "purpose": "Optional/legacy analysis window"},
}


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    v = value.strip()
    if not v:
        return None
    try:
        if v.endswith("Z"):
            v = v[:-1] + "+00:00"
        dt = datetime.fromisoformat(v)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_ids(collection_ids: str | None) -> list[int]:
    if not collection_ids:
        return []
    out: list[int] = []
    for piece in collection_ids.replace(";", ",").split(","):
        piece = piece.strip()
        if piece:
            try:
                out.append(int(piece))
            except ValueError:
                pass
    return sorted(set(out))


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]


def band_range(label: str, width_mhz: float = 20.0) -> tuple[float, float]:
    info = GNSS_BANDS[label]
    centre = float(info["centre_hz"])
    half = float(width_mhz) * 1e6 / 2.0
    return centre - half, centre + half


def safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        f = float(value)
        if math.isfinite(f):
            return f
    except Exception:
        return None
    return None


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    vals = sorted(values)
    if len(vals) == 1:
        return vals[0]
    k = (len(vals) - 1) * p
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return vals[lo]
    return vals[lo] * (hi - k) + vals[hi] * (k - lo)


def build_where(
    *,
    collection_ids: str | None = None,
    start_utc: str | None = None,
    end_utc: str | None = None,
    min_hz: float | None = None,
    max_hz: float | None = None,
    min_dbm: float | None = None,
) -> tuple[str, list[Any]]:
    where = ["valid = 1", "timestamp_utc IS NOT NULL", "frequency_hz IS NOT NULL", "strength_dbm IS NOT NULL"]
    params: list[Any] = []
    ids = parse_ids(collection_ids)
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
    if min_hz is not None:
        where.append("frequency_hz >= ?")
        params.append(float(min_hz))
    if max_hz is not None:
        where.append("frequency_hz <= ?")
        params.append(float(max_hz))
    if min_dbm is not None:
        where.append("strength_dbm >= ?")
        params.append(float(min_dbm))
    return " AND ".join(where), params


def load_events(
    *,
    collection_ids: str | None = None,
    start_utc: str | None = None,
    end_utc: str | None = None,
    min_hz: float | None = None,
    max_hz: float | None = None,
    min_dbm: float | None = None,
    limit: int = 1_000_000,
) -> list[dict[str, Any]]:
    where, params = build_where(
        collection_ids=collection_ids,
        start_utc=start_utc,
        end_utc=end_utc,
        min_hz=min_hz,
        max_hz=max_hz,
        min_dbm=min_dbm,
    )
    conn = connect()
    rows = rows_to_dicts(conn.execute(
        f"""
        SELECT event_id, collection_id, timestamp_utc, frequency_hz, strength_dbm, lat, lon
        FROM moth_events
        WHERE {where}
        ORDER BY timestamp_utc ASC
        LIMIT ?
        """,
        params + [int(limit)],
    ).fetchall())
    conn.close()
    return rows


def bucket_start(dt: datetime, bucket_minutes: int) -> datetime:
    seconds = int(dt.timestamp())
    bucket = max(60, int(bucket_minutes) * 60)
    return datetime.fromtimestamp((seconds // bucket) * bucket, tz=timezone.utc)


def dbm_cleanliness(max_dbm: float | None) -> float:
    """Score lower RF activity as cleaner. This is interference-risk oriented."""
    if max_dbm is None:
        return 0.65  # no event can mean clean or simply not detected; treat as neutral-good but not perfect.
    # -90 -> 1.0, -55 -> 0.0
    return max(0.0, min(1.0, (-55.0 - max_dbm) / (-55.0 + 90.0)))


def label_score(score: float) -> str:
    if score >= 75:
        return "GOOD"
    if score >= 50:
        return "CHECK"
    return "AVOID"


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (STATIC_DIR / "uas_rf.html").read_text(encoding="utf-8")


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "version": APP_VERSION, "db_path": str(DB_PATH), "db_exists": DB_PATH.exists()}


@app.get("/api/collections")
def collections() -> list[dict[str, Any]]:
    conn = connect()
    rows = rows_to_dicts(conn.execute(
        """
        SELECT c.collection_id, c.collection_name, c.device_serial, c.upload_time_utc,
               COALESCE(e.event_count, 0) AS event_count,
               COALESCE(e.valid_event_count, 0) AS valid_event_count,
               e.first_timestamp_utc, e.last_timestamp_utc
        FROM moth_collections c
        LEFT JOIN (
          SELECT collection_id,
                 COUNT(*) AS event_count,
                 SUM(CASE WHEN valid = 1 THEN 1 ELSE 0 END) AS valid_event_count,
                 MIN(timestamp_utc) AS first_timestamp_utc,
                 MAX(timestamp_utc) AS last_timestamp_utc
          FROM moth_events GROUP BY collection_id
        ) e ON e.collection_id = c.collection_id
        ORDER BY c.collection_id DESC
        """
    ).fetchall())
    conn.close()
    return rows


@app.get("/api/gnss-timeline")
def gnss_timeline(
    collection_ids: str | None = None,
    start_utc: str | None = None,
    end_utc: str | None = None,
    bucket_minutes: int = Query(default=15, ge=1, le=1440),
    width_mhz: float = Query(default=20.0, ge=1.0, le=100.0),
    include_l3: bool = False,
) -> dict[str, Any]:
    bands = ["L1", "L2", "L5"] + (["L3"] if include_l3 else [])
    # Fetch only broad GNSS span for performance.
    ranges = [band_range(b, width_mhz) for b in bands]
    min_hz = min(r[0] for r in ranges)
    max_hz = max(r[1] for r in ranges)
    events = load_events(collection_ids=collection_ids, start_utc=start_utc, end_utc=end_utc, min_hz=min_hz, max_hz=max_hz)

    buckets: dict[datetime, dict[str, Any]] = {}
    for e in events:
        dt = parse_dt(e.get("timestamp_utc"))
        freq = safe_float(e.get("frequency_hz"))
        dbm = safe_float(e.get("strength_dbm"))
        if dt is None or freq is None or dbm is None:
            continue
        bdt = bucket_start(dt, bucket_minutes)
        item = buckets.setdefault(bdt, {"bucket_start_utc": iso(bdt), "bands": {}})
        for label in bands:
            lo, hi = band_range(label, width_mhz)
            if lo <= freq <= hi:
                band = item["bands"].setdefault(label, {"event_count": 0, "strengths": []})
                band["event_count"] += 1
                band["strengths"].append(dbm)

    points: list[dict[str, Any]] = []
    for bdt in sorted(buckets):
        raw = buckets[bdt]
        out = {"bucket_start_utc": raw["bucket_start_utc"], "bands": {}}
        for label in bands:
            vals = raw["bands"].get(label, {"event_count": 0, "strengths": []})
            strengths = vals["strengths"]
            out["bands"][label] = {
                "event_count": vals["event_count"],
                "avg_dbm": round(sum(strengths) / len(strengths), 1) if strengths else None,
                "max_dbm": round(max(strengths), 1) if strengths else None,
                "p95_dbm": round(percentile(strengths, 0.95), 1) if strengths else None,
            }
        points.append(out)
    return {
        "bands": {b: {**GNSS_BANDS[b], "min_hz": band_range(b, width_mhz)[0], "max_hz": band_range(b, width_mhz)[1]} for b in bands},
        "bucket_minutes": bucket_minutes,
        "width_mhz": width_mhz,
        "points": points,
        "interpretation": "Higher or spiky RF activity in GNSS windows is treated as interference-risk evidence, not as proof of GNSS quality.",
    }


@app.get("/api/spectrum")
def spectrum(
    collection_ids: str | None = None,
    start_utc: str | None = None,
    end_utc: str | None = None,
    min_hz: float | None = None,
    max_hz: float | None = None,
    min_dbm: float | None = None,
    bin_mhz: float = Query(default=1.0, ge=0.05, le=100.0),
    limit: int = Query(default=1_000_000, ge=1000, le=2_000_000),
) -> dict[str, Any]:
    events = load_events(collection_ids=collection_ids, start_utc=start_utc, end_utc=end_utc, min_hz=min_hz, max_hz=max_hz, min_dbm=min_dbm, limit=limit)
    width = float(bin_mhz) * 1e6
    grouped: dict[int, list[float]] = defaultdict(list)
    for e in events:
        freq = safe_float(e.get("frequency_hz"))
        dbm = safe_float(e.get("strength_dbm"))
        if freq is None or dbm is None:
            continue
        bucket = int(freq // width)
        grouped[bucket].append(dbm)
    bins = []
    all_counts = []
    all_max = []
    for bucket, vals in grouped.items():
        centre_hz = (bucket + 0.5) * width
        counts = len(vals)
        mx = max(vals)
        all_counts.append(counts)
        all_max.append(mx)
        bins.append({
            "frequency_mhz": round(centre_hz / 1e6, 4),
            "min_frequency_mhz": round(bucket * width / 1e6, 4),
            "max_frequency_mhz": round((bucket + 1) * width / 1e6, 4),
            "event_count": counts,
            "avg_dbm": round(sum(vals) / len(vals), 1),
            "max_dbm": round(mx, 1),
            "p95_dbm": round(percentile(vals, 0.95), 1),
        })
    count_p95 = percentile([float(c) for c in all_counts], 0.95) or 0
    max_p95 = percentile([float(m) for m in all_max], 0.95) or -999
    for b in bins:
        b["abnormal"] = bool(b["event_count"] >= count_p95 or b["max_dbm"] >= max_p95 or b["max_dbm"] >= -60)
        reasons = []
        if b["event_count"] >= count_p95 and count_p95 > 0:
            reasons.append("unusually frequent")
        if b["max_dbm"] >= max_p95 and max_p95 > -999:
            reasons.append("unusually strong")
        if b["max_dbm"] >= -60:
            reasons.append("strong signal above -60 dBm")
        b["abnormal_reason"] = ", ".join(reasons) if reasons else "normal range"
    bins.sort(key=lambda x: x["frequency_mhz"])
    return {
        "bin_mhz": bin_mhz,
        "event_count": len(events),
        "count_p95_threshold": round(count_p95, 1),
        "max_dbm_p95_threshold": round(max_p95, 1),
        "bins": bins,
    }


@app.get("/api/spikes")
def spikes(
    collection_ids: str | None = None,
    start_utc: str | None = None,
    end_utc: str | None = None,
    min_hz: float | None = None,
    max_hz: float | None = None,
    bucket_minutes: int = Query(default=15, ge=1, le=1440),
    bin_mhz: float = Query(default=5.0, ge=0.1, le=100.0),
    spike_dbm: float = Query(default=-60.0),
    limit: int = Query(default=1_000_000, ge=1000, le=2_000_000),
) -> dict[str, Any]:
    events = load_events(collection_ids=collection_ids, start_utc=start_utc, end_utc=end_utc, min_hz=min_hz, max_hz=max_hz, limit=limit)
    width = float(bin_mhz) * 1e6
    grouped: dict[tuple[datetime, int], dict[str, Any]] = {}
    for e in events:
        dt = parse_dt(e.get("timestamp_utc"))
        freq = safe_float(e.get("frequency_hz"))
        dbm = safe_float(e.get("strength_dbm"))
        if dt is None or freq is None or dbm is None:
            continue
        bdt = bucket_start(dt, bucket_minutes)
        fb = int(freq // width)
        g = grouped.setdefault((bdt, fb), {"strengths": [], "count": 0})
        g["strengths"].append(dbm)
        g["count"] += 1
    items = []
    for (bdt, fb), g in grouped.items():
        vals = g["strengths"]
        max_dbm = max(vals)
        p95 = percentile(vals, 0.95)
        is_spike = max_dbm >= spike_dbm or (p95 is not None and p95 >= spike_dbm)
        if is_spike:
            items.append({
                "bucket_start_utc": iso(bdt),
                "frequency_mhz": round((fb + 0.5) * width / 1e6, 4),
                "frequency_min_mhz": round(fb * width / 1e6, 4),
                "frequency_max_mhz": round((fb + 1) * width / 1e6, 4),
                "event_count": g["count"],
                "max_dbm": round(max_dbm, 1),
                "p95_dbm": round(p95, 1) if p95 is not None else None,
                "reason": f">= {spike_dbm} dBm threshold",
            })
    items.sort(key=lambda x: (x["bucket_start_utc"], x["frequency_mhz"]))
    return {"spike_dbm": spike_dbm, "bucket_minutes": bucket_minutes, "bin_mhz": bin_mhz, "spikes": items[:5000], "spike_count": len(items)}


@app.get("/api/pattern-of-life")
def pattern_of_life(
    collection_ids: str | None = None,
    min_hz: float | None = None,
    max_hz: float | None = None,
    start_utc: str | None = None,
    end_utc: str | None = None,
    bucket_minutes: int = Query(default=30, ge=5, le=1440),
    limit: int = Query(default=1_000_000, ge=1000, le=2_000_000),
) -> dict[str, Any]:
    events = load_events(collection_ids=collection_ids, min_hz=min_hz, max_hz=max_hz, start_utc=start_utc, end_utc=end_utc, limit=limit)
    by_hour: dict[int, list[float]] = defaultdict(list)
    by_bucket: dict[str, dict[str, Any]] = defaultdict(lambda: {"event_count": 0, "strengths": []})
    for e in events:
        dt = parse_dt(e.get("timestamp_utc"))
        dbm = safe_float(e.get("strength_dbm"))
        if dt is None or dbm is None:
            continue
        by_hour[dt.hour].append(dbm)
        bdt = bucket_start(dt, bucket_minutes)
        g = by_bucket[iso(bdt)]
        g["event_count"] += 1
        g["strengths"].append(dbm)
    hours = []
    for hour in range(24):
        vals = by_hour.get(hour, [])
        hours.append({
            "hour_utc": hour,
            "event_count": len(vals),
            "avg_dbm": round(sum(vals) / len(vals), 1) if vals else None,
            "max_dbm": round(max(vals), 1) if vals else None,
            "cleanliness_score_0_100": round((1.0 - min(1.0, len(vals) / max(1, max((len(v) for v in by_hour.values()), default=1)))) * 45 + (dbm_cleanliness(max(vals) if vals else None) * 55), 1),
        })
    buckets = []
    for key, g in sorted(by_bucket.items()):
        vals = g["strengths"]
        buckets.append({
            "bucket_start_utc": key,
            "event_count": g["event_count"],
            "avg_dbm": round(sum(vals) / len(vals), 1) if vals else None,
            "max_dbm": round(max(vals), 1) if vals else None,
        })
    return {
        "event_count": len(events),
        "bucket_minutes": bucket_minutes,
        "hourly": hours,
        "buckets": buckets,
        "interpretation": "Lower event count and lower max dBm suggest cleaner periods. Repeated high activity at the same time of day indicates a pattern-of-life risk window.",
    }


@app.get("/api/launch-windows")
def launch_windows(
    collection_ids: str | None = None,
    start_utc: str | None = None,
    end_utc: str | None = None,
    bucket_minutes: int = Query(default=30, ge=5, le=240),
    gnss_width_mhz: float = Query(default=20.0, ge=1.0, le=100.0),
    include_l3: bool = False,
    target_min_hz: float | None = None,
    target_max_hz: float | None = None,
) -> dict[str, Any]:
    bands = ["L1", "L2", "L5"] + (["L3"] if include_l3 else [])
    # Use all events for total noise, plus specific GNSS bands and optional target connectivity band.
    events = load_events(collection_ids=collection_ids, start_utc=start_utc, end_utc=end_utc)
    buckets: dict[datetime, dict[str, Any]] = defaultdict(lambda: {
        "total_count": 0,
        "total_strengths": [],
        "gnss_count": 0,
        "gnss_strengths": [],
        "target_count": 0,
        "target_strengths": [],
    })
    for e in events:
        dt = parse_dt(e.get("timestamp_utc"))
        freq = safe_float(e.get("frequency_hz"))
        dbm = safe_float(e.get("strength_dbm"))
        if dt is None or freq is None or dbm is None:
            continue
        bdt = bucket_start(dt, bucket_minutes)
        g = buckets[bdt]
        g["total_count"] += 1
        g["total_strengths"].append(dbm)
        for label in bands:
            lo, hi = band_range(label, gnss_width_mhz)
            if lo <= freq <= hi:
                g["gnss_count"] += 1
                g["gnss_strengths"].append(dbm)
                break
        if target_min_hz is not None and target_max_hz is not None and target_min_hz <= freq <= target_max_hz:
            g["target_count"] += 1
            g["target_strengths"].append(dbm)

    max_total_count = max((g["total_count"] for g in buckets.values()), default=1)
    max_gnss_count = max((g["gnss_count"] for g in buckets.values()), default=1)
    rows = []
    for bdt, g in sorted(buckets.items()):
        total_max = max(g["total_strengths"]) if g["total_strengths"] else None
        gnss_max = max(g["gnss_strengths"]) if g["gnss_strengths"] else None
        target_avg = sum(g["target_strengths"]) / len(g["target_strengths"]) if g["target_strengths"] else None
        low_total = 1.0 - min(1.0, g["total_count"] / max_total_count)
        low_gnss_count = 1.0 - min(1.0, g["gnss_count"] / max_gnss_count)
        low_gnss_strength = dbm_cleanliness(gnss_max)
        low_total_strength = dbm_cleanliness(total_max)
        if target_min_hz is not None and target_max_hz is not None:
            # If a desired connectivity/reference band is supplied, stronger is better.
            target_score = 0.0 if target_avg is None else max(0.0, min(1.0, (target_avg - (-95.0)) / 45.0))
            score = 100.0 * (0.22 * low_total + 0.25 * low_gnss_count + 0.22 * low_gnss_strength + 0.11 * low_total_strength + 0.20 * target_score)
        else:
            target_score = None
            score = 100.0 * (0.30 * low_total + 0.30 * low_gnss_count + 0.25 * low_gnss_strength + 0.15 * low_total_strength)
        reasons = []
        if g["gnss_count"] == 0:
            reasons.append("no GNSS-band events in this window")
        elif gnss_max is not None and gnss_max < -70:
            reasons.append("GNSS-band activity remains relatively low")
        else:
            reasons.append("GNSS-band activity/spikes present")
        if g["total_count"] < max_total_count * 0.25:
            reasons.append("low overall RF event density")
        if target_score is not None:
            reasons.append("target/reference band evidence included")
        rows.append({
            "bucket_start_utc": iso(bdt),
            "bucket_end_utc": iso(bdt + timedelta(minutes=bucket_minutes)),
            "clean_launch_score_0_100": round(score, 1),
            "recommendation": label_score(score),
            "total_event_count": g["total_count"],
            "total_max_dbm": round(total_max, 1) if total_max is not None else None,
            "gnss_event_count": g["gnss_count"],
            "gnss_max_dbm": round(gnss_max, 1) if gnss_max is not None else None,
            "target_event_count": g["target_count"],
            "target_avg_dbm": round(target_avg, 1) if target_avg is not None else None,
            "reasons": reasons,
        })
    ranked = sorted(rows, key=lambda r: r["clean_launch_score_0_100"], reverse=True)
    return {
        "definition": "RF-clean launch windows rank time buckets by low overall RF event density, low GNSS-band spike activity, and optional target/reference-band evidence. This is an RF planning aid only.",
        "bucket_minutes": bucket_minutes,
        "bands_assessed": bands,
        "gnss_width_mhz": gnss_width_mhz,
        "target_band_used": target_min_hz is not None and target_max_hz is not None,
        "top_windows": ranked[:10],
        "all_windows": rows,
        "limitations": [
            "MOTH logs detections/events, not guaranteed absence of RF energy.",
            "Clean RF timing does not replace weather, airspace, crew readiness, battery state, NOTAM/ATC/airport approval, or UAS system checks.",
            "GNSS-band spikes are treated as interference-risk indicators, not definitive proof of GNSS degradation.",
        ],
    }
