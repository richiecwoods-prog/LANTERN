#!/usr/bin/env bash
cd "$HOME/thl-moth-ai"
source .venv/bin/activate
uvicorn app.main:app --host 0.0.0.0 --port 8788
