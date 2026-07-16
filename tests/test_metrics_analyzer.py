"""Tests purs des métriques par run, de l'analyseur et des scénarios budget."""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import radar_metrics
import scan
from budget import plan_monthly_scenarios, project_scenario
from radar_profiles import ScanContext
from scripts.analyze_scan_metrics import build_report, render_text


NOW = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)


def make_run(
    *,
    profile_id="fast_data_ia",
    run_at=NOW,
    duration_s=120.0,
    new_offer_count=1,
    sources=None,
    benchmark=False,
):
    return {
        "run_id": f"run-{run_at.isoformat()}",
        "profile_id": profile_id,
        "run_at": run_at.isoformat(timespec="seconds"),
        "duration_s": duration_s,
        "billable_minutes_estimate": 2,
        "new_offer_count": new_offer_count,
        "benchmark": benchmark,
        "sources": sources if sources is not None else [make_posts_status()],
    }


def make_posts_status(
    *,
    status="success",
    duration_s=10.0,
    raw_count=10,
    accepted_count=2,
    new_count=1,
    duplicate_count=1,
    unknown_date_count=1,
    blocked_count=0,
    network_request_count=3,
    query_count=3,
    query_reports=None,
):
    return {
        "source": "LinkedIn Posts",
        "status": status,
        "duration_s": duration_s,
        "raw_count": raw_count,
        "accepted_count": accepted_count,
        "new_count": new_count,
        "known_count": max(0, accepted_count - new_count),
        "duplicate_count": duplicate_count,
        "unknown_date_count": unknown_date_count,
        "blocked_count": blocked_count,
        "timeout_count": 0,
        "network_request_count": network_request_count,
        "query_count": query_count,
        "query_reports": query_reports or [],
    }


def document_with(runs):
    profiles: dict[str, list] = {}
    for run in runs:
        profiles.setdefault(run["profile_id"], []).append(run)
    return {"schema_version": 1, "profiles": profiles}


class TestRunMetricsRecord(unittest.TestCase):
    def setUp(self):
        self.context = ScanContext(scan.PROFILES["fast_data_ia"], NOW)

    def record(self, total_s=95.0):
        metrics = scan.RunMetrics()
        metrics.total_s = total_s
        return scan.build_metrics_record(
            self.context, [], metrics, new_offer_count=2, offer_count=5
        )

    def test_record_has_run_identity_and_billable_estimate(self):
        record = self.record(total_s=95.0)
        self.assertTrue(record["run_id"])
        self.assertEqual(record["started_at"], NOW.isoformat(timespec="seconds"))
        self.assertEqual(record["duration_s"], 95.0)
        finished = datetime.fromisoformat(record["finished_at"])
        self.assertEqual(finished - NOW, timedelta(seconds=95))
        # 95 s → 2 minutes facturables : GitHub arrondit à la minute supérieure.
        self.assertEqual(record["billable_minutes_estimate"], 2)

    def test_two_records_have_distinct_run_ids(self):
        self.assertNotEqual(self.record()["run_id"], self.record()["run_id"])

    def test_history_is_bounded_to_one_hundred_per_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scan_metrics.json"
            for index in range(105):
                record = self.record()
                record["index"] = index
                scan.save_metrics_record(record, path)
            payload = json.loads(path.read_text(encoding="utf-8"))
        history = payload["profiles"]["fast_data_ia"]
        self.assertEqual(len(history), 100)
        self.assertEqual(history[0]["index"], 5)
        self.assertEqual(history[-1]["index"], 104)


