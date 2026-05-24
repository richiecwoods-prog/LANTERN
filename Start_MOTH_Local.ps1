$ErrorActionPreference = "Stop"

Set-Location "C:\MOTH\app\moth_pi_setup"

if (Test-Path "C:\MOTH\app\.venv\Scripts\Activate.ps1") {
  . "C:\MOTH\app\.venv\Scripts\Activate.ps1"
}

New-Item -ItemType File -Force "C:\MOTH\app\moth_pi_setup\moth_analysis\__init__.py" | Out-Null

$env:LANTERN_ROOT = "C:\MOTH"
$env:LANTERN_SCANS_DIR = "C:\MOTH\scans"
$env:LANTERN_INCOMING_DIR = "C:\MOTH\incoming"
$env:LANTERN_REPORTS_DIR = "C:\MOTH\reports"

# Compatibility aliases for existing backend code.
$env:MOTH_ROOT = "C:\MOTH"
$env:MOTH_SCANS_DIR = "C:\MOTH\scans"
$env:MOTH_INCOMING_DIR = "C:\MOTH\incoming"
$env:MOTH_REPORTS_DIR = "C:\MOTH\reports"

python -m uvicorn moth_analysis.api:app --host 127.0.0.1 --port 8000 --reload
