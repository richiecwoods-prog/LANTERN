import re
import sqlite3
from pathlib import Path

DB_PATH = Path(r"C:\MOTH\app\moth_pi_setup\data\moth.sqlite")

INDEX_PATTERNS = [
    ("time", r"time|timestamp|datetime|utc"),
    ("freq", r"freq|frequency|hz"),
    ("dbm", r"dbm|rssi|strength|power|level"),
    ("lat", r"lat|latitude"),
    ("lon", r"lon|lng|longitude"),
    ("h3", r"h3|hex|cell"),
    ("collection", r"collection|scan|file|source"),
    ("quality", r"quality|status"),
]

def safe_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'

def index_name(table: str, column: str) -> str:
    raw = f"idx_{table}_{column}"
    return re.sub(r"[^a-zA-Z0-9_]+", "_", raw)[:60]

DB_PATH.parent.mkdir(parents=True, exist_ok=True)

con = sqlite3.connect(DB_PATH)
cur = con.cursor()

cur.execute("PRAGMA journal_mode=WAL")
cur.execute("PRAGMA synchronous=NORMAL")
cur.execute("PRAGMA temp_store=MEMORY")
cur.execute("PRAGMA cache_size=-200000")

cur.execute("""
CREATE TABLE IF NOT EXISTS import_quality_summary (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    source_file TEXT,
    mode TEXT NOT NULL,
    raw_rows INTEGER NOT NULL DEFAULT 0,
    kept_rows INTEGER NOT NULL DEFAULT 0,
    rejected_rows INTEGER NOT NULL DEFAULT 0,
    flagged_rows INTEGER NOT NULL DEFAULT 0,
    reject_reasons_json TEXT NOT NULL DEFAULT '{}',
    flag_reasons_json TEXT NOT NULL DEFAULT '{}'
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS analysis_cache (
    cache_key TEXT PRIMARY KEY,
    cache_type TEXT NOT NULL,
    created_at TEXT NOT NULL,
    source_signature TEXT NOT NULL,
    payload_json TEXT NOT NULL
)
""")

tables = [
    row[0]
    for row in cur.execute(
        "select name from sqlite_master where type='table' order by name"
    )
]

created = []

for table in tables:
    cols = [row[1] for row in cur.execute(f"PRAGMA table_info({safe_ident(table)})")]

    for _, pattern in INDEX_PATTERNS:
        for col in cols:
            if re.search(pattern, col, re.IGNORECASE):
                name = index_name(table, col)
                cur.execute(
                    f"CREATE INDEX IF NOT EXISTS {safe_ident(name)} "
                    f"ON {safe_ident(table)} ({safe_ident(col)})"
                )
                created.append((table, col, name))
                break

cur.execute("CREATE INDEX IF NOT EXISTS idx_quality_summary_created ON import_quality_summary (created_at)")
cur.execute("CREATE INDEX IF NOT EXISTS idx_quality_summary_file ON import_quality_summary (source_file)")
cur.execute("CREATE INDEX IF NOT EXISTS idx_cache_type_created ON analysis_cache (cache_type, created_at)")

con.commit()
cur.execute("ANALYZE")
con.commit()
con.close()

print(f"Prepared DB: {DB_PATH}")
print("Indexes ensured:")
for table, col, name in created:
    print(f"  {table}.{col} -> {name}")
