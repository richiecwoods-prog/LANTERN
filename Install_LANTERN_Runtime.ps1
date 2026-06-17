param(
  [string]$Root = "C:\MOTH",
  [switch]$NoOptionalPackaging
)

$ErrorActionPreference = "Stop"
Set-Location $Root

$RuntimeTemp = Join-Path $Root ".runtime_tmp"
$PipCache = Join-Path $RuntimeTemp "pip-cache"
New-Item -ItemType Directory -Path $RuntimeTemp -Force | Out-Null
New-Item -ItemType Directory -Path $PipCache -Force | Out-Null
$env:TEMP = $RuntimeTemp
$env:TMP = $RuntimeTemp
$env:PIP_CACHE_DIR = $PipCache

function Test-LanternPython {
  param([string]$PythonPath)
  if (-not (Test-Path $PythonPath)) { return $false }
  try {
    & $PythonPath -c "import sys; print(sys.executable)" *> $null
    if ($LASTEXITCODE -ne 0) { return $false }
    & $PythonPath -m pip --version *> $null
    return ($LASTEXITCODE -eq 0)
  } catch {
    return $false
  }
}

function Move-BrokenVenv {
  param([string]$VenvPath)
  if (-not (Test-Path $VenvPath)) { return }
  $stamp = Get-Date -Format "yyyyMMdd_HHmmss"
  $broken = Join-Path $Root ".venv_broken_$stamp"
  $n = 1
  while (Test-Path $broken) {
    $broken = Join-Path $Root ".venv_broken_$stamp`_$n"
    $n++
  }
  Write-Host "Existing .venv is not usable on this machine. Moving it to $broken"
  Move-Item -LiteralPath $VenvPath -Destination $broken
}

$venvDir = Join-Path $Root ".venv"
$venvPython = Join-Path $venvDir "Scripts\python.exe"

if ((Test-Path $venvDir) -and -not (Test-LanternPython -PythonPath $venvPython)) {
  Move-BrokenVenv -VenvPath $venvDir
}

if (-not (Test-Path $venvPython)) {
  $made = $false
  if (Get-Command py -ErrorAction SilentlyContinue) {
    foreach ($v in @("3.12", "3.11", "3.10")) {
      py -$v --version *> $null
      if ($LASTEXITCODE -eq 0) {
        Write-Host "Creating .venv using Python $v"
        py -$v -m venv .venv
        if (Test-LanternPython -PythonPath $venvPython) {
          $made = $true
          break
        }
        Write-Host "Python $v created an unusable venv. Trying next candidate..."
        Move-BrokenVenv -VenvPath $venvDir
      }
    }
  }
  if (-not $made) {
    $pythonCmd = Get-Command python -ErrorAction SilentlyContinue
    if (-not $pythonCmd) {
      throw "Python was not found on this laptop. Install Python 3.11 or 3.12, then rerun LANTERN_App.bat."
    }
    Write-Host "Creating .venv using default python"
    & $pythonCmd.Source -m venv .venv
    if (-not (Test-LanternPython -PythonPath $venvPython)) {
      throw "Default python created a venv without a working pip. Check Python installation and free disk space."
    }
  }
}

$py = $venvPython
if (-not (Test-Path $py)) { throw "Could not create or find $py" }

& $py -m pip install --upgrade pip setuptools wheel

$req = Join-Path $Root "requirements-lantern-runtime.txt"
if (Test-Path $req) {
  if ($NoOptionalPackaging) {
    & $py -m pip install fastapi "uvicorn[standard]" python-multipart orjson pydantic pandas h3
  } else {
    & $py -m pip install -r $req
  }
} else {
  & $py -m pip install fastapi "uvicorn[standard]" python-multipart orjson pydantic pandas h3 pywebview pyinstaller
}

& $py -c "import fastapi, uvicorn, pandas, h3, orjson; print('LANTERN runtime dependencies OK')"
