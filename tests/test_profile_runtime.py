"""Tests d'intégration sans réseau du moteur FAST/FULL."""

import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import scan
from radar_profiles import ScanContext
from radar_xray import SearchResult, XRayBlockedError, XRayRotationState


NOW = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)


class TestProfileFreshness(unittest.TestCase):
    def make(self, context, published):
        return scan.make_offer(
            "Stage Data Engineer",
            "ACME",
            "Paris",
            f"https://example.com/{published}",
            "Indeed",
            published.isoformat() if isinstance(published, datetime) else published,
            domains={"data_ia": scan.DOMAINS["data_ia"]},
            active_ids=["data_ia"],
            context=context,
        )

    def test_fast_accepts_1h59_and_rejects_2h01(self):
        context = ScanContext(scan.PROFILES["fast_data_ia"], NOW)
        accepted, reason = self.make(context, NOW - timedelta(hours=1, minutes=59))
        self.assertIsNotNone(accepted)
        self.assertIsNone(reason)
        rejected, reason = self.make(context, NOW - timedelta(hours=2, minutes=1))
        self.assertIsNone(rejected)
        self.assertEqual(reason, "plus de 2 heures")

    def test_full_accepts_23h_and_rejects_more_than_24h(self):
        context = ScanContext(scan.PROFILES["full_general"], NOW)
        accepted, _ = self.make(context, NOW - timedelta(hours=23))
        rejected, reason = self.make(context, NOW - timedelta(hours=24, seconds=1))
        self.assertIsNotNone(accepted)
        self.assertIsNone(rejected)
        self.assertEqual(reason, "plus de 24 heures")

    def test_unknown_date_is_kept_without_inventing_publication(self):
        context = ScanContext(scan.PROFILES["fast_data_ia"], NOW)
        with mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
            candidate, reason = self.make(context, "date illisible")
        self.assertIsNone(reason)
        self.assertFalse(candidate["date_known"])
        self.assertEqual(candidate["date_status"], "unknown")
        self.assertEqual(candidate["date"], "")
        self.assertIn("fraîcheur 2 h non vérifiable", stderr.getvalue())


class TestExplainableScoring(unittest.TestCase):
    def test_exact_matched_keywords_follow_title_then_description_contribution(self):
        domains = {
            "data": {
                "label": "Data", "group": "Test", "enabled": True,
                "queries": ["stage data"],
                "positive": ["python", "spark", "etl"],
                "negative": ["marketing"],
            }
        }
        matched, scores, keywords = scan.score_offer_domains_explained(
            "Stage Spark", "Python ETL Spark marketing",
            domains=domains, active_ids=["data"],
        )
        self.assertEqual(matched, ["data"])
        self.assertEqual(scores["data"], 5)
        self.assertEqual(keywords["data"], ["spark", "python", "etl"])


class EmptyProvider:
    name = "fixture_empty"

    def __init__(self):
        self.calls = []

    def search(self, query, freshness=None, **kwargs):
        self.calls.append((query, freshness, kwargs))
        return []


class BlockedProvider:
    name = "fixture_blocked"

    def search(self, *_args, **_kwargs):
        raise XRayBlockedError("challenge", status_code=202)


class DuplicateProvider:
    name = "fixture_duplicate"

    def __init__(self):
        self.calls = 0

    def search(self, *_args, **_kwargs):
        self.calls += 1
        return [SearchResult(
            title="Alice Martin sur LinkedIn : Stage Data Engineer",
            snippet="Nous recherchons un stagiaire Data Engineer. Envoyez votre CV.",
            url=("https://fr.linkedin.com/posts/alice_stage-data-engineer-"
                 "activity-7351000000000000000?utm_source=share"),
            author="Alice Martin",
            published_at=NOW - timedelta(minutes=30),
            provider=self.name,
        )]


