param(
  [int]$Port = 8000,
  [switch]$Desktop,
  [switch]$NoBrowserFallback
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

if (-not $Desktop) {
  & powershell -ExecutionPolicy Bypass -File (Join-Path $Root "Start_LANTERN_Local.ps1") -Port $Port
  exit $LASTEXITCODE
}

$py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
  & powershell -ExecutionPolicy Bypass -File (Join-Path $Root "Install_LANTERN_Runtime.ps1") -Root $Root
}

$args = @((Join-Path $Root "lantern_desktop.py"), "--port", [string]$Port)
if ($NoBrowserFallback) { $args += "--no-browser-fallback" }
& $py @args
