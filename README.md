# LANTERN

LANTERN is the EEI/MOTH local analysis and reporting stack. It combines a FastAPI-backed MOTH/LAMP data workflow, static operator pages, desktop launch scripts, and early frontend work for field-readiness and GNSS/L-band analysis.

## Repository Layout

```text
app/moth_pi_setup/                 Raspberry Pi and local FastAPI analysis stack
app/moth_pi_setup/moth_analysis/   Core API, parser, scoring, and static operator UI
app/frontend/                      Vite/React frontend prototype
app/tests/                         Python regression tests for deterministic scoring helpers
docs/                              LANTERN navigation and UI notes
tools/                             Local audit and SQLite maintenance utilities
```

Runtime data, local databases, virtual environments, build output, release zips, logs, backups, and generated artifacts are intentionally ignored. Keep those on local/cloud storage rather than in Git.

## Local Windows Run

From the repository root:

```powershell
.\Start_LANTERN_Local.ps1
```

For the recommended travel layout, keep runtime data outside the Git repository:

```powershell
.\Start_LANTERN_Local.ps1 -DataRoot C:\LANTERN-data
```

The desktop wrapper can be started with:

```powershell
.\LANTERN_App.ps1
```

The wrapper accepts the same external data root:

```powershell
.\LANTERN_App.ps1 -DataRoot C:\LANTERN-data
```

## API Development

```powershell
cd app\moth_pi_setup
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m uvicorn moth_analysis.api:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000/`.

## Tests

From the repository root:

```powershell
$env:PYTHONPATH = "app"
python -m pytest app\tests -v
```

## Frontend Prototype

```powershell
cd app\frontend
npm ci
npm run build
```

## GitHub Working Rules

Use GitHub for source, docs, configuration, and repeatable setup. Do not commit real `.env` files, `moth.sqlite`, scans, uploads, build folders, release packages, virtual environments, or backup bundles.
