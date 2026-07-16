"""Tests hors réseau de l'orchestration Actions et des outils de mesure."""

import json
import tempfile
import unittest
from pathlib import Path

from scripts.benchmark_profiles import benchmark_command
from scripts.capture_scan_baseline import aggregate_status


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/scan.yml"
BASELINE = ROOT / "benchmarks/baseline-legacy-full-2026-07-15.json"
FAST_LIVE = ROOT / "benchmarks/fast-data-ia-pre-optimization-live-2026-07-16.json"
XRAY_EVALUATION = ROOT / "benchmarks/xray-provider-evaluation-2026-07-16.json"


class TestWorkflowArchitecture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = WORKFLOW.read_text(encoding="utf-8")

    def test_profiles_share_one_non_cancelling_store_lock(self):
        self.assertEqual(len(list((ROOT / ".github/workflows").glob("*"))), 1)
        self.assertIn("group: radar-stage-store", self.workflow)
        self.assertIn("cancel-in-progress: false", self.workflow)
        self.assertNotIn("matrix:", self.workflow)

    def test_manual_dispatch_exposes_both_profiles(self):
        self.assertIn("workflow_dispatch:", self.workflow)
        self.assertIn("default: full_general", self.workflow)
        self.assertIn("- fast_data_ia", self.workflow)
        self.assertIn("- full_general", self.workflow)
        self.assertIn('python radar.py --profile "$RADAR_PROFILE"', self.workflow)

    def test_only_historical_schedule_remains(self):
        self.assertEqual(self.workflow.count("cron:"), 1)
        self.assertIn('cron: "0 * * * *"', self.workflow)

    def test_all_shared_generated_artifacts_are_committed(self):
        for path in (
            "data/offers.json",
            "data/seen_offers.json",
            "data/source_status.json",
            "data/source_status_fast_data_ia.json",
            "data/source_status_full_general.json",
            "data/scan_metrics.json",
            "data/xray_state.json",
            "docs/index.html",
        ):
            self.assertIn(path, self.workflow)


class TestBenchmarkTools(unittest.TestCase):
    def test_benchmark_command_is_profiled_and_isolated(self):
        command = benchmark_command(
            python="python",
            radar=Path("radar.py"),
            profile_id="fast_data_ia",
            output=Path("/tmp/fast.json"),
        )
        self.assertEqual(command[:2], ["python", "radar.py"])
        self.assertIn("--profile", command)
        self.assertIn("fast_data_ia", command)
        self.assertIn("--benchmark", command)
        self.assertEqual(command[-2:], ["--benchmark-output", "/tmp/fast.json"])

    def test_baseline_aggregation_keeps_machine_metrics_and_sizes(self):
        payload = {
            "generated_at": "2026-01-01T00:00:00+00:00",
            "scan_duration_s": 12.5,
            "active_domains": ["data_ia"],
            "active_regions": ["france"],
            "query_count": 2,
            "sources": [
                {
                    "source": "Indeed",
                    "duration_s": 5,
                    "network_request_count": 2,
                    "pages_fetched": 2,
                    "raw_count": 8,
                    "accepted_count": 3,
                    "rejected_count": 5,
                    "duplicate_count": 1,
                    "timeout_count": 0,
                    "blocked_count": 0,
                    "detail_request_count": 0,
                    "http_error": "",
                }
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            offers = root / "offers.json"
            seen = root / "seen.json"
            offers.write_bytes(b"1234")
            seen.write_bytes(b"12")
            baseline = aggregate_status(payload, offers=offers, seen=seen)
        self.assertEqual(baseline["duration_total_s"], 12.5)
        self.assertEqual(baseline["totals"]["network_request_count"], 2)
        self.assertEqual(baseline["totals"]["pages_fetched"], 2)
        self.assertEqual(baseline["totals"]["accepted_count"], 3)
        self.assertEqual(baseline["artifacts"]["offers_json_bytes"], 4)
        self.assertEqual(baseline["artifacts"]["seen_offers_json_bytes"], 2)

    def test_versioned_legacy_baseline_is_explicitly_reconstructed(self):
        baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
        self.assertEqual(
            baseline["measurement_type"], "reconstructed_from_status"
        )
        self.assertEqual(baseline["duration_total_s"], 3068.6)
        self.assertEqual(baseline["domain_count"], 16)
        self.assertEqual(baseline["region_count"], 4)
        self.assertEqual(baseline["source_count"], 7)
        self.assertEqual(baseline["query_count"], 82)
        self.assertEqual(baseline["totals"]["pages_fetched"], 1156)
        self.assertEqual(baseline["totals"]["raw_count"], 6345)
        self.assertEqual(baseline["totals"]["accepted_count"], 170)
        self.assertIsNone(baseline["totals"]["network_request_count"])

    def test_live_fast_report_has_uniform_source_counters(self):
        report = json.loads(FAST_LIVE.read_text(encoding="utf-8"))
        self.assertEqual(report["measurement_type"], "live_network_benchmark")
        self.assertFalse(report["production_mutated"])
        required = {
            "network_request_count", "pages_fetched", "raw_count",
            "accepted_count", "rejected_count", "duplicate_count",
            "timeout_count", "blocked_count", "detail_request_count",
        }
        for source in report["sources"]:
            self.assertTrue(required.issubset(source), source["source"])

    def test_xray_provider_evaluation_never_claims_unrun_ddgs_success(self):
        report = json.loads(XRAY_EVALUATION.read_text(encoding="utf-8"))
        by_provider = {row["provider"]: row for row in report["providers"]}
        self.assertEqual(report["selected_provider"], "duckduckgo_html")
        self.assertEqual(by_provider["duckduckgo_html"]["http_status"], 202)
        self.assertEqual(by_provider["ddgs"]["network_test"], "not_executed")
        self.assertEqual(by_provider["ddgs"]["result"], "not_measured")
        self.assertEqual(report["safety"]["direct_linkedin_requests"], 0)


if __name__ == "__main__":
    unittest.main()
