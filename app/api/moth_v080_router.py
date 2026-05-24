"""
Optional FastAPI router for MOTH v0.8.0 decision workflow endpoints.

Integration:
    from moth_v080_router import router as moth_v080_router
    app.include_router(moth_v080_router)

The existing app can continue serving legacy dashboards while these endpoints
support the v0.8.0 home screen, decision cards, data quality dashboard and reports.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

try:
    from fastapi import APIRouter, HTTPException
    from pydantic import BaseModel, Field
except Exception as exc:  # pragma: no cover - allows static inspection without FastAPI installed.
    APIRouter = None  # type: ignore
    BaseModel = object  # type: ignore
    Field = None  # type: ignore
    HTTPException = RuntimeError  # type: ignore

from moth_v080_core import (
    CandidateSite,
    calculate_data_quality,
    explain_result,
    rank_launch_windows,
    records_from_mappings,
    render_candidate_report,
    render_launch_report,
    score_candidate_sites,
)


if APIRouter is None:
    router = None
else:
    router = APIRouter(prefix="/api/v0_8", tags=["MOTH v0.8.0"])


class RowsPayload(BaseModel):  # type: ignore[misc]
    rows: List[Dict[str, Any]]


class LaunchPayload(BaseModel):  # type: ignore[misc]
    rows: List[Dict[str, Any]]
    bands: List[str] = ["L1", "L2", "L5"]
    window_minutes: int = 30
    step_minutes: int = 10
    spike_threshold_dbm: float = -60.0
    local_utc_offset_hours: int = 3


class CandidatePayload(BaseModel):  # type: ignore[misc]
    rows: List[Dict[str, Any]]
    candidates: List[Dict[str, Any]]
    target_bands: List[str] = ["L1", "L2", "L5"]
    radius_meters: float = 100.0


class ExplainPayload(BaseModel):  # type: ignore[misc]
    result_type: str
    result: Dict[str, Any]


class ReportPayload(BaseModel):  # type: ignore[misc]
    report_type: str
    decision: Dict[str, Any]


if router is not None:

    @router.post("/data-quality")
    def data_quality(payload: RowsPayload) -> Dict[str, Any]:
        records = records_from_mappings(payload.rows)
        return calculate_data_quality(records)

    @router.post("/launch/recommendation")
    def launch_recommendation(payload: LaunchPayload) -> Dict[str, Any]:
        records = records_from_mappings(payload.rows)
        return rank_launch_windows(
            records,
            bands=payload.bands,
            window_minutes=payload.window_minutes,
            step_minutes=payload.step_minutes,
            spike_threshold_dbm=payload.spike_threshold_dbm,
            local_utc_offset_hours=payload.local_utc_offset_hours,
        )

    @router.post("/candidates/recommendation")
    def candidate_recommendation(payload: CandidatePayload) -> Dict[str, Any]:
        records = records_from_mappings(payload.rows)
        try:
            candidates = [CandidateSite(name=str(c["name"]), latitude=float(c.get("latitude", c.get("lat"))), longitude=float(c.get("longitude", c.get("lon", c.get("lng"))))) for c in payload.candidates]
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Invalid candidate payload: {exc}")
        return score_candidate_sites(records, candidates, target_bands=payload.target_bands, radius_meters=payload.radius_meters)

    @router.post("/explain")
    def explain(payload: ExplainPayload) -> Dict[str, Any]:
        return {"explanation": explain_result(payload.result, payload.result_type)}

    @router.post("/reports")
    def report(payload: ReportPayload) -> Dict[str, Any]:
        if payload.report_type.lower() == "candidate":
            return {"report_markdown": render_candidate_report(payload.decision)}
        if payload.report_type.lower() == "launch":
            return {"report_markdown": render_launch_report(payload.decision)}
        raise HTTPException(status_code=400, detail="report_type must be candidate or launch")
