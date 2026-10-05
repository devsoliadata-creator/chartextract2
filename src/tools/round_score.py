"""Deterministic panel and per-series scores for Python extraction rounds.

Deterministic - built only from numbers the extraction already computed (no new detection, no
model call).  Order of comparison, worst to best:

1. ``axis_passed`` - the hard axis-range check (``hard_checks["passed"]``).  A round whose numbers
   fail the printed axis always loses to one that passes, regardless of how many points it found:
   a wrong calibration makes every point wrong.
2. ``capped_count`` - total points found, capped per series by agent 02's own expected counts
   (``count_check``) when it gave any: ``sum(min(found, expected))`` so a round that overshoots a
   series (duplicates, noise) does not look better than one that matched the expectation.  A round
   with no ``count_check`` rows falls back to its raw point total, since there is nothing to cap
   against.
3. ``recall`` - the redraw recall from ``hard_checks`` (share of native marker ink covered),
   breaking ties between rounds that pass the axis check and found the same capped count.

Agent 02 keeps expected counts stable across an optimization group, which makes the
per-series rankings comparable.  The final Python candidate is composed from the
winning snapshot for each series.  Ties keep the earliest round.
"""

from __future__ import annotations


def capped_count(count_check, total_points) -> int:
    """Points found, capped per series by expected counts when agent 02 gave any."""
    rows = list(count_check or [])
    if not rows:
        return int(total_points)
    return sum(min(int(row["found"]), int(row["expected"])) for row in rows)


def _metric(value):
    return float(value) if value is not None else -1.0


def score_round(hard_checks: dict, count_check: list, total_points: int, series_counts=None) -> dict:
    """One round's score, ready to compare and to write as ``score.json``."""
    hard_checks = hard_checks or {}
    recall = ((hard_checks.get("redraw") or {}).get("recall"))
    series_counts = {str(label): int(count) for label, count in (series_counts or {}).items()}
    redraw = ((hard_checks.get("redraw") or {}).get("series") or {})
    labels = set(series_counts) | {str(row.get("label")) for row in count_check or []}
    per_series = {}
    for label in sorted(labels):
        rows = [row for row in count_check or [] if str(row.get("label")) == label]
        found = series_counts.get(label, sum(int(row.get("found", 0)) for row in rows))
        expected = sum(int(row.get("expected", 0)) for row in rows)
        capped = sum(min(int(row.get("found", 0)), int(row.get("expected", 0))) for row in rows) if rows else found
        redraw_series = redraw.get(label) or {}
        series_recall = redraw_series.get("recall")
        series_precision = redraw_series.get("precision")
        # Expected-count coverage is the primary series-specific measure.  If
        # no expectation exists, native-pixel recall is the best available
        # coverage signal, followed by precision and marker count.
        coverage = min(1.0, capped / expected) if expected > 0 else _metric(series_recall)
        per_series[label] = {
            "axis_passed": bool(hard_checks.get("passed")),
            "expected": expected if rows else None,
            "found": found,
            "capped_count": capped,
            "coverage": coverage,
            "recall": float(series_recall) if series_recall is not None else None,
            "precision": float(series_precision) if series_precision is not None else None,
        }
    return {
        "axis_passed": bool(hard_checks.get("passed")),
        "capped_count": capped_count(count_check, total_points),
        "recall": float(recall) if recall is not None else None,
        "total_points": int(total_points),
        "per_series": per_series,
    }


def _sort_key(score: dict) -> tuple:
    recall = score.get("recall")
    return (bool(score.get("axis_passed")), int(score.get("capped_count", 0)), recall if recall is not None else -1.0)


def pick_best(scores: dict) -> int:
    """The best round number in ``{round_no: score}`` (see module docstring for the order).

    Ties keep the earliest round, so a re-extraction that changes nothing measurable never
    displaces the round already kept.
    """
    if not scores:
        raise ValueError("no rounds to score")
    return max(scores, key=lambda round_no: (_sort_key(scores[round_no]), -int(round_no)))


def _series_sort_key(score: dict) -> tuple:
    return (
        bool(score.get("axis_passed")),
        _metric(score.get("coverage")),
        _metric(score.get("precision")),
        _metric(score.get("recall")),
        int(score.get("capped_count", 0)),
    )


def pick_best_by_series(scores: dict) -> dict[str, int]:
    """Return ``{series_label: round_no}`` using each series' own score.

    A series absent from a round is not eligible for selection from that round.
    Full ties keep the earlier extraction snapshot.
    """
    candidates = {}
    for round_no, score in scores.items():
        for label, series_score in (score.get("per_series") or {}).items():
            candidates.setdefault(str(label), {})[int(round_no)] = series_score
    return {
        label: max(rounds, key=lambda round_no: (_series_sort_key(rounds[round_no]), -int(round_no)))
        for label, rounds in sorted(candidates.items())
    }
