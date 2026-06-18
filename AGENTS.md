# AGENTS.md

## Project Context

This repository is LANTERN, the EEI/MOTH local analysis and reporting app. It contains a Windows desktop launch surface, a Raspberry Pi/FastAPI analysis stack, static operator pages, and a Vite/React frontend prototype.

Keep the repository portable for GitHub, Codex, and travel use. Git should contain source, docs, scripts, tests, and reproducible setup only. Runtime data, databases, scans, uploads, build output, release zips, virtual environments, logs, and backups stay local or in external storage.

## Expected Checks

For Python/core changes from the repository root:

```powershell
$env:PYTHONPATH = "app"
python -m pytest app\tests -v
```

For frontend changes:

```powershell
cd app\frontend
npm ci
npm run build
```

For API syntax-only confidence when dependencies are unavailable:

```powershell
python -m py_compile app\moth_pi_setup\moth_analysis\api.py
```

## Editing Guidance

- Preserve Windows PowerShell launchers and Raspberry Pi deployment docs unless the user explicitly changes direction.
- Treat `app/moth_pi_setup/moth_analysis/api.py`, `quality.py`, `scoring.py`, and `app/moth_pi_setup/moth_analysis/static/` as the active LANTERN/MOTH operator surface.
- Keep standalone static pages usable where they already exist; avoid forcing a single linear workflow.
- Do not commit real `.env` files, SQLite databases, scan CSVs, uploads, logs, build products, release packages, or backup folders.
- Keep generated desktop packages under `build/`, `dist/`, or `releases/`, which are ignored.
- Prefer `Start_LANTERN_Local.ps1 -DataRoot C:\LANTERN-data` or `LANTERN_DATA_ROOT` when running a cloned repo on a travel machine.
- Ask before adding large binary assets or new production dependencies.
