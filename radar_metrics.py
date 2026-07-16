"""Analyse hors ligne des métriques de scan (``data/scan_metrics.json``).

Ce module est pur : aucune requête réseau, aucune écriture. Il est partagé
par ``scripts/analyze_scan_metrics.py``, le dashboard et les tests, afin que
le rendement des sources — LinkedIn Posts en premier — soit calculé une seule
fois, de la même façon partout.
"""

from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Any, Mapping, Sequence

from budget import billable_minutes, p95_seconds

METRICS_FILE = Path("data/scan_metrics.json")
LINKEDIN_POSTS_SOURCE = "LinkedIn Posts"

# Seuils de la classification de rendement. Ils restent des constantes
# nommées pour que le rapport puisse les citer et que les tests les fixent.
MIN_RUNS_INDICATIVE = 10
MIN_RUNS_RELIABLE = 20
VERY_USEFUL_NEW_PER_RUN = 1 / 3
SECONDARY_NEW_PER_RUN = 1 / 10
VERY_USEFUL_MAX_P95_S = 60.0
VERY_USEFUL_MAX_BLOCK_RATE = 0.10
UNSTABLE_ERROR_RUN_RATE = 0.30
UNSTABLE_BLOCK_RATE = 0.25

CLASSIFICATIONS = {
    "tres_utile": "Très utile — exécuter à chaque FAST",
    "utile_secondaire": "Utile mais secondaire — exécuter toutes les 2 ou 3 heures",
    "faible_rendement": "Faible rendement — exécuter uniquement dans le FULL ou en rotation",
    "instable": "Instable — réduire la fréquence et appliquer un cooldown",
    "donnees_insuffisantes": "Données insuffisantes — continuer la mesure",
}


