# Changelog

## v0.8.0 decision workflow MVP

Added:

- Home screen with Antenna placement, Launch window, Data quality and Reports options.
- Briefing, Analyst and Admin modes.
- Browser-side CSV parser for UX testing.
- Data quality dashboard with GPS, 0,0 rows, time span, gaps, scan count, duplicates and frequency coverage checks.
- Decision card model for antenna candidate and launch timing.
- Explain-this-result outputs generated from deterministic metrics.
- Launch workflow progress indicator.
- Candidate and launch report templates.
- Optional FastAPI router and pure Python core functions.

Kept:

- v0.6.0 main dashboard concept.
- v0.7.5 launch RF dashboard concept.
- EEI framing and operational caveat.
