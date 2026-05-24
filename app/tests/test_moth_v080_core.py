from datetime import datetime, timedelta, timezone

from api.moth_v080_core import (
    GNSS_BANDS_HZ,
    MothRecord,
    calculate_data_quality,
    explain_result,
    rank_launch_windows,
    score_candidate_sites,
)


def make_records():
    base = datetime(2026, 5, 12, 6, 0, tzinfo=timezone.utc)
    rows = []
    for i in range(24):
        t = base + timedelta(minutes=10 * i)
        event_count = 1 if 6 <= i <= 10 else 3 if 16 <= i <= 20 else 0
        for band, centre in GNSS_BANDS_HZ.items():
            if band not in ("L1", "L2", "L5"):
                continue
            for n in range(event_count):
                rows.append(MothRecord(t, 2.046 + i * 0.00001, 45.318 + n * 0.00001, centre + n * 100000, -82 + n, "demo.csv", len(rows)+1))
    return rows


def test_data_quality_returns_category():
    result = calculate_data_quality(make_records())
    assert result["category"] in {"HIGH", "MEDIUM", "LOW", "NO DATA"}
    assert "checks" in result


def test_launch_has_decision_card_fields():
    result = rank_launch_windows(make_records())
    decision = result["decision"]
    assert decision["category"] in {"RECOMMENDED", "BEST VIABLE", "LEAST-BUSY OBSERVED", "AVOID IF POSSIBLE", "NO DATA"}
    assert "reason" in decision
    assert "next_action" in decision


def test_candidate_scoring_returns_best_candidate():
    result = score_candidate_sites(make_records(), [{"name": "A", "latitude": 2.046, "longitude": 45.318}])
    assert result["decision"]["candidate"] == "A"
    assert result["decision"]["score"] >= 0


def test_explain_launch_result_is_deterministic_text():
    launch = rank_launch_windows(make_records())
    text = explain_result(launch["decision"], "launch")
    assert "selected" in text.lower()
    assert str(launch["decision"]["category"]) in text
