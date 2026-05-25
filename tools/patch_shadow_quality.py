from datetime import datetime
from pathlib import Path

api_path = Path(r"C:\MOTH\app\moth_pi_setup\moth_analysis\api.py")
text = api_path.read_text(encoding="utf-8")

backup = api_path.with_name(
    f"api.py.bak_shadow_quality_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
)
backup.write_text(text, encoding="utf-8")

start = text.index("def do_import(")
end = text.index('\n\n@app.post("/api/import")', start)

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

    # LANTERN import quality gate - shadow mode.
    # This records quality/filter statistics but still imports the original CSV.
    quality_summary: dict[str, Any] | None = None

    try:
        _raw_df, _cleaned_df, quality_summary = load_and_clean_csv(
            dest,
            source_file=Path(file.filename or dest.name).name,
            mode="shadow",
        )
        save_quality_summary(DB_PATH, quality_summary)
    except Exception as exc:
        print(f"[LANTERN quality gate] warning: {exc}")
        quality_summary = {
            "ok": False,
            "source_file": Path(file.filename or dest.name).name,
            "error": str(exc),
        }

    try:
        result = insert_collection_from_csv(
            dest,
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
            result["quality_mode"] = "shadow"
            result["quality_summary"] = quality_summary

        return result

    except Exception as exc:
        if "UNIQUE constraint failed" in str(exc):
            raise HTTPException(status_code=409, detail="This file hash already exists in the database") from exc
        raise HTTPException(status_code=500, detail=str(exc)) from exc
'''

api_path.write_text(text[:start] + replacement + text[end:], encoding="utf-8")

print(f"Patched: {api_path}")
print(f"Backup:  {backup}")
