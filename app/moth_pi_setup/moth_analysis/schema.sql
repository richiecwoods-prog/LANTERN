PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS moth_collections (
    collection_id INTEGER PRIMARY KEY AUTOINCREMENT,
    collection_name TEXT NOT NULL,
    device_serial TEXT,
    firmware_version TEXT,
    hardware_version TEXT,
    source_type TEXT DEFAULT 'lamp_csv',
    scan_mode TEXT,
    detection_threshold_db REAL,
    white_list_enabled INTEGER DEFAULT 0,
    antenna_height_agl_m REAL,
    antenna_notes TEXT,
    operator_notes TEXT,
    file_name TEXT,
    file_hash TEXT UNIQUE,
    upload_time_utc TEXT NOT NULL,
    collection_start_utc TEXT,
    collection_end_utc TEXT,
    row_count INTEGER DEFAULT 0,
    valid_event_count INTEGER DEFAULT 0,
    invalid_event_count INTEGER DEFAULT 0,
    parser_version TEXT NOT NULL DEFAULT '0.1.0'
);

CREATE TABLE IF NOT EXISTS moth_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    collection_id INTEGER NOT NULL REFERENCES moth_collections(collection_id) ON DELETE CASCADE,
    timestamp_utc TEXT,
    lat REAL,
    lon REAL,
    altitude_msl_m REAL,
    satellites_seen INTEGER,
    frequency_hz REAL,
    signal_type TEXT,
    strength_dbm REAL,
    age_s REAL,
    scan_range_id TEXT,
    h3_r8 TEXT,
    h3_r9 TEXT,
    h3_r10 TEXT,
    valid INTEGER NOT NULL DEFAULT 1,
    validation_notes TEXT,
    raw_row_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_collection ON moth_events(collection_id);
CREATE INDEX IF NOT EXISTS idx_events_time ON moth_events(timestamp_utc);
CREATE INDEX IF NOT EXISTS idx_events_lat_lon ON moth_events(lat, lon);
CREATE INDEX IF NOT EXISTS idx_events_freq ON moth_events(frequency_hz);
CREATE INDEX IF NOT EXISTS idx_events_strength ON moth_events(strength_dbm);
CREATE INDEX IF NOT EXISTS idx_events_h3_r9 ON moth_events(h3_r9);

CREATE TABLE IF NOT EXISTS external_sensor_sessions (
    sensor_session_id INTEGER PRIMARY KEY AUTOINCREMENT,
    collection_id INTEGER NOT NULL REFERENCES moth_collections(collection_id) ON DELETE CASCADE,
    session_uuid TEXT NOT NULL UNIQUE,
    sensor_kind TEXT NOT NULL,
    transport TEXT,
    source_type TEXT NOT NULL,
    device_serial TEXT,
    firmware_version TEXT,
    hardware_version TEXT,
    sdk_version TEXT,
    adapter_version TEXT NOT NULL DEFAULT '0.1.0',
    source_port TEXT,
    baud_rate INTEGER,
    started_utc TEXT NOT NULL,
    last_message_utc TEXT,
    message_count INTEGER NOT NULL DEFAULT 0,
    rf_event_count INTEGER NOT NULL DEFAULT 0,
    gps_fix_type INTEGER,
    satellites_seen INTEGER,
    last_lat REAL,
    last_lon REAL,
    last_altitude_msl_m REAL,
    battery_remaining INTEGER,
    battery_flags INTEGER,
    status TEXT NOT NULL DEFAULT 'open',
    notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_sensor_sessions_uuid ON external_sensor_sessions(session_uuid);
CREATE INDEX IF NOT EXISTS idx_sensor_sessions_kind ON external_sensor_sessions(sensor_kind);
CREATE INDEX IF NOT EXISTS idx_sensor_sessions_collection ON external_sensor_sessions(collection_id);

CREATE TABLE IF NOT EXISTS raw_sensor_messages (
    raw_message_id INTEGER PRIMARY KEY AUTOINCREMENT,
    sensor_session_id INTEGER NOT NULL REFERENCES external_sensor_sessions(sensor_session_id) ON DELETE CASCADE,
    received_utc TEXT NOT NULL,
    message_type TEXT NOT NULL,
    message_id INTEGER,
    source_system INTEGER,
    source_component INTEGER,
    sequence_number INTEGER,
    normalized_event_count INTEGER NOT NULL DEFAULT 0,
    raw_payload_json TEXT NOT NULL,
    parse_status TEXT NOT NULL DEFAULT 'stored',
    parse_notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_raw_sensor_session ON raw_sensor_messages(sensor_session_id);
CREATE INDEX IF NOT EXISTS idx_raw_sensor_type ON raw_sensor_messages(message_type);
CREATE INDEX IF NOT EXISTS idx_raw_sensor_received ON raw_sensor_messages(received_utc);

CREATE TABLE IF NOT EXISTS sensor_parameter_snapshots (
    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    sensor_session_id INTEGER NOT NULL REFERENCES external_sensor_sessions(sensor_session_id) ON DELETE CASCADE,
    captured_utc TEXT NOT NULL,
    parameter_name TEXT NOT NULL,
    parameter_value TEXT,
    parameter_type TEXT,
    raw_payload_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sensor_params_session_name ON sensor_parameter_snapshots(sensor_session_id, parameter_name);
CREATE TABLE IF NOT EXISTS candidate_sites (
    site_id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    antenna_height_agl_m REAL,
    practical_score REAL DEFAULT 0.5,
    site_notes TEXT,
    created_utc TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_candidate_lat_lon ON candidate_sites(lat, lon);
