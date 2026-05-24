from __future__ import annotations

import argparse
import json

from moth_analysis.scoring import score_candidate_sites


def main() -> None:
    p = argparse.ArgumentParser(description="Score candidate antenna sites against imported MOTH data")
    p.add_argument("--radius-m", type=float, default=1500)
    p.add_argument("--target-min-hz", type=float)
    p.add_argument("--target-max-hz", type=float)
    args = p.parse_args()
    print(json.dumps(score_candidate_sites(radius_m=args.radius_m, target_min_hz=args.target_min_hz, target_max_hz=args.target_max_hz), indent=2))


if __name__ == "__main__":
    main()
