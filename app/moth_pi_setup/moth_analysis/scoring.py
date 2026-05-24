from __future__ import annotations

import math
from typing import Any

from .db import connect, rows_to_dicts
from .geo import clamp, haversine_m


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    if len(values) == 1:
        return values[0]
    k = (len(values) - 1) * p
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return values[int(k)]
    return values[f] * (c - k) + values[c] * (k - f)


def dbm_to_quality(dbm: float | None) -> float:
    if dbm is None:
        return 0.0
    return clamp((dbm - (-100.0)) / 60.0, 0.0, 1.0)


def load_valid_events(
    db_path=None,
    target_min_hz: float | None = None,
    target_max_hz: float | None = None,
    collection_ids: list[int] | None = None,
    start_utc: str | None = None,
    end_utc: str | None = None,
    min_dbm: float | None = None,
) -> list[dict[str, Any]]:
    where = ["valid = 1", "lat IS NOT NULL", "lon IS NOT NULL", "strength_dbm IS NOT NULL"]
    params: list[Any] = []
    if target_min_hz is not None:
        where.append("frequency_hz >= ?")
        params.append(target_min_hz)
    if target_max_hz is not None:
        where.append("frequency_hz <= ?")
        params.append(target_max_hz)
    if min_dbm is not None:
        where.append("strength_dbm >= ?")
        params.append(min_dbm)
    if collection_ids:
        placeholders = ",".join("?" for _ in collection_ids)
        where.append(f"collection_id IN ({placeholders})")
        params.extend(collection_ids)
    if start_utc:
        where.append("timestamp_utc >= ?")
        params.append(start_utc)
    if end_utc:
        where.append("timestamp_utc <= ?")
        params.append(end_utc)
    sql = "SELECT * FROM moth_events WHERE " + " AND ".join(where)
    conn = connect(db_path) if db_path else connect()
    rows = rows_to_dicts(conn.execute(sql, params).fetchall())
    conn.close()
    return rows


def score_candidate_sites(
    *,
    db_path=None,
    radius_m: float = 1500.0,
    target_min_hz: float | None = None,
    target_max_hz: float | None = None,
    collection_ids: list[int] | None = None,
    start_utc: str | None = None,
    end_utc: str | None = None,
    min_dbm: float | None = None,
) -> list[dict[str, Any]]:
    conn = connect(db_path) if db_path else connect()
    candidates = rows_to_dicts(conn.execute("SELECT * FROM candidate_sites ORDER BY name").fetchall())
    conn.close()

    target_events = load_valid_events(db_path, target_min_hz, target_max_hz, collection_ids, start_utc, end_utc, min_dbm)
    all_events = load_valid_events(db_path, None, None, collection_ids, start_utc, end_utc, min_dbm)

    scored: list[dict[str, Any]] = []
    for site in candidates:
        lat = float(site["lat"])
        lon = float(site["lon"])
        nearby_target: list[dict[str, Any]] = []
        nearby_all: list[dict[str, Any]] = []

        for event in target_events:
            d = haversine_m(lat, lon, float(event["lat"]), float(event["lon"]))
            if d <= radius_m:
                item = dict(event)
                item["distance_m"] = d
                nearby_target.append(item)
        for event in all_events:
            d = haversine_m(lat, lon, float(event["lat"]), float(event["lon"]))
            if d <= radius_m:
                item = dict(event)
                item["distance_m"] = d
                nearby_all.append(item)

        strengths = [float(e["strength_dbm"]) for e in nearby_target]
        p10 = percentile(strengths, 0.10)
        p50 = percentile(strengths, 0.50)
        target_count = len(nearby_target)
        all_count = len(nearby_all)

        if target_min_hz is not None or target_max_hz is not None:
            strong_non_target = [
                e for e in nearby_all
                if e.get("frequency_hz") is not None
                and not (
                    (target_min_hz is None or float(e["frequency_hz"]) >= target_min_hz)
                    and (target_max_hz is None or float(e["frequency_hz"]) <= target_max_hz)
                )
                and float(e["strength_dbm"]) >= -60.0
            ]
        else:
            strong_non_target = []

        lower_tail_quality = dbm_to_quality(p10)
        median_quality = dbm_to_quality(p50)
        availability = clamp(math.log1p(target_count) / math.log1p(250.0), 0.0, 1.0)
        data_confidence = clamp(target_count / 50.0, 0.0, 1.0)
        low_interference = 1.0 - clamp(len(strong_non_target) / 30.0, 0.0, 1.0)
        practical_score = clamp(float(site.get("practical_score") or 0.5), 0.0, 1.0)

        score = (
            0.25 * lower_tail_quality
            + 0.15 * median_quality
            + 0.20 * availability
            + 0.20 * low_interference
            + 0.10 * practical_score
            + 0.10 * data_confidence
        )

        scored.append({
            "site_id": site["site_id"],
            "name": site["name"],
            "lat": lat,
            "lon": lon,
            "antenna_height_agl_m": site.get("antenna_height_agl_m"),
            "practical_score": practical_score,
            "radius_m": radius_m,
            "target_event_count": target_count,
            "all_event_count": all_count,
            "strong_non_target_count": len(strong_non_target),
            "target_strength_p10_dbm": round(p10, 1) if p10 is not None else None,
            "target_strength_median_dbm": round(p50, 1) if p50 is not None else None,
            "lower_tail_quality": round(lower_tail_quality, 3),
            "target_availability": round(availability, 3),
            "low_interference": round(low_interference, 3),
            "data_confidence": round(data_confidence, 3),
            "score_0_100": round(score * 100.0, 1),
            "site_notes": site.get("site_notes"),
        })

    scored.sort(key=lambda x: x["score_0_100"], reverse=True)
    for idx, item in enumerate(scored, start=1):
        item["rank"] = idx
    return scored