class TestAnalyzerAggregates(unittest.TestCase):
    def test_median_and_p95_from_history(self):
        runs = [
            make_run(run_at=NOW + timedelta(hours=index), duration_s=duration)
            for index, duration in enumerate((10, 20, 30, 40, 100))
        ]
        summary = radar_metrics.summarize_profile(runs)
        self.assertEqual(summary["duration"]["median_s"], 30)
        self.assertEqual(summary["duration"]["p95_s"], 100)
        self.assertEqual(summary["billable_minutes_p95"], 2)

    def test_cost_per_new_offer_and_safe_zero_division(self):
        self.assertAlmostEqual(
            radar_metrics.cost_per_new_offer_minutes(120.0, 4), 0.5
        )
        self.assertIsNone(radar_metrics.cost_per_new_offer_minutes(120.0, 0))
        self.assertEqual(radar_metrics.format_cost(None), "non calculable")

    def test_profile_filter_and_last_window(self):
        runs = [
            make_run(profile_id="fast_data_ia", run_at=NOW),
            make_run(profile_id="fast_data_ia", run_at=NOW + timedelta(hours=1)),
            make_run(profile_id="full_general", run_at=NOW + timedelta(hours=2)),
        ]
        document = document_with(runs)
        fast = radar_metrics.profile_runs(document, "fast_data_ia")
        self.assertEqual(len(fast), 2)
        self.assertEqual(
            len(radar_metrics.profile_runs(document, "fast_data_ia", last=1)), 1
        )
        self.assertEqual(len(radar_metrics.profile_runs(document)), 3)

    def test_benchmark_runs_are_excluded_by_default(self):
        document = document_with([
            make_run(run_at=NOW), make_run(run_at=NOW, benchmark=True),
        ])
        self.assertEqual(len(radar_metrics.profile_runs(document)), 1)

    def test_source_filter_via_report(self):
        document = document_with([make_run()])
        report = build_report(document, source="LinkedIn Posts")
        sources = report["profiles"]["fast_data_ia"]["sources"]
        self.assertEqual(list(sources), ["LinkedIn Posts"])
        rendered = render_text(report)
        self.assertIn("LinkedIn Posts", rendered)
        self.assertIn("Recommandation", rendered)

    def test_markdown_report_renders_a_table(self):
        document = document_with([make_run()])
        rendered = render_text(build_report(document), markdown=True)
        self.assertIn("| source |", rendered)
        self.assertIn("# Rendement du radar", rendered)


class TestLinkedInPostsClassification(unittest.TestCase):
    def runs(self, count, **status_kwargs):
        return [
            make_run(
                run_at=NOW + timedelta(hours=index),
                sources=[make_posts_status(**status_kwargs)],
            )
            for index in range(count)
        ]

    def test_below_minimum_runs_is_insufficient_data(self):
        summary = radar_metrics.summarize_linkedin_posts(self.runs(9))
        verdict = radar_metrics.classify_linkedin_posts(summary)
        self.assertEqual(verdict["classification"], "donnees_insuffisantes")

    def test_very_useful_recommends_every_fast(self):
        summary = radar_metrics.summarize_linkedin_posts(
            self.runs(12, new_count=1, duration_s=8.0, blocked_count=0)
        )
        verdict = radar_metrics.classify_linkedin_posts(summary)
        self.assertEqual(verdict["classification"], "tres_utile")
        self.assertIn("chaque FAST", verdict["label"])
        self.assertEqual(verdict["confidence"], "indicative")

    def test_twenty_runs_upgrade_confidence_to_reliable(self):
        summary = radar_metrics.summarize_linkedin_posts(
            self.runs(20, new_count=1, duration_s=8.0)
        )
        verdict = radar_metrics.classify_linkedin_posts(summary)
        self.assertEqual(verdict["confidence"], "fiable")

    def test_secondary_when_one_new_offer_every_five_to_ten_runs(self):
        runs = self.runs(10, new_count=0)
        runs[0]["sources"][0]["new_count"] = 1
        runs[5]["sources"][0]["new_count"] = 1
        summary = radar_metrics.summarize_linkedin_posts(runs)
        verdict = radar_metrics.classify_linkedin_posts(summary)
        self.assertEqual(verdict["classification"], "utile_secondaire")
        self.assertIn("2 ou 3 heures", verdict["label"])

    def test_low_yield_recommends_full_only(self):
        summary = radar_metrics.summarize_linkedin_posts(
            self.runs(15, new_count=0)
        )
        verdict = radar_metrics.classify_linkedin_posts(summary)
        self.assertEqual(verdict["classification"], "faible_rendement")
        self.assertIn("FULL", verdict["label"])

    def test_unstable_wins_over_yield(self):
        summary = radar_metrics.summarize_linkedin_posts(
            self.runs(12, new_count=1, blocked_count=2, network_request_count=3)
        )
        verdict = radar_metrics.classify_linkedin_posts(summary)
        self.assertEqual(verdict["classification"], "instable")
        self.assertIn("cooldown", verdict["label"])


