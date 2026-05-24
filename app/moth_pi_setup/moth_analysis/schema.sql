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
