# LANTERN navigation overlap audit v0.12

## Canonical split

- Reporting owns end-state outputs: Flight Safety, Mission Brief, J2 Live Report, GNSS Serviceability, Candidate Report, Evidence Log and Export Pack.
- Engineering owns technical evidence: Data Quality Detail, RF Analyst, Spectrum/Spikes, Map/H3, Candidate Engineering, Pattern of Life, Import Diagnostics and API Payload Viewer.

## Keep controlled overlap

- Flight Safety and RF Analyst both use GNSS/RF data: Reporting summarizes; Engineering explains.
- Mission Brief and J2 both mention security context: Mission Brief summarizes; J2 owns source detail.
- Candidate Report and Map/Candidate Engineering share candidate scores: Engineering creates evidence; Reporting packages the end-state recommendation.

## Remove bad overlap

- Do not restore /rotator. Article rotation belongs inside J2 Live Report.
- Do not allow duplicate platform headers. One page = one global platform shell.
- Do not let multiple readiness pages calculate contradictory conclusions.
