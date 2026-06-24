# LANTERN Sensor-Agnostic Patent Demo Notes

## Demo file

Open this file in any modern browser:

```text
app/moth_pi_setup/moth_analysis/static/patent_demo.html
```

The demo is a standalone HTML page. It does not require Python, Node, FastAPI, a database, MOTH hardware, or real scan data.

## Purpose

The page is built for lawyer and patent-office discussion. It presents LANTERN as a sensor-agnostic RF/GNSS decision-support process:

1. Acquire telemetry from a compatible field sensor.
2. Normalise device-specific output into a common evidence model.
3. Reject or flag poor-quality measurements.
4. Assess GNSS-band RF burden, strong spikes and confidence.
5. Rank launch windows.
6. Rank candidate antenna or operating sites.
7. Generate a caveated operator brief.

## Patent positioning

Use this phrasing:

```text
MOTH is the current embodiment. The invention is the sensor-agnostic RF/GNSS evidence pipeline that accepts compatible telemetry from MOTH, SDR receivers, GNSS monitors or future sensors, converts it into a common model, quality-gates it, and produces traceable launch-window, GNSS-serviceability and site-selection recommendations.
```

## Safe sharing

The demo uses synthetic data and illustrative metrics only. Avoid making a public web link before lawyer review or filing advice, because uncontrolled publication may count as disclosure.

Recommended options:

1. Use the standalone HTML file offline.
2. Share the generated zip only with the lawyer/patent office.
3. If online access is needed, host it behind a private company-controlled link.