class TestLinkedInPostsRuntime(unittest.TestCase):
    def setUp(self):
        self.context = ScanContext(scan.PROFILES["fast_data_ia"], NOW)

    def test_zero_results_is_empty_and_budget_is_three_total(self):
        provider = EmptyProvider()
        offers, status, _ = scan.fetch_linkedin_posts(
            context=self.context,
            provider=provider,
            rotation_state=XRayRotationState(),
        )
        self.assertEqual(offers, [])
        self.assertEqual(status.status, "empty")
        self.assertEqual(status.query_count, 3)
        self.assertEqual(len(provider.calls), 3)
        self.assertTrue(all(call[1] == "d" for call in provider.calls))

    def test_blocking_is_isolated_and_reported(self):
        offers, status, _ = scan.fetch_linkedin_posts(
            context=self.context,
            provider=BlockedProvider(),
        )
        self.assertEqual(offers, [])
        self.assertEqual(status.status, "blocked")
        self.assertEqual(status.blocked_count, scan.MAX_CONSECUTIVE_BLOCKS)
        self.assertFalse(status.coverage_complete)

    def test_same_post_from_three_queries_is_one_contact_direct_offer(self):
        provider = DuplicateProvider()
        offers, status, _ = scan.fetch_linkedin_posts(
            context=self.context,
            provider=provider,
        )
        self.assertEqual(len(offers), 1)
        self.assertTrue(offers[0]["contact_direct"])
        self.assertEqual(offers[0]["company"], "Alice Martin")
        self.assertEqual(status.accepted_count, 1)
        self.assertEqual(status.duplicate_count, 2)
        self.assertEqual(len(status.query_reports), 3)


