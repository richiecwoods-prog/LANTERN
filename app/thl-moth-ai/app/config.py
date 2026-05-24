from pathlib import Path

APP_NAME = "THL MOTH AI"
BASE_DIR = Path(__file__).resolve().parent.parent
UPLOAD_DIR = BASE_DIR / "data" / "uploads"
PROCESSED_DIR = BASE_DIR / "data" / "processed"

# Aden Adde International Airport approximate operating area.
DEFAULT_CENTER = {"lat": 2.0144, "lon": 45.3047, "zoom": 14}

# Expected MOTH fields. The loader is tolerant, but these are preferred.
FIELD_ALIASES = {
    "lat": ["lat", "latitude", "Latitude", "LAT"],
    "lon": ["lon", "lng", "long", "longitude", "Longitude", "LON"],
    "rssi": ["rssi", "RSSI", "signal", "signal_dbm", "power_dbm", "dbm"],
    "freq": ["freq", "frequency", "frequency_hz", "freq_hz", "Frequency"],
    "time": ["time", "timestamp", "datetime", "utc", "Time"],
}
