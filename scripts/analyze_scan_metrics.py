#!/usr/bin/env python3
"""Analyse hors ligne des métriques de scan — aucun scan, aucun réseau.

Exemples :
    python scripts/analyze_scan_metrics.py
    python scripts/analyze_scan_metrics.py --profile fast_data_ia
    python scripts/analyze_scan_metrics.py --source "LinkedIn Posts"
    python scripts/analyze_scan_metrics.py --last 20 --format markdown
    python scripts/analyze_scan_metrics.py --format json --scenarios
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import radar_metrics
from budget import plan_monthly_scenarios


def _fmt_seconds(value: float | None) -> str:
    return "—" if value is None else f"{float(value):.1f}s"


def _fmt_rate(value: float | None) -> str:
    return "—" if value is None else f"{float(value):.1%}"


def build_report(
    document: dict,
    *,
    profile: str | None = None,
    source: str | None = None,
    last: int | None = None,
    min_runs: int = radar_metrics.MIN_RUNS_INDICATIVE,
    scenarios: bool = False,
) -> dict:
    """Rapport structuré unique, sérialisable en JSON ou rendu en texte."""
    report: dict = {"filters": {
        "profile": profile, "source": source, "last": last,
    }}

    profile_ids = [profile] if profile else sorted({
        run.get("profile_id")
        for run in radar_metrics.profile_runs(document)
        if run.get("profile_id")
    })
    report["profiles"] = {}
    for profile_id in profile_ids:
        runs = radar_metrics.profile_runs(document, profile_id, last=last)
        summary = radar_metrics.summarize_profile(runs)
        sources: dict[str, dict] = {}
        source_names = [source] if source else sorted({
            str(row.get("source"))
            for run in runs for row in run.get("sources", []) or []
            if isinstance(row, dict) and row.get("source")
        })
        for name in source_names:
            row_summary = radar_metrics.summarize_source(runs, name)
            row_summary.pop("query_rows", None)
            sources[name] = row_summary
        report["profiles"][profile_id] = {
            "summary": summary,
            "sources": sources,
        }

    posts_runs = radar_metrics.profile_runs(document, profile, last=last)
    posts = radar_metrics.summarize_linkedin_posts(posts_runs)
    verdict = radar_metrics.classify_linkedin_posts(posts, min_runs=min_runs)
    report["linkedin_posts"] = {"summary": posts, "classification": verdict}

    if scenarios:
        fast = radar_metrics.summarize_profile(
            radar_metrics.profile_runs(document, "fast_data_ia", last=last)
        )
        full = radar_metrics.summarize_profile(
            radar_metrics.profile_runs(document, "full_general", last=last)
        )
        fast_p95 = fast["duration"]["p95_s"]
        full_p95 = full["duration"]["p95_s"]
        posts_p95 = posts["duration"]["p95_s"] or 0.0
        if fast_p95 is None or full_p95 is None:
            report["scenarios"] = {
                "error": (
                    "p95 FAST et FULL indisponibles : lancer d'abord au moins "
                    "un run de chaque profil pour projeter les scénarios."
                )
            }
        else:
            fast_base = max(0.0, float(fast_p95) - float(posts_p95))
            report["scenarios"] = {
                "inputs": {
                    "fast_p95_s": fast_p95,
                    "full_p95_s": full_p95,
                    "linkedin_posts_p95_s": posts_p95,
                    "fast_base_p95_s": round(fast_base, 3),
                },
                "projections": [
                    {
                        "scenario": item.scenario_id,
                        "label": item.label,
                        "description": item.description,
                        "fast_runs": item.fast_runs,
                        "full_runs": item.full_runs,
                        "fast_runs_with_linkedin_posts":
                            item.fast_runs_with_linkedin_posts,
                        "fast_minutes": item.fast_minutes,
                        "linkedin_posts_extra_minutes":
                            item.linkedin_posts_extra_minutes,
                        "full_minutes": item.full_minutes,
                        "other_workflows_minutes": item.other_workflows_minutes,
                        "planned_total_minutes": item.planned_total_minutes,
                        "plannable_minutes": item.plannable_minutes,
                        "margin_after_reserve_minutes":
                            item.margin_after_reserve_minutes,
                        "overrun_minutes": item.overrun_minutes,
                        "within_plannable_budget": item.within_plannable_budget,
                    }
                    for item in plan_monthly_scenarios(
                        fast_base_p95_seconds=fast_base,
                        linkedin_posts_p95_seconds=float(posts_p95),
                        full_p95_seconds=float(full_p95),
                    )
                ],
            }
    return report


def _render_source_table(sources: dict[str, dict], markdown: bool) -> list[str]:
    header = ("source", "runs", "durée totale", "p95", "bruts", "acceptés",
              "nouveaux", "doublons", "blocages", "coût/nouvelle")
    rows = [header] if markdown else []
    lines: list[str] = []
    for name, row in sources.items():
        values = (
            name, str(row["run_count"]),
            _fmt_seconds(row["duration"]["total_s"]),
            _fmt_seconds(row["duration"]["p95_s"]),
            str(row["raw_count"]), str(row["accepted_count"]),
            str(row["new_count"]), str(row["duplicate_count"]),
            str(row["blocked_count"]),
            radar_metrics.format_cost(row["cost_per_new_offer_min"]),
        )
        if markdown:
            rows.append(values)
        else:
            lines.append(
                f"  - {name}: runs={values[1]} durée={values[2]} p95={values[3]} "
                f"bruts={values[4]} acceptés={values[5]} nouveaux={values[6]} "
                f"doublons={values[7]} blocages={values[8]} coût={values[9]}"
            )
    if markdown:
        lines.append("| " + " | ".join(header) + " |")
        lines.append("|" + "---|" * len(header))
        for values in rows[1:]:
            lines.append("| " + " | ".join(values) + " |")
    return lines


def render_text(report: dict, *, markdown: bool = False) -> str:
    lines: list[str] = []
    title = "# Rendement du radar" if markdown else "=== Rendement du radar ==="
    lines.append(title)
    for profile_id, block in report["profiles"].items():
        summary = block["summary"]
        heading = f"## Profil {profile_id}" if markdown else f"\nProfil {profile_id}"
        lines.append(heading)
        lines.append(
            f"Runs : {summary['run_count']} · dernier : {summary['last_run_at'] or '—'} · "
            f"durée médiane : {_fmt_seconds(summary['duration']['median_s'])} · "
            f"p95 : {_fmt_seconds(summary['duration']['p95_s'])} · "
            f"nouvelles offres : {summary['new_offer_count']} · "
            f"coût : {radar_metrics.format_cost(summary['cost_per_new_offer_min'])}"
        )
        lines.extend(_render_source_table(block["sources"], markdown))

    posts = report["linkedin_posts"]["summary"]
    verdict = report["linkedin_posts"]["classification"]
    heading = "## LinkedIn Posts" if markdown else "\nLinkedIn Posts"
    lines.append(heading)
    lines.append(
        f"Runs : {posts['run_count']} · requêtes : {posts['executed_query_count']} · "
        f"providers : {', '.join(posts['providers']) or '—'}"
    )
    lines.append(
        f"Durées : total {_fmt_seconds(posts['duration']['total_s'])} · "
        f"moyenne {_fmt_seconds(posts['duration']['mean_s'])} · "
        f"médiane {_fmt_seconds(posts['duration']['median_s'])} · "
        f"p95 {_fmt_seconds(posts['duration']['p95_s'])}"
    )
    lines.append(
        f"Volumes : bruts {posts['raw_count']} · acceptés {posts['accepted_count']} · "
        f"réellement nouveaux {posts['new_count']}"
    )
    lines.append(
        f"Taux : acceptation {_fmt_rate(posts['acceptance_rate'])} · "
        f"nouveauté {_fmt_rate(posts['novelty_rate'])} · "
        f"doublons {_fmt_rate(posts['duplicate_rate'])} · "
        f"dates inconnues {_fmt_rate(posts['unknown_date_rate'])} · "
        f"blocage {_fmt_rate(posts['block_rate'])}"
    )
    lines.append(
        "Coût par nouvelle publication : "
        + radar_metrics.format_cost(posts["cost_per_new_offer_min"])
    )
    lines.append(
        f"Classification : {verdict['label']} (confiance {verdict['confidence']})"
    )
    lines.append(f"Recommandation : {verdict['recommendation']}")

    scenarios = report.get("scenarios")
    if scenarios:
        heading = "## Scénarios mensuels" if markdown else "\nScénarios mensuels"
        lines.append(heading)
        if "error" in scenarios:
            lines.append(scenarios["error"])
        else:
            inputs = scenarios["inputs"]
            lines.append(
                f"Entrées : p95 FAST {_fmt_seconds(inputs['fast_p95_s'])} · "
                f"p95 FULL {_fmt_seconds(inputs['full_p95_s'])} · "
                f"p95 LinkedIn Posts {_fmt_seconds(inputs['linkedin_posts_p95_s'])}"
            )
            for item in scenarios["projections"]:
                verdict_label = ("OK" if item["within_plannable_budget"]
                                 else f"DÉPASSEMENT {item['overrun_minutes']} min")
                lines.append(
                    f"  - Scénario {item['scenario']} ({item['label']}) : "
                    f"{item['fast_runs']} FAST + {item['full_runs']} FULL = "
                    f"{item['planned_total_minutes']} min "
                    f"(FAST {item['fast_minutes']} · LinkedIn Posts "
                    f"+{item['linkedin_posts_extra_minutes']} · FULL "
                    f"{item['full_minutes']}) · marge "
                    f"{item['margin_after_reserve_minutes']} min / "
                    f"{item['plannable_minutes']} → {verdict_label}"
                )
                lines.append(f"    {item['description']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Analyse hors ligne de data/scan_metrics.json (aucun scan)."
    )
    parser.add_argument("--metrics-file", type=Path,
                        default=radar_metrics.METRICS_FILE)
    parser.add_argument("--profile", default=None,
                        help="Limiter au profil (ex. fast_data_ia)")
    parser.add_argument("--source", default=None,
                        help="Limiter à une source (ex. \"LinkedIn Posts\")")
    parser.add_argument("--last", type=int, default=None,
                        help="Ne garder que les N derniers runs")
    parser.add_argument("--format", choices=("text", "markdown", "json"),
                        default="text")
    parser.add_argument("--min-runs", type=int,
                        default=radar_metrics.MIN_RUNS_INDICATIVE,
                        help="Runs minimaux avant une première recommandation")
    parser.add_argument("--scenarios", action="store_true",
                        help="Projeter les scénarios mensuels A/B/C")
    args = parser.parse_args(argv)

    document = radar_metrics.load_metrics_document(args.metrics_file)
    report = build_report(
        document,
        profile=args.profile,
        source=args.source,
        last=args.last,
        min_runs=args.min_runs,
        scenarios=args.scenarios,
    )
    if args.format == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_text(report, markdown=args.format == "markdown"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
