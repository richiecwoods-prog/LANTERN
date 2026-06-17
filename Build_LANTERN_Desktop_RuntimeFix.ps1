param(
  [string]$Root = (Split-Path -Parent $MyInvocation.MyCommand.Path)
)

$ErrorActionPreference = "Stop"

Set-Location -LiteralPath $Root

$py = Join-Path $Root ".venv\Scripts\python.exe"
$desktop = Join-Path $Root "lantern_desktop.py"

if (-not (Test-Path -LiteralPath $py)) {
  throw "Venv Python not found: $py"
}

if (-not (Test-Path -LiteralPath $desktop)) {
  throw "lantern_desktop.py not found: $desktop"
}

$sqlitePkg = (& $py -c "import sqlite3, pathlib; print(pathlib.Path(sqlite3.__file__).parent)").Trim()
$sqliteExt = (& $py -c "import _sqlite3; print(_sqlite3.__file__)").Trim()
$sqliteDll = (& $py -c "import sys, pathlib; p=pathlib.Path(sys.base_prefix)/'DLLs'/'sqlite3.dll'; print(p if p.exists() else '')").Trim()

Write-Host "Using root: $Root"
Write-Host "Using Python: $py"
Write-Host "SQLite package: $sqlitePkg"
Write-Host "SQLite extension: $sqliteExt"
Write-Host "SQLite DLL: $sqliteDll"

& $py -m pip install --upgrade pyinstaller pywebview

Remove-Item -Recurse -Force (Join-Path $Root "build\EEI_LANTERN") -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force (Join-Path $Root "dist\EEI_LANTERN") -ErrorAction SilentlyContinue

$env:PYTHONPATH = Join-Path $Root "app\moth_pi_setup"

$mothPkgData = "$Root\app\moth_pi_setup\moth_analysis;app\moth_pi_setup\moth_analysis"
$sqlitePkgData = "$sqlitePkg;sqlite3"
$sqliteExtBin = "$sqliteExt;."

$args = @(
  "--noconfirm",
  "--clean",
  "--onedir",
  "--name", "EEI_LANTERN",

  "--paths", (Join-Path $Root "app\moth_pi_setup"),

  "--add-data", $mothPkgData,
  "--add-data", $sqlitePkgData,
  "--add-binary", $sqliteExtBin,

  "--hidden-import", "moth_analysis.api",
  "--hidden-import", "moth_analysis.db",
  "--hidden-import", "moth_analysis.config",
  "--hidden-import", "moth_analysis.quality",
  "--hidden-import", "moth_analysis.ingest",
  "--hidden-import", "moth_analysis.scoring",
  "--hidden-import", "moth_analysis.geo",
  "--hidden-import", "moth_analysis.h3tools",

  "--hidden-import", "sqlite3",
  "--hidden-import", "_sqlite3",

  "--hidden-import", "uvicorn.logging",
  "--hidden-import", "uvicorn.loops.auto",
  "--hidden-import", "uvicorn.protocols.http.auto",
  "--hidden-import", "uvicorn.protocols.websockets.auto",
  "--hidden-import", "uvicorn.lifespan.on",

  "--hidden-import", "fastapi",
  "--hidden-import", "starlette",
  "--hidden-import", "pydantic",
  "--hidden-import", "orjson",
  "--hidden-import", "pandas",
  "--hidden-import", "h3",

  "--exclude-module", "fastapi.testclient",
  "--exclude-module", "starlette.testclient",
  "--exclude-module", "pytest",
  "--exclude-module", "IPython",
  "--exclude-module", "notebook",
  "--exclude-module", "matplotlib.tests",
  "--exclude-module", "pandas.tests",
  "--exclude-module", "numpy.tests"
)

if ($sqliteDll -and (Test-Path -LiteralPath $sqliteDll)) {
  $args += @("--add-binary", "$sqliteDll;.")
}

$args += $desktop

Write-Host "Building EEI_LANTERN desktop folder..."
& $py -m PyInstaller @args

if ($LASTEXITCODE -ne 0) {
  throw "PyInstaller failed with exit code $LASTEXITCODE"
}

$exe = Join-Path $Root "dist\EEI_LANTERN\EEI_LANTERN.exe"

if (-not (Test-Path -LiteralPath $exe)) {
  throw "Build finished but EXE was not found: $exe"
}

Write-Host ""
Write-Host "Built:" $exe -ForegroundColor Green
Write-Host ""
Write-Host "Test with:"
Write-Host "Start-Process -FilePath `"$exe`""
