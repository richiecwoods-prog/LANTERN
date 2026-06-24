from moth_pi_setup.moth_analysis.source_location import (
    SourceLocationConfig,
    build_source_location_result,
)


def event(collection_id, lat, lon, strength_dbm, *, timestamp="2026-06-19T10:00:01Z", frequency_hz=1_575_420_000):
    return {
        "event_id": collection_id,
        "collection_id": collection_id,
        "collection_name": f"Receiver {collection_id}",
        "timestamp_utc": timestamp,
        "lat": lat,
        "lon": lon,
        "frequency_hz": frequency_hz,
        "strength_dbm": strength_dbm,
    }


def test_source_location_reports_no_concurrent_captures():
    rows = [
        event(1, 53.2300, -0.5400, -64, timestamp="2026-06-19T10:00:01Z"),
        event(2, 53.2320, -0.5420, -72, timestamp="2026-06-19T10:01:01Z"),
    ]

    result = build_source_location_result(rows, config=SourceLocationConfig(time_bucket_s=5), include_heatmap=True, grid_size=16)

    assert result["analysis_ready"] is False
    assert result["state"] == "no_concurrent_captures"
    assert result["summary"]["concurrent_group_count"] == 0
    assert result["heatmap"] is None
    assert result["report"]["available"] is False
    assert result["report"]["status"] == "bad"
    assert "No concurrent" in result["report"]["headline"]


def test_source_location_builds_heatmap_for_separated_concurrent_receivers():
    rows = [
        event(1, 53.2300, -0.5400, -54),
        event(2, 53.2330, -0.5480, -67),
        event(3, 53.2260, -0.5350, -71),
    ]

    result = build_source_location_result(rows, config=SourceLocationConfig(time_bucket_s=5, min_receiver_spread_m=10), include_heatmap=True, grid_size=16)

    assert result["analysis_ready"] is True
    assert result["state"] == "ready"
    assert result["summary"]["concurrent_group_count"] == 1
    assert result["summary"]["eligible_group_count"] == 1
    assert result["heatmap"]["cell_count"] == 16 * 16
    assert result["candidates"]
    assert result["report"]["available"] is True
    assert result["report"]["location"]["lat"] == result["candidates"][0]["lat"]
    assert result["report"]["text"].startswith("Likely Source Location Report")
    assert "suspicious RF ping" in result["report"]["text"]


def test_source_location_blocks_same_position_receiver_geometry():
    rows = [
        event(1, 53.2300, -0.5400, -54),
        event(2, 53.2300, -0.5400, -67),
    ]

    result = build_source_location_result(rows, config=SourceLocationConfig(time_bucket_s=5, min_receiver_spread_m=10), include_heatmap=True, grid_size=16)

    assert result["analysis_ready"] is False
    assert result["state"] == "insufficient_receiver_geometry"
    assert result["summary"]["concurrent_group_count"] == 1
    assert result["summary"]["eligible_group_count"] == 0
    assert result["heatmap"] is None
    assert result["report"]["available"] is False
    assert "receiver separation" in result["report"]["headline"].lower()


def test_source_location_can_label_all_rf_debug_inputs():
    result = build_source_location_result([], event_label="RF detection")

    assert result["state_label"] == "No usable RF detections"
    assert "No usable RF detections" in result["report"]["conclusion"]
    assert "suspicious" not in result["report"]["conclusion"].lower()
