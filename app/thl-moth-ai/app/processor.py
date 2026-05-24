import json
from pathlib import Path
from typing import Dict, List

import numpy as np
import pandas as pd
from sklearn.cluster import DBSCAN

from .config import FIELD_ALIASES


def _find_column(df: pd.DataFrame, aliases: List[str]):
    for name in aliases:
        if name in df.columns:
            return name
    lower = {c.lower(): c for c in df.columns}
    for name in aliases:
        if name.lower() in lower:
            return lower[name.lower()]
    return None


def load_moth_file(path: Path) -> pd.DataFrame:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        df = pd.read_csv(path)
    elif suffix in [".json", ".geojson"]:
        raw = json.loads(path.read_text())
        if isinstance(raw, dict) and "features" in raw:
            rows = []
            for f in raw["features"]:
                props = f.get("properties", {}) or {}
                coords = (f.get("geometry", {}) or {}).get("coordinates", [])
                if len(coords) >= 2:
                    props["lon"] = coords[0]
                    props["lat"] = coords[1]
                rows.append(props)
            df = pd.DataFrame(rows)
        else:
            df = pd.DataFrame(raw if isinstance(raw, list) else raw.get("records", []))
    else:
        raise ValueError("Unsupported file type. Use CSV, JSON or GeoJSON.")

    mapped: Dict[str, str] = {}
    for canonical, aliases in FIELD_ALIASES.items():
        col = _find_column(df, aliases)
        if col:
            mapped[canonical] = col

    if "lat" not in mapped or "lon" not in mapped:
        raise ValueError("MOTH file needs latitude/longitude columns.")

    out = pd.DataFrame()
    out["lat"] = pd.to_numeric(df[mapped["lat"]], errors="coerce")
    out["lon"] = pd.to_numeric(df[mapped["lon"]], errors="coerce")
    out["rssi"] = pd.to_numeric(df[mapped["rssi"]], errors="coerce") if "rssi" in mapped else np.nan
    out["freq"] = pd.to_numeric(df[mapped["freq"]], errors="coerce") if "freq" in mapped else np.nan
    out["time"] = df[mapped["time"]].astype(str) if "time" in mapped else ""
    out = out.dropna(subset=["lat", "lon"]).reset_index(drop=True)

    if out.empty:
        raise ValueError("No valid georeferenced rows found.")

    out["quality"] = out["rssi"].fillna(out["rssi"].median() if not out["rssi"].dropna().empty else -100)
    out["quality_norm"] = (out["quality"] - out["quality"].min()) / max((out["quality"].max() - out["quality"].min()), 1e-9)
    return out


def analyse(df: pd.DataFrame) -> Dict:
    coords = df[["lat", "lon"]].to_numpy()
    eps_deg = 0.0015  # roughly 150-170 m near equator
    labels = DBSCAN(eps=eps_deg, min_samples=3).fit_predict(coords) if len(df) >= 3 else np.zeros(len(df), dtype=int)
    df = df.copy()
    df["cluster"] = labels

    candidates = []
    for cluster_id in sorted(set(labels)):
        if cluster_id == -1:
            continue
        part = df[df["cluster"] == cluster_id]
        score = float(part["quality_norm"].mean() * 60 + min(len(part), 40))
        candidates.append({
            "cluster": int(cluster_id),
            "lat": float(part["lat"].mean()),
            "lon": float(part["lon"].mean()),
            "detections": int(len(part)),
            "mean_rssi": None if part["rssi"].isna().all() else float(part["rssi"].mean()),
            "score": round(score, 1),
            "recommendation": "Primary candidate" if score >= 70 else "Secondary candidate" if score >= 45 else "Low confidence",
        })

    candidates = sorted(candidates, key=lambda x: x["score"], reverse=True)
    return {
        "count": int(len(df)),
        "detections": df.to_dict(orient="records"),
        "candidates": candidates,
    }
