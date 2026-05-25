import csv
import json
import os
import re
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(r"C:\MOTH")
APP = ROOT / "app"
STATIC = APP / "moth_pi_setup" / "moth_analysis" / "static"
DB_PATH = APP / "moth_pi_setup" / "data" / "moth.sqlite"
REPORTS = ROOT / "reports"
SCANS_DIRS = [ROOT / "scans", ROOT / "incoming"]

BASE_URL = "http://127.0.0.1:8000"

URLS = [
    "/static/dashboard.html",
    "/static/index.html?v=060",
    "/static/data_quality.html",
    "/static/launch_analysis.html?v=080",
    "/static/jsp101_report.html",
    "/static/mission_brief.html",
    "/static/briefing.html?v=080",
    "/static/moth_v080_decision_workflow.html",
    "/static/uas_rf.html",
    "/docs",
]

LAT_NAMES = {"lat", "latitude", "gps_lat", "gps latitude"}
LON_NAMES = {"lon", "lng", "longitude", "gps_lon", "gps longitude"}
FREQ_NAMES = {"frequency", "freq", "frequency_hz", "freq_hz", "hz"}
DBM_NAMES = {"dbm", "rssi", "power", "strength", "signal", "level"}
TIME_NAMES = {"timestamp", "time", "datetime", "utc", "date_time", "created_at"}


def norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")


def http_probe(path: str) -> dict:
    url = BASE_URL + path
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            body = resp.read()
            elapsed = time.perf_counter() - start
            return {
                "url": url,
                "status": resp.status,
                "ok": 200 <= resp.status < 400,
                "elapsed_ms": round(elapsed * 1000, 1),
                "bytes": len(body),
            }
    except Exception as exc:
        elapsed = time.perf_counter() - start
        return {
            "url": url,
            "status": "ERROR",
            "ok": False,
            "elapsed_ms": round(elapsed * 1000, 1),
            "bytes": 0,
            "error": repr(exc),
        }


def check_static_files() -> list[dict]:
    expected = [
        "dashboard.html",
        "index.html",
        "data_quality.html",
        "launch_analysis.html",
        "jsp101_report.html",
        "mission_brief.html",
        "briefing.html",
        "moth_v080_decision_workflow.html",
        "uas_rf.html",
        "lantern_nav.js",
        "lantern_nav.css",
    ]

    rows = []
    for name in expected:
        path = STATIC / name
        rows.append({
            "file": name,
            "exists": path.exists(),
            "size": path.stat().st_size if path.exists() else 0,
            "modified": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds") if path.exists() else "",
        })
    return rows


def check_links() -> list[dict]:
    results = []
    if not STATIC.exists():
        return results

    html_files = sorted(STATIC.glob("*.html"))
    pattern = re.compile(r"""(?:href|src)=["']([^"']+)["']""", re.IGNORECASE)

    for html in html_files:
        text = html.read_text(encoding="utf-8", errors="ignore")
        for link in pattern.findall(text):
            if link.startswith(("http://", "https://", "mailto:", "tel:", "#", "data:")):
                continue

            clean = link.split("?")[0].split("#")[0]
            if clean.startswith("/static/"):
                target = STATIC / clean.replace("/static/", "")
            elif clean.startswith("/"):
                continue
            else:
                target = html.parent / clean

            results.append({
                "page": html.name,
                "link": link,
                "target_exists": target.exists(),
                "target": str(target),
            })

    return results


def sqlite_summary() -> dict:
    if not DB_PATH.exists():
        return {"exists": False, "path": str(DB_PATH), "tables": []}

    out = {"exists": True, "path": str(DB_PATH), "tables": []}

    try:
        con = sqlite3.connect(DB_PATH)
        cur = con.cursor()
        tables = [r[0] for r in cur.execute("select name from sqlite_master where type='table' order by name")]
        for table in tables:
            safe_table = '"' + table.replace('"', '""') + '"'
            try:
                count = cur.execute(f"select count(*) from {safe_table}").fetchone()[0]
            except Exception:
                count = None

            cols = [r[1] for r in cur.execute(f"pragma table_info({safe_table})")]
            indexes = [r[1] for r in cur.execute(f"pragma index_list({safe_table})")]

            out["tables"].append({
                "table": table,
                "rows": count,
                "columns": cols,
                "indexes": indexes,
            })

        con.close()
    except Exception as exc:
        out["error"] = repr(exc)

    return out


def find_col(columns, accepted):
    lookup = {norm(c): c for c in columns}
    for key, original in lookup.items():
        if key in accepted:
            return original
    return None


