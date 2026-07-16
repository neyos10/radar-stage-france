"""Calcul prudent du budget mensuel GitHub Actions du radar."""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass
from typing import Iterable


DEFAULT_QUOTA_MINUTES = 3000
DEFAULT_RESERVE_MINUTES = 50
MINIMUM_RESERVE_MINUTES = 50


def _non_negative_integer(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} doit être un entier positif ou nul")
    return value


def _non_negative_number(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} doit être un nombre positif ou nul")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{name} doit être un nombre positif ou nul")
    return result


def billable_minutes(duration_seconds: float) -> int:
    """Arrondit prudemment la durée d'un job à la minute supérieure."""

    seconds = _non_negative_number(duration_seconds, "duration_seconds")
    return math.ceil(seconds / 60)


def p95_seconds(samples: Iterable[float]) -> float:
    """Percentile p95 par rang le plus proche, sans interpolation optimiste."""

    values = sorted(
        _non_negative_number(value, "sample") for value in samples
    )
    if not values:
        raise ValueError("Au moins une durée est requise pour calculer le p95")
    rank = max(1, math.ceil(0.95 * len(values)))
    return values[rank - 1]


@dataclass(frozen=True)
class ActionsBudget:
    quota_minutes: int
    reserve_minutes: int
    plannable_minutes: int
    fast_billable_minutes_per_run: int
    fast_runs: int
    fast_minutes: int
    full_billable_minutes_per_run: int
    full_runs: int
    full_minutes: int
    other_workflows_minutes: int
    planned_total_minutes: int
    quota_remaining_minutes: int
    margin_after_reserve_minutes: int
    overrun_minutes: int
    within_plannable_budget: bool


def calculate_actions_budget(
    *,
    fast_p95_seconds: float,
    fast_runs: int,
    full_p95_seconds: float,
    full_runs: int,
    other_workflows_minutes: int = 0,
    quota_minutes: int = DEFAULT_QUOTA_MINUTES,
    reserve_minutes: int = DEFAULT_RESERVE_MINUTES,
) -> ActionsBudget:
    """Projette le coût mensuel sans choisir de fréquence de scan.

    Chaque durée p95 est d'abord arrondie à la minute supérieure, puis
    multipliée par son nombre de runs. La limite planifiable vaut par défaut
    ``3000 - 50 = 2950`` minutes.
    """

    fast_runs = _non_negative_integer(fast_runs, "fast_runs")
    full_runs = _non_negative_integer(full_runs, "full_runs")
    other = _non_negative_integer(
        other_workflows_minutes, "other_workflows_minutes"
    )
    quota = _non_negative_integer(quota_minutes, "quota_minutes")
    reserve = _non_negative_integer(reserve_minutes, "reserve_minutes")
    if reserve < MINIMUM_RESERVE_MINUTES:
        raise ValueError(
            f"reserve_minutes doit être au moins {MINIMUM_RESERVE_MINUTES}"
        )
    if reserve > quota:
        raise ValueError("reserve_minutes ne peut pas dépasser quota_minutes")

    fast_per_run = billable_minutes(fast_p95_seconds)
    full_per_run = billable_minutes(full_p95_seconds)
    fast_total = fast_per_run * fast_runs
    full_total = full_per_run * full_runs
    total = fast_total + full_total + other
    plannable = quota - reserve
    margin = plannable - total
    return ActionsBudget(
        quota_minutes=quota,
        reserve_minutes=reserve,
        plannable_minutes=plannable,
        fast_billable_minutes_per_run=fast_per_run,
        fast_runs=fast_runs,
        fast_minutes=fast_total,
        full_billable_minutes_per_run=full_per_run,
        full_runs=full_runs,
        full_minutes=full_total,
        other_workflows_minutes=other,
        planned_total_minutes=total,
        quota_remaining_minutes=quota - total,
        margin_after_reserve_minutes=margin,
        overrun_minutes=max(0, -margin),
        within_plannable_budget=margin >= 0,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Projette le budget mensuel GitHub Actions du radar."
    )
    parser.add_argument("--fast-p95-seconds", type=float, required=True)
    parser.add_argument("--fast-runs", type=int, required=True)
    parser.add_argument("--full-p95-seconds", type=float, required=True)
    parser.add_argument("--full-runs", type=int, required=True)
    parser.add_argument("--other-workflows-minutes", type=int, default=0)
    parser.add_argument("--quota-minutes", type=int, default=DEFAULT_QUOTA_MINUTES)
    parser.add_argument("--reserve-minutes", type=int, default=DEFAULT_RESERVE_MINUTES)
    args = parser.parse_args(argv)
    projection = calculate_actions_budget(
        fast_p95_seconds=args.fast_p95_seconds,
        fast_runs=args.fast_runs,
        full_p95_seconds=args.full_p95_seconds,
        full_runs=args.full_runs,
        other_workflows_minutes=args.other_workflows_minutes,
        quota_minutes=args.quota_minutes,
        reserve_minutes=args.reserve_minutes,
    )
    print(json.dumps(asdict(projection), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
