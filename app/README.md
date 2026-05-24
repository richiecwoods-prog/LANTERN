# MOTH v0.8.0 Decision Workflow Build Package

## Build intent

This package implements the first v0.8.0 build slice for the MOTH firmware + field-readiness increment. It shifts the app from a technical analysis dashboard toward a guided decision workflow:

1. Home screen with four major tasks.
2. Briefing / Analyst / Admin modes.
3. Data quality dashboard.
4. Decision cards for antenna placement and launch timing.
5. Explain-this-result text generated from deterministic metrics.
6. Loading/progress feedback for heavier actions.
7. One-page candidate and launch report templates.

## Files

```text
static/moth_v080_decision_workflow.html   Standalone v0.8.0 decision workflow MVP
static/moth_v080.css                      Shared UX styling
static/moth_v080.js                       Browser-side CSV parser and deterministic scoring demo
api/moth_v080_core.py                     Deterministic backend analysis helpers
api/moth_v080_router.py                   Optional FastAPI router
api/__init__.py                           Package marker
tests/test_moth_v080_core.py              Minimal regression tests
docs/acceptance_checks_v080.md            Build acceptance checks
docs/integration_notes_v080.md            Merge notes for the current MOTH app
```

## Quick local use

Copy the `static/` files into the existing app's static directory and open:

```text
http://192.168.0.120:8000/static/moth_v080_decision_workflow.html?v=080
```

This standalone page can parse uploaded CSV files directly in the browser. It is suitable for initial UX testing before wiring into the current backend.

## Optional FastAPI integration

Copy the `api/` files into the backend project and include the router:

```python
from api.moth_v080_router import router as moth_v080_router

app.include_router(moth_v080_router)
```

Endpoints provided:

```text
POST /api/v0_8/data-quality
POST /api/v0_8/launch/recommendation
POST /api/v0_8/candidates/recommendation
POST /api/v0_8/explain
POST /api/v0_8/reports
```

The router expects rows already parsed into dictionaries. The pure core module also includes CSV-file parsing helpers for backend-side use.

## Deterministic scoring rule

The API calculates metrics and scores. AI HAT+ 2, if used later, should only turn those metrics into short operator explanations. It should not invent, override or silently modify a score.

## Suggested merge order

1. Add static decision workflow page at `/static/moth_v080_decision_workflow.html?v=080`.
2. Wire the home page buttons to the existing main dashboard, launch dashboard and new data quality/report sections.
3. Add the FastAPI router or equivalent backend endpoints.
4. Validate with real MOTH/LAMP CSV exports.
5. Update the root page only after the v0.8.0 flow is accepted.

## Field-readiness milestone

Recommended milestone name: **Controlled Validation Test Report**.

The v0.8.0 patch should be accepted only after test results confirm:

- firmware version and update process are documented,
- LAMP log export behavior is verified,
- data quality checks detect poor inputs,
- launch and candidate decision cards produce traceable results,
- reports include the required caveat,
- field users can operate the workflow without engineering support.
