# Experimental single-file build for EEI LANTERN.
# Run after the v0.10 patch is applied and tested locally.

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$Python = if (Test-Path ".venv\Scripts\python.exe") { ".venv\Scripts\python.exe" } elseif (Test-Path "venv\Scripts\python.exe") { "venv\Scripts\python.exe" } else { "python" }

& $Python -m pip install --upgrade pyinstaller pywebview uvicorn fastapi pydantic orjson

$AddData = "app\moth_pi_setup\moth_analysis;app\moth_pi_setup\moth_analysis"
& $Python -m PyInstaller --clean --onefile --name "EEI_LANTERN" --add-data $AddData lantern_desktop.py

Write-Host "Build complete. Test dist\EEI_LANTERN.exe on the same laptop before field use." -ForegroundColor Green
