# MOTH Pi Data Analysis Starter Stack

This starter stack is for a Raspberry Pi 5 with AI HAT+ 2 already installed and verified. It handles the first data-analysis target:

```text
MOTH / LAMP CSV upload
→ tolerant parser
→ SQLite event store
→ H3 cell aggregation
→ local FastAPI API
→ browser map UI
→ candidate antenna-site scoring
```

The AI HAT+ 2 is not required for this MVP. Add Hailo inference after the parser, database, and scoring workflow are proven with real MOTH CSV files.

## 1. Copy to the Pi

```bash
scp -r moth_pi_setup pi@<pi-ip>:/home/pi/
ssh pi@<pi-ip>
cd /home/pi/moth_pi_setup
```

## 2. Install OS packages

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip sqlite3
```

## 3. Install the Python stack

```bash
./install_on_pi.sh
```

## 4. Start the local API/UI

```bash
source .venv/bin/activate
uvicorn moth_analysis.api:app --host 0.0.0.0 --port 8000
```

Open:

```text
http://<pi-ip>:8000
```

API docs:

```text
http://<pi-ip>:8000/docs
```

## 5. Test with bundled example data

```bash
source .venv/bin/activate
python scripts/import_moth_csv.py examples/sample_moth_lamp.csv \
  --collection-name "Example MOTH survey" \
  --scan-mode "example" \
  --detection-threshold-db 10

python scripts/import_candidates.py examples/candidate_sites.csv
python scripts/score_candidates.py --target-min-hz 430000000 --target-max-hz 440000000
```

Then refresh the browser map.

## 6. Install as a system service

Edit `moth-api.service` if your username or path is not `/home/pi/moth_pi_setup`.

```bash
sudo cp moth-api.service /etc/systemd/system/moth-api.service
sudo systemctl daemon-reload
sudo systemctl enable --now moth-api.service
sudo systemctl status moth-api.service
```

## 7. Candidate CSV format

```csv
name,lat,lon,antenna_height_agl_m,practical_score,site_notes
Candidate Alpha,2.0149,45.3048,8,0.8,Sample site near survey centre
```

## 8. Current parser assumptions

The parser accepts many likely LAMP/MOTH column names and maps them to:

```text
timestamp_utc
lat
lon
altitude_msl_m
satellites_seen
frequency_hz
signal_type
strength_dbm
age_s
scan_range_id
```

Frequency values labelled as MHz or GHz are converted to Hz. Plain numeric values between 5 and 6000 are treated as MHz because MOTH covers 5 MHz to 6 GHz.

## 9. Scoring model

The first scoring model is deliberately simple and visible:

```text
score = lower-tail signal strength
      + median signal strength
      + target-band event availability
      + low strong-non-target interference
      + practical site score
      + data confidence
```

It is an analysis aid only. Do not use it as an unsupervised transmit-control system.

## 10. Next upgrades

1. Lock the parser against a real LAMP CSV.
2. Add runway/TOL corridor geometry as a GeoJSON layer.
3. Add offline MBTiles instead of internet map tiles.
4. Add terrain/building line-of-sight checks.
5. Add AI HAT+ 2 inference for anomaly/interference/site-risk classification.
