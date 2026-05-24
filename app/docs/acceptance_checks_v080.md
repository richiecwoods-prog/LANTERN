# MOTH v0.8.0 Acceptance Checks

## 1. Home screen

- [ ] User lands on a simple home screen rather than a technical dashboard.
- [ ] Four large options are visible: Antenna placement, Launch window, Data quality, Reports.
- [ ] Existing v0.6.0/v0.7.5 dashboard links remain available during transition.

## 2. Modes

- [ ] Briefing mode shows decision card, simple map/graph, top recommendations, explanation and limitations.
- [ ] Analyst mode shows filters, graphs, raw markers, spectrum/spikes and candidate scoring.
- [ ] Admin mode shows import status, parser version, API health, duplicates and bad GPS row counts.

## 3. Data quality dashboard

- [ ] Displays valid GPS percentage.
- [ ] Flags 0,0 coordinate rows.
- [ ] Displays time span covered.
- [ ] Flags gaps in time.
- [ ] Displays scan count.
- [ ] Flags duplicate rows.
- [ ] Shows GNSS frequency coverage.
- [ ] Produces a plain result: HIGH, MEDIUM, LOW or NO DATA.
- [ ] Produces a concrete recommendation.

## 4. Decision cards

- [ ] Antenna page has a top-level recommended candidate card.
- [ ] Launch page has a top-level best timing card.
- [ ] Cards show recommendation, score, confidence, reason and next action.
- [ ] Map, graph and tables support the card; they do not compete with it.

## 5. Explain this result

- [ ] Every decision card has Explain this result.
- [ ] Explanation uses only traceable deterministic metrics.
- [ ] Explanation does not invent score values.

## 6. Launch workflow

- [ ] User can select scans.
- [ ] User can select GNSS L1, L2, L5 or L1+L2+L5 presets.
- [ ] Custom Hz fields appear only after selecting Custom.
- [ ] One button runs ranking, graph generation, spike check and summary.
- [ ] Button disables while running and shows step-by-step progress.

## 7. Reports

- [ ] Candidate site report includes candidate name, score, confidence, why selected/rejected, timeline result, limitations and next action.
- [ ] Launch window report includes recommended timing, local time, RF score, L1/L2/L5 status, spike status, caution and checklist.
- [ ] Both reports end with: "RF planning aid only. Final launch decision requires authorised operational approval and normal UAS safety checks."

## 8. Field-readiness validation

- [ ] Test Table View, Detection View and Spectrum View under controlled RF conditions.
- [ ] Validate scan range behavior from 5 MHz to 6 GHz.
- [ ] Confirm USB live CSV logging and log-to-memory behavior.
- [ ] Confirm timestamp, GPS, frequency and signal strength fields are captured correctly.
- [ ] Verify battery, antenna, charger, pouch and Pelican case configuration.
