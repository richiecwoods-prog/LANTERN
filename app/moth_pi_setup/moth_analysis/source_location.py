from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import math
from typing import Any, Iterable

from .geo import clamp, haversine_m


@dataclass(frozen=True)
class SourceLocationConfig:
    time_bucket_s: int = 5
    frequency_bin_hz: float = 1_000_000.0
    min_collections: int = 2
    min_receiver_spread_m: float = 10.0
    path_loss_exponent: float = 2.0


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None


def _public_receiver(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_id": event.get("event_id"),
        "collection_id": event["collection_id"],
        "collection_name": event.get("collection_name") or f"Collection {event['collection_id']}",
        "device_serial": event.get("device_serial"),
        "file_name": event.get("file_name"),
        "timestamp_utc": event["timestamp_utc"],
        "lat": round(float(event["lat"]), 7),
        "lon": round(float(event["lon"]), 7),
        "frequency_hz": round(float(event["frequency_hz"]), 3),
        "frequency_mhz": round(float(event["frequency_hz"]) / 1_000_000.0, 6),
        "strength_dbm": round(float(event["strength_dbm"]), 1),
    }


def normalise_source_events(rows: Iterable[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    events: list[dict[str, Any]] = []
    skipped = 0
    for row in rows:
        dt = _parse_dt(row.get("timestamp_utc"))
        collection_id = _to_int(row.get("collection_id"))
        lat = _to_float(row.get("lat"))
        lon = _to_float(row.get("lon"))
        frequency_hz = _to_float(row.get("frequency_hz"))
        strength_dbm = _to_float(row.get("strength_dbm"))
        if dt is None or collection_id is None or lat is None or lon is None or frequency_hz is None or strength_dbm is None:
            skipped += 1
            continue
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            skipped += 1
            continue
        events.append({
            "event_id": row.get("event_id"),
            "collection_id": collection_id,
            "collection_name": row.get("collection_name"),
            "device_serial": row.get("device_serial"),
            "file_name": row.get("file_name"),
            "timestamp_utc": _iso(dt),
            "timestamp_epoch": dt.timestamp(),
            "lat": lat,
            "lon": lon,
            "frequency_hz": frequency_hz,
            "strength_dbm": strength_dbm,
        })
    return events, skipped


def _max_receiver_spread_m(receivers: list[dict[str, Any]]) -> float:
    max_distance = 0.0
    for i, a in enumerate(receivers):
        for b in receivers[i + 1:]:
            max_distance = max(max_distance, haversine_m(float(a["lat"]), float(a["lon"]), float(b["lat"]), float(b["lon"])))
    return max_distance


def find_concurrent_groups(
    events: Iterable[dict[str, Any]],
    config: SourceLocationConfig,
    *,
    max_groups: int = 500,
) -> list[dict[str, Any]]:
    time_bucket_s = max(1, int(config.time_bucket_s))
    frequency_bin_hz = max(1.0, float(config.frequency_bin_hz))
    buckets: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for event in events:
        time_bucket = int(math.floor(float(event["timestamp_epoch"]) / time_bucket_s))
        frequency_bucket = int(math.floor(float(event["frequency_hz"]) / frequency_bin_hz))
        buckets.setdefault((time_bucket, frequency_bucket), []).append(event)

    groups: list[dict[str, Any]] = []
    for (time_bucket, frequency_bucket), bucket_events in buckets.items():
        by_collection: dict[int, dict[str, Any]] = {}
        for event in bucket_events:
            collection_id = int(event["collection_id"])
            current = by_collection.get(collection_id)
            if current is None or float(event["strength_dbm"]) > float(current["strength_dbm"]):
                by_collection[collection_id] = event
        if len(by_collection) < int(config.min_collections):
            continue

        receivers = sorted(by_collection.values(), key=lambda r: float(r["strength_dbm"]), reverse=True)
        public_receivers = [_public_receiver(r) for r in receivers]
        strengths = [float(r["strength_dbm"]) for r in receivers]
        spread_m = _max_receiver_spread_m(public_receivers)
        bucket_start = datetime.fromtimestamp(time_bucket * time_bucket_s, tz=timezone.utc)
        bucket_end = bucket_start + timedelta(seconds=time_bucket_s)
        center_hz = (frequency_bucket * frequency_bin_hz) + (frequency_bin_hz / 2.0)
        group = {
            "group_id": f"{time_bucket}:{frequency_bucket}",
            "bucket_start_utc": _iso(bucket_start),
            "bucket_end_utc": _iso(bucket_end),
            "time_bucket_s": time_bucket_s,
            "frequency_bin_hz": frequency_bin_hz,
            "frequency_center_hz": round(center_hz, 3),
            "frequency_center_mhz": round(center_hz / 1_000_000.0, 6),
            "raw_event_count": len(bucket_events),
            "collection_count": len(receivers),
            "receiver_spread_m": round(spread_m, 1),
            "eligible": spread_m >= float(config.min_receiver_spread_m),
            "min_dbm": round(min(strengths), 1),
            "max_dbm": round(max(strengths), 1),
            "avg_dbm": round(sum(strengths) / len(strengths), 1),
            "receivers": public_receivers,
        }
        groups.append(group)

    groups.sort(
        key=lambda g: (
            bool(g.get("eligible")),
            int(g.get("collection_count") or 0),
            float(g.get("max_dbm") or -999.0),
            float(g.get("receiver_spread_m") or 0.0),
        ),
        reverse=True,
    )
    return groups[:max(1, int(max_groups))]


def _receiver_summary(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_collection: dict[int, dict[str, Any]] = {}
    for group in groups:
        for receiver in group.get("receivers") or []:
            collection_id = int(receiver["collection_id"])
            row = by_collection.setdefault(collection_id, {
                "collection_id": collection_id,
                "collection_name": receiver.get("collection_name"),
                "device_serial": receiver.get("device_serial"),
                "file_name": receiver.get("file_name"),
                "overlap_event_count": 0,
                "max_dbm": None,
                "lat": receiver.get("lat"),
                "lon": receiver.get("lon"),
            })
            row["overlap_event_count"] += 1
            strength = float(receiver.get("strength_dbm") or -999.0)
            if row["max_dbm"] is None or strength > float(row["max_dbm"]):
                row["max_dbm"] = round(strength, 1)
                row["lat"] = receiver.get("lat")
                row["lon"] = receiver.get("lon")
    return sorted(by_collection.values(), key=lambda r: (int(r["overlap_event_count"]), float(r.get("max_dbm") or -999.0)), reverse=True)


def _bounds_for_groups(groups: list[dict[str, Any]]) -> dict[str, float]:
    points = [
        (float(receiver["lat"]), float(receiver["lon"]))
        for group in groups
        for receiver in group.get("receivers") or []
        if receiver.get("lat") is not None and receiver.get("lon") is not None
    ]
    lats = [p[0] for p in points]
    lons = [p[1] for p in points]
    min_lat, max_lat = min(lats), max(lats)
    min_lon, max_lon = min(lons), max(lons)
    center_lat = (min_lat + max_lat) / 2.0
    center_lon = (min_lon + max_lon) / 2.0
    spread_m = max(
        haversine_m(center_lat, center_lon, lat, lon)
        for lat, lon in points
    ) if points else 0.0
    pad_m = max(250.0, min(75_000.0, (spread_m * 1.35) + 500.0))
    lat_pad = pad_m / 111_320.0
    lon_scale = max(0.20, abs(math.cos(math.radians(center_lat))))
    lon_pad = pad_m / (111_320.0 * lon_scale)
    return {
        "min_lat": round(max(-90.0, min_lat - lat_pad), 7),
        "max_lat": round(min(90.0, max_lat + lat_pad), 7),
        "min_lon": round(max(-180.0, min_lon - lon_pad), 7),
        "max_lon": round(min(180.0, max_lon + lon_pad), 7),
    }


def _score_candidate(lat: float, lon: float, group: dict[str, Any], config: SourceLocationConfig) -> float:
    receivers = group.get("receivers") or []
    if len(receivers) < int(config.min_collections):
        return 0.0
    losses: list[float] = []
    strengths: list[float] = []
    for receiver in receivers:
        distance_m = max(10.0, haversine_m(lat, lon, float(receiver["lat"]), float(receiver["lon"])))
        losses.append(10.0 * float(config.path_loss_exponent) * math.log10(distance_m))
        strengths.append(float(receiver["strength_dbm"]))
    fitted_tx_power = sum(strength + loss for strength, loss in zip(strengths, losses)) / len(strengths)
    residuals = [
        strength - (fitted_tx_power - loss)
        for strength, loss in zip(strengths, losses)
    ]
    rmse = math.sqrt(sum(r * r for r in residuals) / len(residuals))
    fit_score = math.exp(-rmse / 8.0)
    strength_span = max(strengths) - min(strengths)
    span_weight = clamp(strength_span / 12.0, 0.45, 1.0)
    spread_weight = clamp(float(group.get("receiver_spread_m") or 0.0) / 250.0, 0.35, 1.0)
    receiver_weight = clamp(len(receivers) / 4.0, 0.70, 1.25)
    return clamp(100.0 * fit_score * span_weight * spread_weight * receiver_weight, 0.0, 100.0)


def _plural_label(label: str) -> str:
    return label if label.endswith("s") else f"{label}s"


def build_heatmap(
    groups: list[dict[str, Any]],
    config: SourceLocationConfig,
    *,
    grid_size: int = 36,
    max_groups: int = 24,
) -> dict[str, Any] | None:
    eligible = [g for g in groups if g.get("eligible")][:max(1, int(max_groups))]
    if not eligible:
        return None
    grid_size = max(12, min(80, int(grid_size)))
    bounds = _bounds_for_groups(eligible)
    min_lat = float(bounds["min_lat"])
    max_lat = float(bounds["max_lat"])
    min_lon = float(bounds["min_lon"])
    max_lon = float(bounds["max_lon"])
    cells: list[dict[str, Any]] = []
    for row in range(grid_size):
        lat = max_lat - (row / max(1, grid_size - 1)) * (max_lat - min_lat)
        for col in range(grid_size):
            lon = min_lon + (col / max(1, grid_size - 1)) * (max_lon - min_lon)
            total = 0.0
            weight_sum = 0.0
            support_groups = 0
            for group in eligible:
                score = _score_candidate(lat, lon, group, config)
                weight = math.sqrt(float(group.get("collection_count") or 1)) * clamp((float(group.get("max_dbm") or -110.0) + 110.0) / 60.0, 0.45, 1.20)
                total += score * weight
                weight_sum += weight
                if score >= 35.0:
                    support_groups += 1
            final_score = total / weight_sum if weight_sum else 0.0
            cells.append({
                "row": row,
                "col": col,
                "lat": round(lat, 7),
                "lon": round(lon, 7),
                "score_0_100": round(final_score, 1),
                "support_groups": support_groups,
            })

    ranked = sorted(cells, key=lambda c: float(c["score_0_100"]), reverse=True)
    diagonal_m = haversine_m(min_lat, min_lon, max_lat, max_lon)
    min_spacing_m = max(25.0, diagonal_m / (grid_size * 2.0))
    candidates: list[dict[str, Any]] = []
    for cell in ranked:
        if all(haversine_m(float(cell["lat"]), float(cell["lon"]), float(c["lat"]), float(c["lon"])) >= min_spacing_m for c in candidates):
            candidates.append({
                "rank": len(candidates) + 1,
                "lat": cell["lat"],
                "lon": cell["lon"],
                "score_0_100": cell["score_0_100"],
                "support_groups": cell["support_groups"],
            })
        if len(candidates) >= 12:
            break

    return {
        "bounds": bounds,
        "rows": grid_size,
        "cols": grid_size,
        "cell_count": len(cells),
        "cells": cells,
        "candidates": candidates,
        "model": {
            "type": "rssi_log_distance_fit",
            "path_loss_exponent": config.path_loss_exponent,
            "unknown_tx_power": "fitted_per_overlap_group",
        },
    }


def _source_location_report(
    *,
    state: str,
    state_label: str,
    confidence_label: str,
    event_label: str,
    best: dict[str, Any] | None,
    events: list[dict[str, Any]],
    skipped: int,
    groups: list[dict[str, Any]],
    eligible_groups: list[dict[str, Any]],
    receivers: list[dict[str, Any]],
    limitations: list[str],
) -> dict[str, Any]:
    best_score = float(best.get("score_0_100")) if best else None
    event_plural = _plural_label(event_label)
    evidence_points = [
        f"{len(events)} usable {event_plural} were available; {skipped} row(s) were skipped because key timing, RF or location fields were missing or invalid.",
        f"{len(groups)} concurrent multi-collection group(s) of {event_plural} were found; {len(eligible_groups)} met the receiver-geometry rule.",
        f"{len(receivers)} receiver collection(s) contributed to the eligible source-location evidence.",
    ]
    if eligible_groups:
        starts = sorted(str(g.get("bucket_start_utc") or "") for g in eligible_groups if g.get("bucket_start_utc"))
        ends = sorted(str(g.get("bucket_end_utc") or "") for g in eligible_groups if g.get("bucket_end_utc"))
        if starts and ends:
            evidence_points.append(f"Eligible overlap window spans {starts[0]} to {ends[-1]} UTC.")

    location = None
    if best:
        location = {
            "lat": best.get("lat"),
            "lon": best.get("lon"),
            "score_0_100": round(best_score, 1) if best_score is not None else None,
            "support_groups": best.get("support_groups"),
            "rank": best.get("rank"),
        }
        evidence_points.append(
            f"Top ranked area is {best.get('lat')}, {best.get('lon')} with score {round(best_score, 1) if best_score is not None else 'n/a'}/100."
        )

    if state == "ready" and best:
        if best_score is not None and best_score >= 70.0:
            status = "good"
            headline = "Likely source area suspected"
            conclusion = f"The overlapping {event_label} evidence produces a strong RSSI-fit source area."
            action = "Use the ranked coordinate as a high-priority search or validation area, then confirm with another collection pass or independent source evidence."
        elif best_score is not None and best_score >= 45.0:
            status = "check"
            headline = "Likely source area requires review"
            conclusion = f"The overlapping {event_label} evidence produces a reviewable RSSI-fit source area, but confidence is not strong."
            action = "Use the ranked coordinate as a tasking lead only. Re-run with tighter filters and collect another pass before briefing it as a firm location."
        else:
            status = "check"
            headline = "Weak likely-source indication"
            conclusion = f"The {event_label} data is concurrent and geometrically usable, but the RSSI fit is weak."
            action = "Treat the location as a low-confidence lead. Improve receiver spread, repeat the collection, or narrow the frequency/time filters."
    elif state == "no_events":
        status = "bad"
        headline = "No source-location report available"
        conclusion = f"No usable {event_plural} were available for source-location reporting."
        if event_label == "suspicious RF ping":
            action = "Import or select CSV collections with timestamp, frequency, dBm and receiver position fields, or review the suspicious-ping threshold."
        else:
            action = "Import or select CSV collections with timestamp, frequency, dBm and receiver position fields before producing a report."
    elif state == "no_concurrent_captures":
        status = "bad"
        headline = "No concurrent source-location report"
        conclusion = f"The selected data does not show the same {event_label} activity across multiple collections in the configured time and frequency bins."
        action = "Select overlapping collections or adjust the time bucket and frequency bin, then refresh the heat map."
    elif state == "insufficient_receiver_geometry":
        status = "check"
        headline = "Concurrent detections need receiver separation"
        conclusion = f"Concurrent {event_plural} were found, but receiver positions were too close together for a useful source-location estimate."
        action = "Use collections from separated receiver positions or reduce the minimum receiver-spread setting only if that matches the collection geometry."
    else:
        status = "check"
        headline = "Source-location report pending"
        conclusion = state_label
        action = "Review filters and collection coverage, then refresh the heat map."

    caveats = limitations + [
        "Report the result as a likely area or tasking lead, not as a confirmed emitter origin.",
        "Keep the heat map, filters and contributing collections with the report for auditability.",
    ]
    text_lines = [
        "Likely Source Location Report",
        f"Status: {headline}",
        f"Confidence: {confidence_label}",
        f"Conclusion: {conclusion}",
        f"Recommended action: {action}",
    ]
    if location:
        text_lines.extend([
            f"Top area: {location['lat']}, {location['lon']}",
            f"Score: {location['score_0_100']}/100",
            f"Supporting groups: {location['support_groups']}",
        ])
    text_lines.append("Evidence:")
    text_lines.extend(f"- {point}" for point in evidence_points)
    text_lines.append("Caveats:")
    text_lines.extend(f"- {caveat}" for caveat in caveats)

    return {
        "available": bool(best),
        "status": status,
        "headline": headline,
        "confidence_label": confidence_label,
        "conclusion": conclusion,
        "recommended_action": action,
        "location": location,
        "evidence_points": evidence_points,
        "caveats": caveats,
        "text": "\n".join(text_lines),
    }


def build_source_location_result(
    rows: Iterable[dict[str, Any]],
    *,
    config: SourceLocationConfig | None = None,
    include_heatmap: bool = True,
    grid_size: int = 36,
    max_groups: int = 24,
    event_label: str = "suspicious RF ping",
) -> dict[str, Any]:
    cfg = config or SourceLocationConfig()
    event_plural = _plural_label(event_label)
    events, skipped = normalise_source_events(rows)
    groups = find_concurrent_groups(events, cfg, max_groups=500)
    eligible_groups = [g for g in groups if g.get("eligible")]
    receivers = _receiver_summary(eligible_groups)
    heatmap = build_heatmap(groups, cfg, grid_size=grid_size, max_groups=max_groups) if include_heatmap else None
    candidates = (heatmap or {}).get("candidates") or []
    best = candidates[0] if candidates else None
    analysis_ready = bool(eligible_groups)

    if not events:
        state = "no_events"
        state_label = f"No usable {event_plural}"
    elif not groups:
        state = "no_concurrent_captures"
        state_label = f"No concurrent multi-collection {event_plural}"
    elif not eligible_groups:
        state = "insufficient_receiver_geometry"
        state_label = f"Concurrent {event_plural} found, but receiver positions are not separated enough"
    else:
        state = "ready"
        state_label = f"Likely source heat map available for {event_plural}"

    best_score = float(best.get("score_0_100")) if best else None
    if best_score is None:
        confidence_label = "NO FIX"
    elif best_score >= 70.0:
        confidence_label = "STRONG"
    elif best_score >= 45.0:
        confidence_label = "REVIEW"
    else:
        confidence_label = "WEAK"

    limitations = [
        "This is RSSI confidence mapping, not bearing triangulation.",
        f"Inputs are {event_plural} selected by the active threshold and frequency/time/AOI filters.",
        "Unknown transmitter power, antenna pattern, terrain, reflections and receiver calibration can move the likely area.",
        "Concurrent means same configured time bucket and frequency bin across separate collections.",
    ]
    report = _source_location_report(
        state=state,
        state_label=state_label,
        confidence_label=confidence_label,
        event_label=event_label,
        best=best,
        events=events,
        skipped=skipped,
        groups=groups,
        eligible_groups=eligible_groups,
        receivers=receivers,
        limitations=limitations,
    )

    return {
        "status": "ok",
        "analysis_ready": analysis_ready,
        "location_suspected": analysis_ready,
        "input_event_type": "suspicious_rf_pings",
        "state": state,
        "state_label": state_label,
        "config": asdict(cfg),
        "summary": {
            "usable_event_count": len(events),
            "skipped_event_count": skipped,
            "concurrent_group_count": len(groups),
            "eligible_group_count": len(eligible_groups),
            "receiver_count": len(_receiver_summary(eligible_groups)),
            "best_score_0_100": round(best_score, 1) if best_score is not None else None,
            "best_lat": best.get("lat") if best else None,
            "best_lon": best.get("lon") if best else None,
            "confidence_label": confidence_label,
        },
        "receivers": receivers,
        "overlap_groups": groups[:max(1, int(max_groups))],
        "heatmap": heatmap,
        "candidates": candidates,
        "report": report,
        "limitations": limitations,
    }
