#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
python3 -m venv .venv
source .venv/bin/activate
export PYTHONPATH="$(pwd):${PYTHONPATH:-}"
python -m pip install --upgrade pip wheel
python -m pip install -r requirements.txt
python scripts/init_db.py
printf '\nMOTH analysis stack installed. Start it with:\n'
printf 'source .venv/bin/activate\n'
export PYTHONPATH="$(pwd):${PYTHONPATH:-}"
printf 'uvicorn moth_analysis.api:app --host 0.0.0.0 --port 8000\n'
printf '\nThen open http://<pi-ip>:8000\n'
