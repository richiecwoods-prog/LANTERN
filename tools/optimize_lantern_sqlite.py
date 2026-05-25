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
    ("collection", r"collection|scan|file"),
]

def safe_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'

def index_name(table: str, column: str) -> str:
    raw = f"idx_{table}_{column}"
    return re.sub(r"[^a-zA-Z0-9_]+", "_", raw)[:60]

if not DB_PATH.exists():
    raise SystemExit(f"DB not found: {DB_PATH}")

con = sqlite3.connect(DB_PATH)
cur = con.cursor()

cur.execute("PRAGMA journal_mode=WAL")
cur.execute("PRAGMA synchronous=NORMAL")
cur.execute("PRAGMA temp_store=MEMORY")
cur.execute("PRAGMA cache_size=-200000")

tables = [r[0] for r in cur.execute("select name from sqlite_master where type='table' order by name")]

created = []

for table in tables:
    cols = [r[1] for r in cur.execute(f"PRAGMA table_info({safe_ident(table)})")]

    for label, pattern in INDEX_PATTERNS:
        for col in cols:
            if re.search(pattern, col, re.IGNORECASE):
                name = index_name(table, col)
                sql = f"CREATE INDEX IF NOT EXISTS {safe_ident(name)} ON {safe_ident(table)} ({safe_ident(col)})"
                cur.execute(sql)
                created.append((table, col, name))
                break

con.commit()
cur.execute("ANALYZE")
con.commit()
con.close()

print(f"Optimized: {DB_PATH}")
print("Indexes ensured:")
for table, col, name in created:
    print(f"  {table}.{col} -> {name}")