def load_metrics_document(path: str | Path = METRICS_FILE) -> dict:
    """Charge le document de métriques ; absent ou corrompu = document vide."""
    path = Path(path)
    if not path.exists():
        return {"schema_version": 1, "profiles": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema_version": 1, "profiles": {}}
    if not isinstance(payload, dict) or not isinstance(payload.get("profiles"), dict):
        return {"schema_version": 1, "profiles": {}}
    return payload


def profile_runs(
    document: Mapping[str, Any],
    profile_id: str | None = None,
    *,
    last: int | None = None,
    include_benchmark: bool = False,
) -> list[dict]:
    """Runs d'un profil (ou de tous), du plus ancien au plus récent."""
    profiles = document.get("profiles", {})
    if not isinstance(profiles, Mapping):
        return []
    selected: list[dict] = []
    for candidate_id, history in profiles.items():
        if profile_id is not None and candidate_id != profile_id:
            continue
        if not isinstance(history, list):
            continue
        for run in history:
            if not isinstance(run, Mapping):
                continue
            if run.get("benchmark") and not include_benchmark:
                continue
            selected.append(dict(run))
    selected.sort(key=lambda run: str(run.get("run_at", "")))
    if last is not None and last >= 0:
        selected = selected[-last:]
    return selected


def source_rows(runs: Sequence[Mapping[str, Any]], source: str) -> list[dict]:
    """Lignes de statut d'une source, une par (run, région)."""
    rows: list[dict] = []
    for run in runs:
        for row in run.get("sources", []) or []:
            if isinstance(row, Mapping) and row.get("source") == source:
                enriched = dict(row)
                enriched["run_at"] = run.get("run_at", "")
                enriched["run_profile_id"] = run.get("profile_id", "")
                rows.append(enriched)
    return rows


def median_seconds(samples: Sequence[float]) -> float:
    values = [float(value) for value in samples]
    if not values:
        raise ValueError("Au moins une durée est requise pour calculer la médiane")
    return float(statistics.median(values))


def cost_per_new_offer_minutes(
    total_duration_s: float, new_count: int
) -> float | None:
    """Minutes dépensées par nouvelle offre ; ``None`` si aucune nouveauté."""
    if new_count <= 0:
        return None
    return (float(total_duration_s) / 60.0) / new_count


def format_cost(cost: float | None) -> str:
    return "non calculable" if cost is None else f"{cost:.2f} min/nouvelle"


def _duration_stats(durations: Sequence[float]) -> dict:
    if not durations:
        return {
            "total_s": 0.0, "mean_s": None, "median_s": None, "p95_s": None,
        }
    return {
        "total_s": round(sum(durations), 3),
        "mean_s": round(sum(durations) / len(durations), 3),
        "median_s": round(median_seconds(durations), 3),
        "p95_s": round(p95_seconds(durations), 3),
    }


def summarize_profile(runs: Sequence[Mapping[str, Any]]) -> dict:
    """Synthèse d'un profil : durées, nouveautés, coût par nouvelle offre."""
    durations = [float(run.get("duration_s") or 0.0) for run in runs]
    new_total = sum(int(run.get("new_offer_count") or 0) for run in runs)
    stats = _duration_stats(durations)
    last_run = dict(runs[-1]) if runs else None
    return {
        "run_count": len(runs),
        "new_offer_count": new_total,
        "duration": stats,
        "billable_minutes_p95": (
            billable_minutes(stats["p95_s"]) if stats["p95_s"] is not None else None
        ),
        "cost_per_new_offer_min": cost_per_new_offer_minutes(
            stats["total_s"], new_total
        ),
        "last_run_at": last_run.get("run_at") if last_run else None,
        "last_duration_s": (
            float(last_run.get("duration_s") or 0.0) if last_run else None
        ),
        "last_new_offer_count": (
            int(last_run.get("new_offer_count") or 0) if last_run else None
        ),
    }


def summarize_source(runs: Sequence[Mapping[str, Any]], source: str) -> dict:
    """Agrégats d'une source sur l'historique fourni."""
    rows = source_rows(runs, source)
    durations = [float(row.get("duration_s") or 0.0) for row in rows]
    raw = sum(int(row.get("raw_count") or 0) for row in rows)
    accepted = sum(int(row.get("accepted_count") or 0) for row in rows)
    new = sum(int(row.get("new_count") or 0) for row in rows)
    duplicates = sum(int(row.get("duplicate_count") or 0) for row in rows)
    unknown_dates = sum(int(row.get("unknown_date_count") or 0) for row in rows)
    blocked = sum(int(row.get("blocked_count") or 0) for row in rows)
    timeouts = sum(int(row.get("timeout_count") or 0) for row in rows)
    requests = sum(int(row.get("network_request_count") or 0) for row in rows)
    queries = sum(int(row.get("query_count") or 0) for row in rows)
    error_rows = sum(1 for row in rows if row.get("status") in {"blocked", "error"})
    stats = _duration_stats(durations)
    return {
        "source": source,
        "run_count": len(rows),
        "query_count": queries,
        "network_request_count": requests,
        "duration": stats,
        "raw_count": raw,
        "accepted_count": accepted,
        "new_count": new,
        "duplicate_count": duplicates,
        "unknown_date_count": unknown_dates,
        "blocked_count": blocked,
        "timeout_count": timeouts,
        "error_run_count": error_rows,
        "acceptance_rate": (accepted / raw) if raw else None,
        "novelty_rate": (new / accepted) if accepted else None,
        "duplicate_rate": (duplicates / raw) if raw else None,
        "unknown_date_rate": (unknown_dates / accepted) if accepted else None,
        "block_rate": (blocked / requests) if requests else None,
        "error_run_rate": (error_rows / len(rows)) if rows else None,
        "new_per_run": (new / len(rows)) if rows else None,
        "cost_per_new_offer_min": cost_per_new_offer_minutes(stats["total_s"], new),
    }


def linkedin_posts_query_rows(runs: Sequence[Mapping[str, Any]]) -> list[dict]:
    """Aplati les rapports par requête X-ray, avec la référence du run."""
    rows: list[dict] = []
    for status in source_rows(runs, LINKEDIN_POSTS_SOURCE):
        for report in status.get("query_reports", []) or []:
            if not isinstance(report, Mapping):
                continue
            enriched = dict(report)
            enriched["run_at"] = status.get("run_at", "")
            enriched["run_profile_id"] = status.get("run_profile_id", "")
            rows.append(enriched)
    return rows


def summarize_linkedin_posts(runs: Sequence[Mapping[str, Any]]) -> dict:
    """Rendement complet de LinkedIn Posts, requête par requête incluse."""
    summary = summarize_source(runs, LINKEDIN_POSTS_SOURCE)
    query_rows = linkedin_posts_query_rows(runs)
    summary["executed_query_count"] = len(query_rows)
    summary["query_rows"] = query_rows
    providers = sorted({
        str(row.get("provider")) for row in query_rows if row.get("provider")
    })
    summary["providers"] = providers
    return summary


def classify_linkedin_posts(
    summary: Mapping[str, Any],
    *,
    min_runs: int = MIN_RUNS_INDICATIVE,
    reliable_runs: int = MIN_RUNS_RELIABLE,
) -> dict:
    """Classe le rendement et recommande une fréquence, sans changer le cron."""
    run_count = int(summary.get("run_count") or 0)
    if run_count < min_runs:
        return {
            "classification": "donnees_insuffisantes",
            "label": CLASSIFICATIONS["donnees_insuffisantes"],
            "recommendation": (
                f"Continuer la mesure : {run_count} run(s) sur les {min_runs} "
                "requis pour une première indication."
            ),
            "confidence": "aucune",
            "run_count": run_count,
        }
    confidence = "fiable" if run_count >= reliable_runs else "indicative"
    block_rate = float(summary.get("block_rate") or 0.0)
    error_run_rate = float(summary.get("error_run_rate") or 0.0)
    new_per_run = float(summary.get("new_per_run") or 0.0)
    p95 = summary.get("duration", {}).get("p95_s")

    if error_run_rate >= UNSTABLE_ERROR_RUN_RATE or block_rate >= UNSTABLE_BLOCK_RATE:
        key = "instable"
        recommendation = (
            "Réduire la fréquence et appliquer un cooldown : "
            f"{error_run_rate:.0%} de runs en erreur, "
            f"{block_rate:.0%} de requêtes bloquées."
        )
    elif (
        new_per_run >= VERY_USEFUL_NEW_PER_RUN
        and block_rate < VERY_USEFUL_MAX_BLOCK_RATE
        and (p95 is None or p95 <= VERY_USEFUL_MAX_P95_S)
    ):
        key = "tres_utile"
        recommendation = (
            "Exécuter à chaque FAST : au moins une nouvelle publication "
            f"tous les 3 runs ({new_per_run:.2f}/run), p95 raisonnable, "
            f"blocage {block_rate:.0%}."
        )
    elif new_per_run >= SECONDARY_NEW_PER_RUN:
        key = "utile_secondaire"
        recommendation = (
            "Exécuter toutes les 2 ou 3 heures : environ une nouvelle "
            f"publication tous les {1 / new_per_run:.0f} runs."
        )
    else:
        key = "faible_rendement"
        recommendation = (
            "Exécuter uniquement dans le FULL ou en rotation : moins d'une "
            "nouvelle publication tous les 10 runs "
            f"({new_per_run:.2f}/run), coût "
            f"{format_cost(summary.get('cost_per_new_offer_min'))}."
        )
    return {
        "classification": key,
        "label": CLASSIFICATIONS[key],
        "recommendation": recommendation,
        "confidence": confidence,
        "run_count": run_count,
        "new_per_run": round(new_per_run, 4),
        "block_rate": round(block_rate, 4),
        "error_run_rate": round(error_run_rate, 4),
        "p95_s": p95,
    }


def detect_linkedin_posts_anomalies(
    runs: Sequence[Mapping[str, Any]],
    current_status: Mapping[str, Any] | None = None,
) -> list[str]:
    """Anomalies techniques justifiant un diagnostic, jamais un simple zéro.

    Règles : trois runs consécutifs en erreur, taux de blocage important,
    durée anormalement élevée, ou provider principal et fallback tous deux
    indisponibles sur le dernier run.
    """
    rows = source_rows(runs, LINKEDIN_POSTS_SOURCE)
    if current_status is not None:
        rows = rows + [dict(current_status)]
    anomalies: list[str] = []
    if not rows:
        return anomalies

    recent = rows[-3:]
    if len(recent) == 3 and all(
        row.get("status") in {"blocked", "error"} for row in recent
    ):
        anomalies.append(
            "LinkedIn Posts : 3 runs consécutifs en erreur ou bloqués"
        )

    window = rows[-10:]
    requests = sum(int(row.get("network_request_count") or 0) for row in window)
    blocked = sum(int(row.get("blocked_count") or 0) for row in window)
    if requests >= 3 and blocked / requests >= 0.5:
        anomalies.append(
            f"LinkedIn Posts : taux de blocage important "
            f"({blocked}/{requests} requêtes sur les derniers runs)"
        )

    durations = [float(row.get("duration_s") or 0.0) for row in rows[:-1]]
    last_duration = float(rows[-1].get("duration_s") or 0.0)
    if len(durations) >= 3:
        typical = median_seconds(durations)
        if last_duration > 30.0 and typical > 0 and last_duration > 3 * typical:
            anomalies.append(
                f"LinkedIn Posts : durée anormale ({last_duration:.0f}s "
                f"contre une médiane de {typical:.0f}s)"
            )

    last_reports = rows[-1].get("query_reports") or []
    attempts = [
        attempt
        for report in last_reports if isinstance(report, Mapping)
        for attempt in (report.get("provider_attempts") or [])
        if isinstance(attempt, Mapping)
    ]
    if attempts and all(
        attempt.get("outcome") in {"blocked", "timeout", "error",
                                   "skipped_circuit_open"}
        for attempt in attempts
    ):
        anomalies.append(
            "LinkedIn Posts : provider principal et fallback tous deux "
            "indisponibles sur le dernier run"
        )
    return anomalies


__all__ = [
    "CLASSIFICATIONS",
    "LINKEDIN_POSTS_SOURCE",
    "METRICS_FILE",
    "MIN_RUNS_INDICATIVE",
    "MIN_RUNS_RELIABLE",
    "classify_linkedin_posts",
    "cost_per_new_offer_minutes",
    "detect_linkedin_posts_anomalies",
    "format_cost",
    "linkedin_posts_query_rows",
    "load_metrics_document",
    "median_seconds",
    "profile_runs",
    "source_rows",
    "summarize_linkedin_posts",
    "summarize_profile",
    "summarize_source",
]
