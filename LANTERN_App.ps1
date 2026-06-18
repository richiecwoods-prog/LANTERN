param(
  [int]$Port = 8000,
  [string]$DataRoot = "",
  [switch]$Desktop,
  [switch]$NoBrowserFallback
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

if (-not $Desktop) {
  $startArgs = @("-ExecutionPolicy", "Bypass", "-File", (Join-Path $Root "Start_LANTERN_Local.ps1"), "-Port", [string]$Port)
  if ($DataRoot) { $startArgs += @("-DataRoot", $DataRoot) }
  & powershell @startArgs
  exit $LASTEXITCODE
}

if (-not $DataRoot) {
  $appDir = Join-Path $Root "app\moth_pi_setup"
  $DataRoot = if ($env:LANTERN_DATA_ROOT) { $env:LANTERN_DATA_ROOT } else { Join-Path $appDir "data" }
}

$DataRoot = [System.IO.Path]::GetFullPath($DataRoot)
$UploadRoot = Join-Path $DataRoot "uploads"
New-Item -ItemType Directory -Path $DataRoot -Force | Out-Null
New-Item -ItemType Directory -Path $UploadRoot -Force | Out-Null

$env:MOTH_PROJECT_ROOT = Join-Path $Root "app\moth_pi_setup"
$env:MOTH_DATA_DIR = $DataRoot
$env:MOTH_DB_PATH = Join-Path $DataRoot "moth.sqlite"
$env:MOTH_UPLOAD_DIR = $UploadRoot

$py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
  & powershell -ExecutionPolicy Bypass -File (Join-Path $Root "Install_LANTERN_Runtime.ps1") -Root $Root
}

$args = @((Join-Path $Root "lantern_desktop.py"), "--port", [string]$Port)
if ($NoBrowserFallback) { $args += "--no-browser-fallback" }
& $py @args