class TestDeduplicationAndDashboard(unittest.TestCase):
    def test_tracking_url_is_same_exact_identity(self):
        first, _ = scan.make_offer(
            "Stage Data Engineer", "ACME", "Paris",
            "https://example.com/jobs/42?utm_source=a&ref=x", "Indeed",
            NOW.isoformat(), active_ids=["data_ia"], now=NOW,
        )
        second, _ = scan.make_offer(
            "Stage Data Engineer", "ACME", "Paris",
            "https://example.com/jobs/42?utm_campaign=b", "Indeed",
            NOW.isoformat(), active_ids=["data_ia"], now=NOW,
        )
        offers = {}
        status = scan.SourceStatus(source="Indeed")
        scan.register_offer(status, offers, first, None)
        scan.register_offer(status, offers, second, None)
        self.assertEqual(len(offers), 1)
        self.assertEqual(status.duplicate_count, 1)
        self.assertEqual(scan.canonicalize_url("https://example.com:bad/job"), "")

    def test_cross_source_probable_duplicate_keeps_both_sources(self):
        first, _ = scan.make_offer(
            "Stage Data Engineer Confirmé", "ACME France", "Paris 8e",
            "https://one.example/1", "LinkedIn", NOW.isoformat(),
            active_ids=["data_ia"], now=NOW,
        )
        second, _ = scan.make_offer(
            "Stage Data Engineer Confirmé", "ACME France", "Paris 8e",
            "https://two.example/2", "Indeed", NOW.isoformat(),
            active_ids=["data_ia"], now=NOW,
        )
        offers = {}
        status = scan.SourceStatus(source="test")
        scan.register_offer(status, offers, first, None)
        scan.register_offer(status, offers, second, None)
        merged = next(iter(offers.values()))
        self.assertEqual(set(merged["sources"]), {"LinkedIn", "Indeed"})
        self.assertEqual(set(merged["source_urls"]), {"LinkedIn", "Indeed"})

    def test_two_generic_stages_are_not_merged_probabilistically(self):
        first, _ = scan.make_offer(
            "Stage Assistant", "ACME France", "Paris 8e",
            "https://one.example/assistant-1", "LinkedIn", NOW.isoformat(),
            confirmed_internship=True,
            description="Missions data engineer et ETL pour une première équipe.",
            active_ids=["data_ia"], now=NOW,
        )
        second, _ = scan.make_offer(
            "Stage Assistant", "ACME France", "Paris 8e",
            "https://two.example/assistant-2", "Indeed", NOW.isoformat(),
            confirmed_internship=True,
            description="Missions data engineer et ETL pour une seconde équipe.",
            active_ids=["data_ia"], now=NOW,
        )
        self.assertEqual(first["probable_fingerprint"], "")
        self.assertEqual(second["probable_fingerprint"], "")
        offers = {}
        status = scan.SourceStatus(source="test")
        scan.register_offer(status, offers, first, None)
        scan.register_offer(status, offers, second, None)
        self.assertEqual(len(offers), 2)
        self.assertEqual(status.duplicate_count, 0)

    def test_stable_native_id_merges_even_when_urls_change(self):
        first, _ = scan.make_offer(
            "Stage Data Engineer", "ACME", "Paris",
            "https://candidat.francetravail.fr/offres/recherche/detail/FT-42",
            "France Travail", NOW.isoformat(), confirmed_internship=True,
            active_ids=["data_ia"], now=NOW, external_id="FT-42",
        )
        second, _ = scan.make_offer(
            "Stage Data Engineer", "ACME", "Paris",
            "https://emploi.example/offre/renouvellee",
            "France Travail", NOW.isoformat(), confirmed_internship=True,
            active_ids=["data_ia"], now=NOW, external_id="FT-42",
        )
        self.assertIn("native:france travail:ft-42", first["identity_keys"])
        offers = {}
        status = scan.SourceStatus(source="France Travail")
        scan.register_offer(status, offers, first, None)
        scan.register_offer(status, offers, second, None)
        self.assertEqual(len(offers), 1)
        self.assertEqual(status.duplicate_count, 1)

    def test_dashboard_shows_two_profiles_keywords_and_new_badges(self):
        candidate, _ = scan.make_offer(
            "Stage Data Engineer Python Spark ETL SQL Machine Learning",
            "ACME", "Paris",
            "https://linkedin.com/posts/acme_stage-data-activity-7351000000000000001",
            "LinkedIn Posts",
            NOW.isoformat(), confirmed_internship=True,
            description="Nous recrutons un stagiaire data science power bi.",
            active_ids=["data_ia"], now=NOW, contact_direct=True,
        )
        candidate["matched_keywords"]["data_ia"] = [
            "python", "spark", "etl", "sql", "machine learning", "power bi"
        ]
        expired = dict(candidate)
        expired["id"] = "expired"
        expired["likely_expired"] = True
        payloads = {
            "fast_data_ia": {
                "generated_at": NOW.isoformat(), "scan_duration_s": 12,
                "new_offer_count": 1, "window_hours": 2, "sources": [],
            },
            "full_general": {
                "generated_at": NOW.isoformat(), "scan_duration_s": 120,
                "new_offer_count": 4, "window_hours": 24, "sources": [],
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "index.html"
            scan.render_dashboard(
                [candidate], {candidate["id"]}, expired_offers=[expired],
                profile_status_payloads=payloads,
                context=ScanContext(scan.PROFILES["fast_data_ia"], NOW),
                output_path=output,
            )
            page = output.read_text(encoding="utf-8")
        self.assertIn("Dernier FAST Data/IA", page)
        self.assertIn("Dernier FULL général", page)
        self.assertIn("contact direct", page)
        self.assertIn("Probablement pourvue", page)
        self.assertIn('id="sortSelect"', page)
        self.assertIn("Par pertinence", page)
        self.assertIn("match :</span> python · spark · etl · sql · machine learning", page)
        self.assertNotIn("machine learning · power bi", page)

    def test_profile_summary_does_not_call_partial_source_operational(self):
        payloads = {
            "fast_data_ia": {
                "generated_at": NOW.isoformat(), "scan_duration_s": 1,
                "new_offer_count": 0, "window_hours": 2,
                "sources": [{
                    "source": "LinkedIn", "status": "success",
                    "coverage_complete": False, "failed_queries": 0,
                    "timeout_count": 0, "blocked_count": 0,
                }],
            },
            "full_general": {},
        }
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "index.html"
            scan.render_dashboard(
                [], set(), profile_status_payloads=payloads,
                context=ScanContext(scan.PROFILES["fast_data_ia"], NOW),
                output_path=output,
            )
            page = output.read_text(encoding="utf-8")
        fast_card = page.split('data-profile="fast_data_ia"', 1)[1].split(
            "</article>", 1
        )[0]
        self.assertIn("<strong>0/6</strong>", fast_card)


class TestBenchmarkIsolationAndMetrics(unittest.TestCase):
    def test_benchmark_writes_only_explicit_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            files = {
                "offers": root / "offers.json",
                "seen": root / "seen.json",
                "status": root / "status.json",
                "metrics": root / "metrics.json",
                "xray": root / "xray.json",
                "dashboard": root / "index.html",
            }
            files["offers"].write_text("{}\n", encoding="utf-8")
            files["seen"].write_text("[]\n", encoding="utf-8")
            files["xray"].write_text("{}\n", encoding="utf-8")
            originals = {name: path.read_bytes() for name, path in files.items()
                         if path.exists()}
            report = root / "benchmark.json"
            with mock.patch.object(scan, "OFFERS_FILE", files["offers"]), \
                 mock.patch.object(scan, "SEEN_FILE", files["seen"]), \
                 mock.patch.object(scan, "STATUS_FILE", files["status"]), \
                 mock.patch.object(scan, "METRICS_FILE", files["metrics"]), \
                 mock.patch.object(scan, "XRAY_STATE_FILE", files["xray"]), \
                 mock.patch.object(scan, "DOCS_INDEX", files["dashboard"]), \
                 mock.patch.object(scan, "PROFILE_STATUS_FILES", {}), \
                 mock.patch.object(scan, "run_all_sources", return_value=({}, [])), \
                 mock.patch("sys.stdout", new_callable=io.StringIO):
                result = scan.main([
                    "--profile", "fast_data_ia", "--benchmark",
                    "--benchmark-output", str(report),
                ])
            self.assertEqual(result, 0)
            self.assertTrue(report.exists())
            self.assertFalse(files["status"].exists())
            self.assertFalse(files["metrics"].exists())
            self.assertFalse(files["dashboard"].exists())
            for name, content in originals.items():
                self.assertEqual(files[name].read_bytes(), content)
            self.assertFalse(json.loads(report.read_text())["production_mutated"])

    def test_metrics_history_is_bounded_per_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "metrics.json"
            for index in range(55):
                scan.save_metrics_record({
                    "profile_id": "fast_data_ia", "run_at": str(index)
                }, path=path, history_limit=50)
            payload = json.loads(path.read_text(encoding="utf-8"))
            history = payload["profiles"]["fast_data_ia"]
            self.assertEqual(len(history), 50)
            self.assertEqual(history[0]["run_at"], "5")

    def test_legacy_status_migrates_to_full_without_retired_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "source_status.json"
            legacy.write_text(json.dumps({
                "generated_at": NOW.isoformat(),
                "sources": [
                    {"source": "LinkedIn", "status": "success"},
                    {"source": "Glassdoor", "status": "blocked"},
                ],
            }), encoding="utf-8")
            with mock.patch.object(scan, "STATUS_FILE", legacy), \
                 mock.patch.object(scan, "PROFILE_STATUS_FILES", {
                     "fast_data_ia": root / "fast.json",
                     "full_general": root / "full.json",
                 }):
                payloads = scan.load_profile_status_payloads()
        self.assertNotIn("fast_data_ia", payloads)
        self.assertEqual(payloads["full_general"]["profile_id"], "full_general")
        self.assertEqual(
            [row["source"] for row in payloads["full_general"]["sources"]],
            ["LinkedIn"],
        )


class TestOfficialApiEdges(unittest.TestCase):
    def test_france_travail_204_is_a_healthy_empty_response(self):
        response = mock.Mock(status_code=204, headers={})
        context = ScanContext(scan.PROFILES["fast_data_ia"], NOW)
        with mock.patch.dict(os.environ, {
                "FT_CLIENT_ID": "id", "FT_CLIENT_SECRET": "secret"}), \
             mock.patch.object(scan, "ft_token", return_value="token"), \
             mock.patch.object(scan.requests, "get", return_value=response):
            offers, status = scan.fetch_france_travail_all(context=context)
        self.assertEqual(offers, [])
        self.assertEqual(status.status, "empty")
        self.assertEqual(status.completed_queries, status.query_count)


if __name__ == "__main__":
    unittest.main()
