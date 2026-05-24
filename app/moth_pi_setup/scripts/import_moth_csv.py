from __future__ import annotations

import argparse
from pathlib import Path

from moth_analysis.ingest import insert_collection_from_csv


def main() -> None:
    p = argparse.ArgumentParser(description="Import a MOTH/LAMP CSV into the local database")
    p.add_argument("csv", type=Path)
    p.add_argument("--collection-name")
    p.add_argument("--device-serial")
    p.add_argument("--scan-mode")
    p.add_argument("--detection-threshold-db", type=float)
    p.add_argument("--white-list-enabled", action="store_true")
    p.add_argument("--antenna-height-agl-m", type=float)
    p.add_argument("--notes")
    args = p.parse_args()
    result = insert_collection_from_csv(
        args.csv,
        collection_name=args.collection_name,
        device_serial=args.device_serial,
        scan_mode=args.scan_mode,
        detection_threshold_db=args.detection_threshold_db,
        white_list_enabled=args.white_list_enabled,
        antenna_height_agl_m=args.antenna_height_agl_m,
        operator_notes=args.notes,
    )
    print(result)


if __name__ == "__main__":
    main()
