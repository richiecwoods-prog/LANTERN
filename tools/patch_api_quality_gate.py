from pathlib import Path
import re

api_path = Path(r"C:\MOTH\app\moth_pi_setup\moth_analysis\api.py")
backup_path = api_path.with_suffix(".py.bak_quality_gate")

text = api_path.read_text(encoding="utf-8")
backup_path.write_text(text, encoding="utf-8")

# Ensure os is imported.
if "import os\n" not in text:
    text = text.replace("import math\n", "import math\nimport os\n")

# Remove duplicate config import lines while keeping the first one.
lines = text.splitlines()
seen_config = False
deduped = []

for line in lines:
    if line.strip() == "from .config import DB_PATH, UPLOAD_DIR":
        if seen_config:
            continue
        seen_config = True
    deduped.append(line)

text = "\n".join(deduped) + "\n"

new_func = '''def do_import(
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

    # LANTERN quality gate.
    #
    # shadow = record quality summary but import original CSV.
    # clean  = record quality summary and import a cleaned copy.
    #
    # Start with shadow mode. Switch to clean mode after confirming
    # /api/quality/summary reports sensible row counts.
    quality_mode = os.getenv("LANTERN_IMPORT_QUALITY_MODE", "shadow").strip().lower()
    import_path = dest
    quality_summary: dict[str, Any] | None = None

    try:
        raw_df, cleaned_df, quality_summary = load_and_clean_csv(
            dest,
            source_file=Path(file.filename or dest.name).name,
            mode="standard",
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
        # Do not break import because of quality-summary failure.
        # The backend terminal will show this warning.
        print(f"[LANTERN quality gate] warning: {exc}")
        quality_summary = {
            "ok": False,
            "source_file": Path(file.filename or dest.name).name,
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
            result["quality_mode"] = quality_mode
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

pattern = re.compile(
    r'def do_import\(\n.*?\n\) -> dict\[str, Any\]:\n.*?\n\n@app\.post\("/api/import"\)',
    re.DOTALL,
)

match = pattern.search(text)

if not match:
    raise SystemExit("Could not find do_import block. No changes written.")

text = text[:match.start()] + new_func + '\n\n@app.post("/api/import")' + text[match.end():]

api_path.write_text(text, encoding="utf-8")

print(f"Patched: {api_path}")
print(f"Backup:  {backup_path}")
