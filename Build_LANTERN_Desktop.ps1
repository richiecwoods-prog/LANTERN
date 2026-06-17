param(
  [string]$Root = "E:\EEI APPS\MOTH",
  [string]$Name = "EEI_LANTERN"
)

$ErrorActionPreference = "Stop"
Set-Location $Root

& powershell -ExecutionPolicy Bypass -File (Join-Path $Root "Install_LANTERN_Runtime.ps1") -Root $Root
$py = Join-Path $Root ".venv\Scripts\python.exe"

& $py -m pip install --upgrade pyinstaller pywebview

if (Test-Path "dist\$Name") { Remove-Item "dist\$Name" -Recurse -Force }
if (Test-Path "build\$Name") { Remove-Item "build\$Name" -Recurse -Force }

$addApp = "app;app"
$addReq = "requirements-lantern-runtime.txt;."

& $py -m PyInstaller `
  --noconfirm `
  --clean `
  --onedir `
  --name $Name `
  --add-data $addApp `
  --add-data $addReq `
  --collect-all h3 `
  --collect-all uvicorn `
  --collect-all fastapi `
  lantern_desktop.py

Write-Host "Build complete: $Root\dist\$Name\$Name.exe"
Write-Host "Run that EXE from its folder. Data remains external; keep mission data in LANTERN_Data or the project DB location."
