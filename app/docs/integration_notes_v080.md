# MOTH v0.8.0 Integration Notes

## Current baseline retained

The current app baseline remains:

- Main dashboard v0.6.0 for scan upload, map hexagons, candidate scoring and candidate reports.
- Launch RF dashboard v0.7.5 for L1/L2/L5 graph, spectrum/spikes, pattern of life and launch-window ranking.

v0.8.0 should not remove these pages initially. It should add a decision-led entry point above them.

## Proposed routing

```text
/                                           v0.8.0 home screen after acceptance
/static/moth_v080_decision_workflow.html    v0.8.0 prototype route now
/?v=060                                     legacy main dashboard
/static/launch_analysis.html?v=075          legacy launch RF dashboard
/api/v0_8/data-quality                      new data quality endpoint
/api/v0_8/launch/recommendation             new launch decision endpoint
/api/v0_8/candidates/recommendation         new candidate decision endpoint
/api/v0_8/explain                           explanation endpoint
/api/v0_8/reports                           report endpoint
```

## Implementation notes

### Home screen

The first screen should show four large choices:

1. Antenna placement
2. Launch window
3. Data quality
4. Reports

This is the main cognitive-load reduction.

### Modes

Use CSS or app state to hide advanced controls in Briefing mode. Keep Analyst and Admin modes available for engineering and troubleshooting.

### Data quality

Run data quality before trusting any candidate or launch card. At minimum, show:

- valid GPS percentage,
- 0,0 rows,
- time span,
- time gaps,
- scan count,
- duplicate rows,
- frequency coverage,
- row percentage inside 5 MHz to 6 GHz.

### Deterministic score / AI split

The deterministic API must calculate scores and traceable metrics. AI HAT+ 2 can generate operator-facing explanations from those metrics, but it must not create or override scores.

### Reports

Reports should be one page where possible and should end with the operational caveat.

## Merge-safe approach

1. Add static assets and open them directly.
2. Compare v0.8.0 outputs against existing v0.6.0/v0.7.5 outputs.
3. Wire API endpoints.
4. Switch the root route to the v0.8.0 home page only after operator review.
