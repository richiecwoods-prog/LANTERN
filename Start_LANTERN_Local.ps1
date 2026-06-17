param(
  [int]$Port = 8000,
  [string]$HostAddress = "127.0.0.1",
  [string]$OpenPath = "/app?v=0122",
  [switch]$Foreground,
  [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$env:LANTERN_IMPORT_QUALITY_MODE = if ($env:LANTERN_IMPORT_QUALITY_MODE) { $env:LANTERN_IMPORT_QUALITY_MODE } else { "standard" }

function Test-LanternRuntime {
  param([string]$PythonPath)
  if (-not (Test-Path $PythonPath)) { return $false }
  try {
    & $PythonPath -c "import sys, fastapi, uvicorn; print(sys.executable)" *> $null
    return ($LASTEXITCODE -eq 0)
  } catch {
    return $false
  }
}

$py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-LanternRuntime -PythonPath $py)) {
  Write-Host "Runtime missing or not portable. Creating/repairing runtime first..."
  & powershell -ExecutionPolicy Bypass -File (Join-Path $Root "Install_LANTERN_Runtime.ps1") -Root $Root -NoOptionalPackaging
}
if (-not (Test-Path $py)) { throw "Runtime python not found at $py" }

if (-not (Test-LanternRuntime -PythonPath $py)) {
  Write-Host "Runtime modules missing after repair. Re-running dependency install..."
  & powershell -ExecutionPolicy Bypass -File (Join-Path $Root "Install_LANTERN_Runtime.ps1") -Root $Root -NoOptionalPackaging
}
if (-not (Test-LanternRuntime -PythonPath $py)) { throw "LANTERN runtime is still not usable at $py" }

# Stop stale local server on the requested port, if Windows exposes the TCP table.
if (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue) {
  Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue |
    Select-Object -ExpandProperty OwningProcess -Unique |
    Where-Object { $_ -and $_ -ne $PID } |
    ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }
}

$AppDir = Join-Path $Root "app\moth_pi_setup"
if (-not (Test-Path $AppDir)) { throw "Cannot find app directory: $AppDir" }

$logs = Join-Path $Root "logs"
New-Item -ItemType Directory -Path $logs -Force | Out-Null
$outLog = Join-Path $logs "lantern_stdout.log"
$errLog = Join-Path $logs "lantern_stderr.log"
$url = "http://$HostAddress`:$Port$OpenPath"

Write-Host "Starting LANTERN on $url"
Write-Host "Eagle Eye Innovations launch runtime"
Write-Host "Import quality mode: $env:LANTERN_IMPORT_QUALITY_MODE"

if ($Foreground) {
  Set-Location $AppDir
  & $py -m uvicorn moth_analysis.api:app --host $HostAddress --port $Port --log-level info
  exit $LASTEXITCODE
}

$args = @("-m", "uvicorn", "moth_analysis.api:app", "--host", $HostAddress, "--port", [string]$Port, "--log-level", "info")
$p = Start-Process -FilePath $py -ArgumentList $args -WorkingDirectory $AppDir -RedirectStandardOutput $outLog -RedirectStandardError $errLog -WindowStyle Hidden -PassThru
Start-Sleep -Milliseconds 900

if ($p.HasExited) {
  Write-Host "LANTERN failed to start. stderr:" -ForegroundColor Red
  if (Test-Path $errLog) { Get-Content $errLog -Tail 80 }
  throw "LANTERN server exited immediately."
}

Write-Host "Server PID: $($p.Id). Logs: $outLog / $errLog"

$healthUrl = "http://$HostAddress`:$Port/api/platform/health"
$ready = $false
for ($i = 0; $i -lt 30; $i++) {
  try {
    Invoke-WebRequest -UseBasicParsing -Uri $healthUrl -TimeoutSec 1 *> $null
    $ready = $true
    break
  } catch {
    Start-Sleep -Milliseconds 500
  }
}

if ($ready) {
  Write-Host "LANTERN is ready: $url" -ForegroundColor Green
} else {
  Write-Host "LANTERN started but did not answer health checks yet. Try the URL manually: $url" -ForegroundColor Yellow
}

if (-not $NoBrowser) {
  try {
    Start-Process $url
  } catch {
    Write-Host "Could not open browser automatically. Open this URL manually:" -ForegroundColor Yellow
    Write-Host $url -ForegroundColor Cyan
  }
}