def parse_float(value):
    try:
        if value is None or str(value).strip() == "":
            return None
        return float(str(value).strip())
    except Exception:
        return None


def csv_quality_summary(max_rows_per_file=250000) -> list[dict]:
    summaries = []

    for root in SCANS_DIRS:
        if not root.exists():
            continue

        for path in sorted(root.glob("*.csv")):
            started = time.perf_counter()
            total = 0
            invalid_latlon = 0
            zero_zero = 0
            missing_freq = 0
            invalid_freq = 0
            missing_dbm = 0
            suspicious_dbm = 0
            duplicate_keys = 0
            seen = set()

            try:
                with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as f:
                    reader = csv.DictReader(f)
                    cols = reader.fieldnames or []

                    lat_col = find_col(cols, LAT_NAMES)
                    lon_col = find_col(cols, LON_NAMES)
                    freq_col = find_col(cols, FREQ_NAMES)
                    dbm_col = find_col(cols, DBM_NAMES)
                    time_col = find_col(cols, TIME_NAMES)

                    for row in reader:
                        total += 1
                        if total > max_rows_per_file:
                            break

                        lat = parse_float(row.get(lat_col)) if lat_col else None
                        lon = parse_float(row.get(lon_col)) if lon_col else None
                        freq = parse_float(row.get(freq_col)) if freq_col else None
                        dbm = parse_float(row.get(dbm_col)) if dbm_col else None
                        ts = row.get(time_col, "") if time_col else ""

                        if lat is None or lon is None or not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
                            invalid_latlon += 1
                        elif abs(lat) < 1e-12 and abs(lon) < 1e-12:
                            zero_zero += 1

                        if freq is None:
                            missing_freq += 1
                        elif freq <= 0 or freq > 20_000_000_000:
                            invalid_freq += 1

                        if dbm is None:
                            missing_dbm += 1
                        elif dbm > 10 or dbm < -180:
                            suspicious_dbm += 1

                        key = (
                            str(ts)[:32],
                            round(lat, 7) if lat is not None else None,
                            round(lon, 7) if lon is not None else None,
                            round(freq, 1) if freq is not None else None,
                            round(dbm, 1) if dbm is not None else None,
                        )

                        if key in seen:
                            duplicate_keys += 1
                        else:
                            seen.add(key)

                elapsed = time.perf_counter() - started

                summaries.append({
                    "file": str(path),
                    "rows_sampled": total,
                    "elapsed_ms": round(elapsed * 1000, 1),
                    "columns": cols,
                    "lat_col": lat_col,
                    "lon_col": lon_col,
                    "freq_col": freq_col,
                    "dbm_col": dbm_col,
                    "time_col": time_col,
                    "invalid_latlon": invalid_latlon,
                    "zero_zero": zero_zero,
                    "missing_freq": missing_freq,
                    "invalid_freq": invalid_freq,
                    "missing_dbm": missing_dbm,
                    "suspicious_dbm": suspicious_dbm,
                    "duplicate_keys": duplicate_keys,
                })

            except Exception as exc:
                summaries.append({
                    "file": str(path),
                    "error": repr(exc),
                })

    return summaries


