param(
  [string]$Url = "http://127.0.0.1:8000/?v=060"
)

$ErrorActionPreference = "Stop"

$healthUrl = "http://127.0.0.1:8000/docs"
$backendScript = "C:\MOTH\Start_MOTH_Local.ps1"

function Test-LanternUrl {
  param([string]$TestUrl)

  try {
    Invoke-WebRequest -Uri $TestUrl -UseBasicParsing -TimeoutSec 2 | Out-Null
    return $true
  } catch {
    return $false
  }
}

function Show-LanternError {
  param([string]$Message)

  try {
    Add-Type -AssemblyName PresentationFramework
    [System.Windows.MessageBox]::Show($Message, "LANTERN Local", "OK", "Error") | Out-Null
  } catch {
    Write-Host $Message
  }
}

if (-not (Test-Path $backendScript)) {
  Show-LanternError "Cannot find backend launcher: $backendScript"
  exit 1
}

# Start backend if it is not already running.
if (-not (Test-LanternUrl $healthUrl)) {
  Start-Process powershell.exe -ArgumentList @(
    "-NoExit",
    "-ExecutionPolicy", "Bypass",
    "-File", $backendScript
  ) -WindowStyle Minimized
}

# Wait for backend readiness.
$deadline = (Get-Date).AddSeconds(45)

while ((Get-Date) -lt $deadline) {
  if (Test-LanternUrl $healthUrl) {
    break
  }

  Start-Sleep -Milliseconds 750
}

if (-not (Test-LanternUrl $healthUrl)) {
  Show-LanternError "LANTERN backend did not become ready on http://127.0.0.1:8000. Open C:\MOTH\Start_MOTH_Local.ps1 manually and check the terminal error."
  exit 1
}

# Open URL in app-style browser window.
$edgeCandidates = @(
  "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
  "${env:ProgramFiles}\Microsoft\Edge\Application\msedge.exe",
  "${env:LOCALAPPDATA}\Microsoft\Edge\Application\msedge.exe"
)

$edge = $edgeCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1

if ($edge) {
  Start-Process $edge -ArgumentList @("--app=$Url", "--new-window")
} else {
  Start-Process $Url
}
