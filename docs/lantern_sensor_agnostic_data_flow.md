# LANTERN Sensor-Agnostic Data Flow

Purpose: simple patent-advisor diagram showing inputs, outputs, processes and the main decision points.

## Diagram

```mermaid
flowchart LR
  S1["Sensor telemetry\nMOTH / SDR / GNSS monitor / other scanner"] --> P1["Import and normalise\nMap device logs to common event fields"]
  S2["Mission context\nAOI, time window, candidate sites"] --> P1
  S3["Rules and thresholds\nGNSS bands, spike level, confidence rules"] --> P2

  P1 --> D1{"Can each record be mapped?"}
  D1 -- "No" --> Q1["Reject or flag row\nReason recorded"]
  D1 -- "Yes" --> E1["Common evidence model\nTime, frequency, dBm, location, source, confidence"]

  Q1 --> O1["Data quality output\nRejects, flags, caveats"]
  E1 --> P2["Quality gate and confidence\nRemove unusable rows, retain caveated evidence"]

  P2 --> D2{"Enough clean evidence?"}
  D2 -- "No" --> O2["Limited-confidence output\nCollect more data or widen filters"]
  D2 -- "Yes" --> P3["RF/GNSS analysis\nBand overlap, spikes, patterns, proximity"]

  P3 --> D3{"RF/GNSS burden acceptable?"}
  D3 -- "No" --> O3["Avoid, delay or re-scan\nDecision support only"]
  D3 -- "Yes" --> P4["Rank options\nLaunch windows and candidate sites"]

  P4 --> O4["Operator outputs\nLaunch window, site recommendation, GNSS/serviceability brief, evidence pack"]
```

## Inputs

| Input | Basic detail |
| --- | --- |
| Sensor telemetry | RF/GNSS events from MOTH, SDR receiver, GNSS monitor or another compatible scanner. |
| Mission context | Area of interest, time window, selected collections and candidate sites. |
| Rules and thresholds | GNSS band definitions, spike threshold, quality rules and confidence rules. |

## Processes

| Process | Basic detail |
| --- | --- |
| Import and normalise | Convert device-specific logs into one common evidence model. |
| Quality gate and confidence | Reject unusable records, flag suspect records and calculate confidence. |
| RF/GNSS analysis | Check GNSS-band overlap, strong RF spikes, persistence, timing and location proximity. |
| Rank options | Rank launch windows and candidate sites using retained evidence and confidence. |
| Generate outputs | Produce a caveated operator brief and evidence pack. |

## Decision Points

| Decision point | Yes path | No path |
| --- | --- | --- |
| Can each record be mapped? | Store in common evidence model. | Reject or flag row and record the reason. |
| Enough clean evidence? | Continue to RF/GNSS analysis. | Produce limited-confidence output and recommend more data or wider filters. |
| RF/GNSS burden acceptable? | Rank launch windows and candidate sites. | Recommend avoid, delay, re-scan or validate before use. |

## Outputs

| Output | Basic detail |
| --- | --- |
| Data quality output | Counts of accepted, rejected and flagged records with reasons. |
| Launch-window recommendation | Ranked time windows with caveats and confidence. |
| Candidate-site recommendation | Ranked antenna or operating locations with supporting evidence. |
| GNSS/serviceability brief | RF/GNSS burden summary and validation caveats. |
| Evidence pack | Traceable source, assumptions, decision status and caveats for review. |

## Boundary Statement

LANTERN provides decision support only. It does not authorise flight, certify GNSS integrity, guarantee communications performance or attribute interference to a source.