def markdown_report(payload: dict) -> str:
    lines = []
    lines.append("# LANTERN App Efficacy Audit")
    lines.append("")
    lines.append(f"Generated: `{payload['generated']}`")
    lines.append("")

    lines.append("## Page / endpoint timing")
    lines.append("")
    lines.append("| URL | OK | Status | ms | bytes |")
    lines.append("|---|---:|---:|---:|---:|")
    for r in payload["http"]:
        lines.append(f"| `{r['url']}` | {r['ok']} | {r['status']} | {r['elapsed_ms']} | {r['bytes']} |")

    slow = [r for r in payload["http"] if r["ok"] and r["elapsed_ms"] > 1500]
    failed = [r for r in payload["http"] if not r["ok"]]

    lines.append("")
    lines.append("## Static file presence")
    lines.append("")
    lines.append("| File | Exists | Size | Modified |")
    lines.append("|---|---:|---:|---|")
    for r in payload["static_files"]:
        lines.append(f"| `{r['file']}` | {r['exists']} | {r['size']} | {r['modified']} |")

    broken = [r for r in payload["links"] if not r["target_exists"]]
    lines.append("")
    lines.append("## Link check")
    lines.append("")
    lines.append(f"Broken local static links: **{len(broken)}**")
    if broken:
        lines.append("")
        lines.append("| Page | Link | Target |")
        lines.append("|---|---|---|")
        for r in broken[:50]:
            lines.append(f"| `{r['page']}` | `{r['link']}` | `{r['target']}` |")

    lines.append("")
    lines.append("## SQLite summary")
    lines.append("")
    db = payload["sqlite"]
    lines.append(f"DB exists: **{db.get('exists')}**")
    lines.append(f"DB path: `{db.get('path')}`")
    if db.get("error"):
        lines.append(f"DB error: `{db['error']}`")

    if db.get("tables"):
        lines.append("")
        lines.append("| Table | Rows | Index count | Key columns observed |")
        lines.append("|---|---:|---:|---|")
        for t in db["tables"]:
            key_cols = [c for c in t["columns"] if re.search(r"freq|hz|lat|lon|time|stamp|dbm|h3|collection|scan|file", c, re.I)]
            lines.append(f"| `{t['table']}` | {t['rows']} | {len(t['indexes'])} | `{', '.join(key_cols[:12])}` |")

    lines.append("")
    lines.append("## CSV quality summary")
    lines.append("")
    if payload["csv_quality"]:
        lines.append("| File | Rows sampled | Invalid GPS | Zero/zero | Missing freq | Bad freq | Missing dBm | Suspicious dBm | Duplicates |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
        for r in payload["csv_quality"]:
            if "error" in r:
                lines.append(f"| `{r['file']}` | ERROR |  |  |  |  |  |  | `{r['error']}` |")
            else:
                lines.append(
                    f"| `{Path(r['file']).name}` | {r['rows_sampled']} | {r['invalid_latlon']} | {r['zero_zero']} | "
                    f"{r['missing_freq']} | {r['invalid_freq']} | {r['missing_dbm']} | {r['suspicious_dbm']} | {r['duplicate_keys']} |"
                )
    else:
        lines.append("No CSV files found in `C:\\MOTH\\scans` or `C:\\MOTH\\incoming`.")

    lines.append("")
    lines.append("## Initial findings")
    lines.append("")
    if failed:
        lines.append(f"- **Fail:** {len(failed)} core URLs did not load.")
    else:
        lines.append("- **Pass:** all core URLs loaded.")

    if slow:
        lines.append(f"- **Performance warning:** {len(slow)} URL(s) exceeded 1500 ms.")
    else:
        lines.append("- **Performance:** core page loads are within the initial threshold.")

    if broken:
        lines.append("- **Navigation warning:** broken local static links exist.")
    else:
        lines.append("- **Navigation:** no broken local static links found.")

    suspect_files = []
    for r in payload["csv_quality"]:
        if "error" in r:
            continue
        suspect = (
            r["invalid_latlon"] +
            r["zero_zero"] +
            r["missing_freq"] +
            r["invalid_freq"] +
            r["missing_dbm"] +
            r["suspicious_dbm"] +
            r["duplicate_keys"]
        )
        if suspect:
            suspect_files.append((r["file"], suspect))

    if suspect_files:
        lines.append(f"- **Filtering needed:** {len(suspect_files)} CSV file(s) contain suspect rows.")
    else:
        lines.append("- **Data quality:** no obvious suspect rows detected in sampled CSVs.")

    lines.append("")
    lines.append("## Recommended next fixes")
    lines.append("")
    lines.append("1. Add an import-time quality gate: invalid GPS, zero/zero, missing frequency, bad frequency, missing dBm, duplicate rows.")
    lines.append("2. Add a visible filter summary: raw rows, kept rows, rejected rows, and reject reasons.")
    lines.append("3. Add SQLite indexes for timestamp, frequency, location, h3 cell, collection/scan IDs.")
    lines.append("4. Cache expensive map/hex/spectrum aggregations by selected scan IDs and filter hash.")
    lines.append("5. Default to aggregate hex layers; load raw markers only on demand.")
    lines.append("6. Keep spectrum/spike analysis behind row cap, wider bins, and explicit user action.")

    return "\n".join(lines)


def main():
    REPORTS.mkdir(parents=True, exist_ok=True)

    payload = {
        "generated": datetime.now().isoformat(timespec="seconds"),
        "http": [http_probe(u) for u in URLS],
        "static_files": check_static_files(),
        "links": check_links(),
        "sqlite": sqlite_summary(),
        "csv_quality": csv_quality_summary(),
    }

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = REPORTS / f"lantern_app_audit_{stamp}.json"
    md_path = REPORTS / f"lantern_app_audit_{stamp}.md"

    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    md_path.write_text(markdown_report(payload), encoding="utf-8")

    print(f"Wrote: {md_path}")
    print(f"Wrote: {json_path}")


if __name__ == "__main__":
    main()
