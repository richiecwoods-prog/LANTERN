from __future__ import annotations

import argparse
import csv
from pathlib import Path

from moth_analysis.ingest import insert_candidate_site


def main() -> None:
    p = argparse.ArgumentParser(description="Import candidate antenna sites from CSV")
    p.add_argument("csv", type=Path)
    args = p.parse_args()
    imported = 0
    with args.csv.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            insert_candidate_site(
                name=row["name"],
                lat=float(row["lat"]),
                lon=float(row["lon"]),
                antenna_height_agl_m=float(row["antenna_height_agl_m"]) if row.get("antenna_height_agl_m") else None,
                practical_score=float(row["practical_score"]) if row.get("practical_score") else 0.5,
                site_notes=row.get("site_notes"),
            )
            imported += 1
    print({"imported": imported})


if __name__ == "__main__":
    main()
