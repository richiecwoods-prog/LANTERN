from __future__ import annotations

from datetime import datetime
from pathlib import Path

api_path = Path(r"C:\MOTH\app\moth_pi_setup\moth_analysis\api.py")
text = api_path.read_text(encoding="utf-8-sig")

backup = Path(r"C:\MOTH\backups") / f"api_before_restore_quality_gate_{datetime.now().strftime('%Y%m%d_%H%M%S')}.py"
backup.write_text(text, encoding="utf-8")

# Ensure os is imported for LANTERN_IMPORT_QUALITY_MODE.
if "\nimport os\n" not in text:
    text = text.replace("import math\n", "import math\nimport os\n", 1)

# Remove duplicate config imports while keeping the first one.
lines = text.splitlines()
clean_lines = []
seen_config = False

for line in lines:
    if line.strip() == "from .config import DB_PATH, UPLOAD_DIR":
        if seen_config:
            continue
        seen_config = True
    clean_lines.append(line)

text = "\n".join(clean_lines) + "\n"

replacement = '''def do_import(
    file: UploadFile,
    *,
    collection_name: str | None = None,
    device_serial: str | None = None,
    firmware_version: str | None = None,
    hardware_version: str | None = None,
    source_type: str = "lamp_csv",
    scan_mode: str | None = None,
    detection_threshold_db: float | None = None,
    white_list_enabled: bool = False,
    antenna_height_agl_m: float | None = None,
    antenna_notes: str | None = None,
    operator_notes: str | None = None,
) -> dict[str, Any]:
    dest = save_upload_file(file)

    # LANTERN import quality gate.
    #
    # Default behaviour is shadow mode:
    #   - save the original upload
    #   - calculate and store quality/filter summary
    #   - import the original CSV unchanged
    #
    # Set LANTERN_IMPORT_QUALITY_MODE=clean to import a cleaned copy after
    # the quality summary has been proven against real field data.
    quality_mode = os.getenv("LANTERN_IMPORT_QUALITY_MODE", "shadow").strip().lower()
    import_path = dest
    quality_summary: dict[str, Any] | None = None

    try:
        raw_df, cleaned_df, quality_summary = load_and_clean_csv(
            dest,
            source_file=Path(file.filename or dest.name).name,
            mode=quality_mode or "shadow",
        )
        save_quality_summary(DB_PATH, quality_summary)

        if quality_mode in {"clean", "active", "standard"}:
            original_columns = [column for column in raw_df.columns if column in cleaned_df.columns]

            if not original_columns:
                raise ValueError("Quality gate could not identify original CSV columns for cleaned import.")

            cleaned_for_import = cleaned_df[original_columns].copy()
            clean_dest = dest.with_name(f"{dest.stem}__quality_cleaned{dest.suffix}")
            counter = 1

            while clean_dest.exists():
                clean_dest = dest.with_name(f"{dest.stem}__quality_cleaned_{counter}{dest.suffix}")
                counter += 1

            cleaned_for_import.to_csv(clean_dest, index=False)
            import_path = clean_dest

    except Exception as exc:
        # Never break CSV import just because the quality summary failed.
        # The backend terminal will show the warning while normal import continues.
        print(f"[LANTERN quality gate] warning: {exc}")
        quality_summary = {
            "ok": False,
            "source_file": Path(file.filename or dest.name).name,
            "mode": quality_mode or "shadow",
            "error": str(exc),
        }

    try:
        result = insert_collection_from_csv(
            import_path,
            collection_name=collection_name,
            device_serial=device_serial,
            firmware_version=firmware_version,
            hardware_version=hardware_version,
            source_type=source_type,
            scan_mode=scan_mode,
            detection_threshold_db=detection_threshold_db,
            white_list_enabled=white_list_enabled,
            antenna_height_agl_m=antenna_height_agl_m,
            antenna_notes=antenna_notes,
            operator_notes=operator_notes,
        )

        if isinstance(result, dict):
            result["quality_mode"] = quality_mode or "shadow"
            result["quality_summary"] = quality_summary

            if import_path != dest:
                result["original_upload_path"] = str(dest)
                result["quality_cleaned_import_path"] = str(import_path)

        return result

    except Exception as exc:
        if "UNIQUE constraint failed" in str(exc):
            raise HTTPException(status_code=409, detail="This file hash already exists in the database") from exc
        raise HTTPException(status_code=500, detail=str(exc)) from exc
'''

start = text.index("def do_import(")
end = text.index('\n\n@app.post("/api/import")', start)

text = text[:start] + replacement + text[end:]
api_path.write_text(text, encoding="utf-8")

print(f"Restored quality-gate do_import in: {api_path}")
print(f"Backup written to: {backup}")
