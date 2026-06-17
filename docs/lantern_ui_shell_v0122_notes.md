# LANTERN v0.12.2 UI hotfix notes

- Removed remaining LANTERN Flow controls more aggressively using CSS, JS, iframe cleanup and mutation observation.
- Rebuilt wrapper pages as flex-column shell + content frames so the global shell cannot sit on top of map/RF/J2 content.
- Added map-workspace direct-page safeguards for legacy map pages still opened outside canonical wrappers.
- Updated tactical styling toward cobalt, carbon-fibre black, metallic silver and dark control surfaces.
- Added a stronger tactical EEI eagle SVG placeholder at static/eei_tactical_eagle.svg. Replace with approved EEI artwork when available.
- No GNSS scoring, RF burden logic, J2 live feed logic, import quality filtering or database schema changed.