class TestAnomalies(unittest.TestCase):
    def test_zero_results_alone_is_never_an_anomaly(self):
        runs = [
            make_run(sources=[make_posts_status(
                status="empty", raw_count=0, accepted_count=0, new_count=0,
                duplicate_count=0, unknown_date_count=0,
            )])
            for _ in range(5)
        ]
        self.assertEqual(
            radar_metrics.detect_linkedin_posts_anomalies(runs), []
        )

    def test_three_consecutive_errors_are_flagged(self):
        runs = [make_run(sources=[make_posts_status()]) for _ in range(2)]
        runs += [
            make_run(sources=[make_posts_status(status="error")])
            for _ in range(3)
        ]
        anomalies = radar_metrics.detect_linkedin_posts_anomalies(runs)
        self.assertTrue(any("3 runs consécutifs" in item for item in anomalies))

    def test_high_block_rate_is_flagged(self):
        runs = [
            make_run(sources=[make_posts_status(
                status="blocked", blocked_count=3, network_request_count=3,
            )])
            for _ in range(2)
        ]
        anomalies = radar_metrics.detect_linkedin_posts_anomalies(runs)
        self.assertTrue(any("blocage" in item for item in anomalies))

    def test_abnormal_duration_is_flagged(self):
        runs = [
            make_run(sources=[make_posts_status(duration_s=10.0)])
            for _ in range(4)
        ]
        current = make_posts_status(duration_s=120.0)
        anomalies = radar_metrics.detect_linkedin_posts_anomalies(
            runs, current_status=current
        )
        self.assertTrue(any("durée anormale" in item for item in anomalies))

    def test_both_providers_down_is_flagged(self):
        report = {
            "query": "site:linkedin.com/posts …",
            "provider_attempts": [
                {"provider": "duckduckgo_html", "outcome": "blocked"},
                {"provider": "duckduckgo_lite", "outcome": "blocked"},
            ],
            "status": "error",
        }
        runs = [make_run(sources=[make_posts_status(
            status="error", query_reports=[report],
        )])]
        anomalies = radar_metrics.detect_linkedin_posts_anomalies(runs)
        self.assertTrue(any("fallback" in item for item in anomalies))


class TestBudgetScenarios(unittest.TestCase):
    def test_scenarios_report_runs_minutes_and_margin_under_2950(self):
        scenarios = plan_monthly_scenarios(
            fast_base_p95_seconds=100.0,
            linkedin_posts_p95_seconds=30.0,
            full_p95_seconds=480.0,
        )
        self.assertEqual([item.scenario_id for item in scenarios],
                         ["A", "B", "C"])
        for item in scenarios:
            self.assertEqual(item.plannable_minutes, 2950)
            self.assertEqual(
                item.planned_total_minutes,
                item.fast_minutes + item.linkedin_posts_extra_minutes
                + item.full_minutes + item.other_workflows_minutes,
            )
            self.assertEqual(
                item.within_plannable_budget,
                item.margin_after_reserve_minutes >= 0,
            )
        economical = scenarios[2]
        self.assertTrue(economical.within_plannable_budget)
        self.assertLess(economical.planned_total_minutes,
                        scenarios[0].planned_total_minutes)

    def test_linkedin_posts_extra_minutes_are_isolated(self):
        projection = project_scenario(
            scenario_id="T", label="test", description="",
            fast_base_p95_seconds=50.0,
            linkedin_posts_p95_seconds=30.0,
            full_p95_seconds=300.0,
            fast_runs_per_workday=1,
            fast_runs_per_weekend_day=0,
            full_runs_per_month=0,
            linkedin_posts_share=1.0,
            workdays_per_month=10,
            weekend_days_per_month=0,
        )
        # 50 s = 1 min facturable, 80 s = 2 min : le surcoût X-ray est
        # explicitement porté par linkedin_posts_extra_minutes.
        self.assertEqual(projection.fast_runs, 10)
        self.assertEqual(projection.fast_minutes, 10)
        self.assertEqual(projection.linkedin_posts_extra_minutes, 10)
        self.assertEqual(projection.planned_total_minutes, 20)

    def test_overrun_is_reported_when_over_2950(self):
        projection = project_scenario(
            scenario_id="X", label="hors budget", description="",
            fast_base_p95_seconds=600.0,
            linkedin_posts_p95_seconds=0.0,
            full_p95_seconds=600.0,
            fast_runs_per_workday=20,
            fast_runs_per_weekend_day=10,
            full_runs_per_month=30,
            linkedin_posts_share=0.0,
        )
        self.assertFalse(projection.within_plannable_budget)
        self.assertGreater(projection.overrun_minutes, 0)
        self.assertEqual(
            projection.overrun_minutes,
            projection.planned_total_minutes - 2950,
        )

    def test_scenario_report_from_measured_history(self):
        runs = [
            make_run(profile_id="fast_data_ia",
                     run_at=NOW + timedelta(hours=index), duration_s=120.0)
            for index in range(3)
        ] + [
            make_run(profile_id="full_general",
                     run_at=NOW + timedelta(hours=index), duration_s=600.0)
            for index in range(3)
        ]
        report = build_report(document_with(runs), scenarios=True)
        projections = report["scenarios"]["projections"]
        self.assertEqual(len(projections), 3)
        rendered = render_text(report)
        self.assertIn("Scénarios mensuels", rendered)
        self.assertIn("Scénario A", rendered)


if __name__ == "__main__":
    unittest.main()