def _event_is_target(event: dict[str, Any], target_min_hz: float | None, target_max_hz: float | None) -> bool:
    if target_min_hz is None and target_max_hz is None:
        return True
    f = event.get("frequency_hz")
    if f is None:
        return False
    ff = float(f)
    return (target_min_hz is None or ff >= target_min_hz) and (target_max_hz is None or ff <= target_max_hz)


def score_h3_suitability(
    *,
    db_path=None,
    resolution: int = 9,
    target_min_hz: float | None = None,
    target_max_hz: float | None = None,
    collection_ids: list[int] | None = None,
    start_utc: str | None = None,
    end_utc: str | None = None,
    min_dbm: float | None = None,
) -> list[dict[str, Any]]:
    if resolution not in (8, 9, 10, 11, 12):
        raise ValueError("resolution must be 8, 9, 10, 11, or 12")
    h3_col = f"h3_r{resolution}"
    events = load_valid_events(db_path, None, None, collection_ids, start_utc, end_utc, min_dbm)

    grouped: dict[str, dict[str, Any]] = {}
    for event in events:
        cell = event.get(h3_col)
        if not cell:
            continue
        g = grouped.setdefault(cell, {
            "h3_cell": cell,
            "event_count": 0,
            "target_event_count": 0,
            "strong_non_target_count": 0,
            "lat_sum": 0.0,
            "lon_sum": 0.0,
            "target_strengths": [],
            "all_strengths": [],
        })
        strength = float(event["strength_dbm"])
        g["event_count"] += 1
        g["lat_sum"] += float(event["lat"])
        g["lon_sum"] += float(event["lon"])
        g["all_strengths"].append(strength)
        if _event_is_target(event, target_min_hz, target_max_hz):
            g["target_event_count"] += 1
            g["target_strengths"].append(strength)
        elif strength >= -60.0:
            g["strong_non_target_count"] += 1

    rows: list[dict[str, Any]] = []
    for g in grouped.values():
        target_strengths = g["target_strengths"]
        p10 = percentile(target_strengths, 0.10)
        p50 = percentile(target_strengths, 0.50)
        avg_target = sum(target_strengths) / len(target_strengths) if target_strengths else None
        avg_all = sum(g["all_strengths"]) / len(g["all_strengths"]) if g["all_strengths"] else None

        lower_tail_quality = dbm_to_quality(p10)
        median_quality = dbm_to_quality(p50)
        availability = clamp(math.log1p(g["target_event_count"]) / math.log1p(250.0), 0.0, 1.0)
        data_confidence = clamp(g["target_event_count"] / 40.0, 0.0, 1.0)
        low_interference = 1.0 - clamp(g["strong_non_target_count"] / 20.0, 0.0, 1.0)

        score = (
            0.35 * lower_tail_quality
            + 0.20 * median_quality
            + 0.20 * availability
            + 0.15 * low_interference
            + 0.10 * data_confidence
        )

        count = max(g["event_count"], 1)
        rows.append({
            "h3_cell": g["h3_cell"],
            "lat": g["lat_sum"] / count,
            "lon": g["lon_sum"] / count,
            "event_count": g["event_count"],
            "target_event_count": g["target_event_count"],
            "strong_non_target_count": g["strong_non_target_count"],
            "target_strength_p10_dbm": round(p10, 1) if p10 is not None else None,
            "target_strength_median_dbm": round(p50, 1) if p50 is not None else None,
            "target_strength_avg_dbm": round(avg_target, 1) if avg_target is not None else None,
            "avg_dbm": round(avg_all, 1) if avg_all is not None else None,
            "lower_tail_quality": round(lower_tail_quality, 3),
            "target_availability": round(availability, 3),
            "low_interference": round(low_interference, 3),
            "data_confidence": round(data_confidence, 3),
            "suitability_score_0_100": round(score * 100.0, 1),
        })
    rows.sort(key=lambda r: r["suitability_score_0_100"], reverse=True)
    return rows
