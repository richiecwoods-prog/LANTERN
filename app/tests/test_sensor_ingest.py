import sqlite3

from moth_pi_setup.moth_analysis.sensor_ingest import (
    ingest_sensor_message,
    sensor_capabilities,
    sensor_status,
    start_sensor_session,
)


def test_sensor_capabilities_preserve_csv_and_reserve_rf_explorer():
    caps = sensor_capabilities()

    assert caps["csv_ingest_preserved"] is True
    assert caps["supported_now"][0]["sensor_kind"] == "moth_sdk"
    assert caps["reserved_future"][0]["sensor_kind"] == "rf_explorer"


def test_moth_sdk_messages_archive_raw_and_feed_existing_events(tmp_path):
    db_path = tmp_path / "lantern.sqlite"
    session = start_sensor_session(
        session_uuid="sdk-test-session",
        collection_name="SDK test session",
        device_serial="MOTH-001",
        db_path=db_path,
    )

    gps_result = ingest_sensor_message(
        session_uuid="sdk-test-session",
        db_path=db_path,
        payload={
            "message_type": "GPS_RAW_INT",
            "message_id": 24,
            "fields": {
                "fix_type": 3,
                "lat": 533200000,
                "lon": -15400000,
                "alt": 42000,
                "satellites_visible": 11,
            },
        },
    )
    rf_result = ingest_sensor_message(
        session_uuid="sdk-test-session",
        db_path=db_path,
        payload={
            "message_type": "RF_SIGNAL",
            "message_id": 15613,
            "fields": {
                "frequency": 1575.42,
                "bandwidth": 2.0,
                "power_level": -67,
                "time_nsec": 1770000000000000000,
            },
        },
    )

    assert gps_result["normalized_event_count"] == 0
    assert rf_result["normalized_event_count"] == 1

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    raw_count = conn.execute("SELECT COUNT(*) FROM raw_sensor_messages").fetchone()[0]
    event = conn.execute("SELECT * FROM moth_events").fetchone()
    collection = conn.execute("SELECT * FROM moth_collections WHERE collection_id = ?", (session["collection_id"],)).fetchone()
    status = sensor_status(db_path=db_path)
    conn.close()

    assert raw_count == 2
    assert collection["source_type"] == "moth_sdk_mavlink"
    assert event["collection_id"] == session["collection_id"]
    assert event["frequency_hz"] == 1575420000.0
    assert event["strength_dbm"] == -67
    assert event["lat"] == 53.32
    assert event["lon"] == -1.54
    assert status["normalized_event_count"] == 1


def test_moth_table_log_creates_one_event_per_valid_point(tmp_path):
    db_path = tmp_path / "lantern.sqlite"
    start_sensor_session(session_uuid="table-session", db_path=db_path)
    ingest_sensor_message(
        session_uuid="table-session",
        db_path=db_path,
        payload={
            "message_type": "GPS_RAW_INT",
            "fields": {"fix_type": 3, "lat": 533200000, "lon": -15400000, "satellites_visible": 9},
        },
    )
    result = ingest_sensor_message(
        session_uuid="table-session",
        db_path=db_path,
        payload={
            "message_type": "MOTH_TABLE_LOG",
            "fields": {
                "num_points": 2,
                "frequencies": [1176.45, 1227.60],
                "display_strength": [-72, -80],
                "age": [1, 3],
            },
        },
    )

    conn = sqlite3.connect(db_path)
    event_count = conn.execute("SELECT COUNT(*) FROM moth_events").fetchone()[0]
    conn.close()

    assert result["normalized_event_count"] == 2
    assert event_count == 2
