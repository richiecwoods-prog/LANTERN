param(
    [int]$RotateSeconds = 12,
    [int]$NewsPollSeconds = 120,
    [int]$LiveRefreshMinutes = 5
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$j2Path = Join-Path $PSScriptRoot "app\moth_pi_setup\moth_analysis\static\j2_report.html"
if (-not (Test-Path $j2Path)) {
    throw "J2 report page not found: $j2Path. Reapply the v0.11.3 J2 live articles patch."
}

# These values are kept for compatibility with the original installer command.
# The page defaults are 12 s article rotation and 5 min full refresh. The API endpoint refreshes live news on demand and caches for about 10 minutes.
Write-Host "J2 Article Rotator installed."
Write-Host "RotateSeconds: $RotateSeconds"
Write-Host "NewsPollSeconds: $NewsPollSeconds"
Write-Host "LiveRefreshMinutes: $LiveRefreshMinutes"
Write-Host "Open: http://127.0.0.1:8000/static/j2_report.html?v=113"
