"""Tests d'observabilité runtime : nouveauté réelle, X-ray détaillé, profils."""

import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import radar_store
import scan
from radar_profiles import ScanContext
from radar_xray import (
    FallbackXRayProvider,
    SearchResult,
    XRayBlockedError,
    XRayRotationState,
)


NOW = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)


def post_result(activity="7351000000000000001", *, author="Alice Martin",
                published_at=NOW - timedelta(minutes=30), provider="fixture"):
    return SearchResult(
        title=f"{author} sur LinkedIn : Stage Data Engineer",
        snippet="Nous recherchons un stagiaire data engineer. Envoyez votre CV.",
        url=f"https://www.linkedin.com/posts/x_stage-activity-{activity}",
        author=author,
        published_at=published_at,
        provider=provider,
    )


class TwoPostsProvider:
    name = "fixture_two_posts"

    def search(self, query, freshness=None, **kwargs):
        return [
            post_result("7351000000000000001"),
            post_result("7351000000000000002", author="Paul Durand",
                        published_at=NOW - timedelta(hours=2)),
        ]


class TestLinkedInPostsQueryReports(unittest.TestCase):
    def setUp(self):
        self.context = ScanContext(scan.PROFILES["fast_data_ia"], NOW)

    def test_per_query_report_details_and_discovery_fields(self):
        known = post_result("7351000000000000002",
                            published_at=NOW - timedelta(hours=2))
        known_offer, _ = scan.make_offer(
            known.title, "Paul Durand", "France", known.url, "LinkedIn Posts",
            known.published_at.isoformat(), confirmed_internship=True,
            active_ids=["data_ia"], context=self.context,
            external_id=known.external_id,
        )
        known_index = {key: known_offer
                       for key in known_offer["identity_keys"]}
        offers, status, _ = scan.fetch_linkedin_posts(
            context=self.context,
            provider=TwoPostsProvider(),
            rotation_state=XRayRotationState(),
            known_identity_index=known_index,
        )
        self.assertEqual(len(offers), 2)
        report = status.query_reports[0]
        self.assertEqual(report["parsed_count"], 2)
        self.assertEqual(report["raw_count"], 2)
        self.assertEqual(report["accepted_count"], 2)
        self.assertEqual(report["new_count"], 1)
        self.assertEqual(report["known_count"], 1)
        self.assertEqual(report["unknown_date_count"], 0)
        self.assertEqual(report["known_date_count"], 2)
        self.assertEqual(report["author_detected_count"], 2)
        self.assertEqual(report["contact_direct_count"], 2)
        self.assertEqual(report["profile_id"], "fast_data_ia")
        self.assertIn("site:", report["query"])
        self.assertTrue(report["host"])
        self.assertEqual(report["status"], "success")
        self.assertEqual(report["error"], "")
        for offer in offers:
            self.assertEqual(offer["discovery_count"], 1)
            self.assertEqual(offer["first_discovered_at"],
                             offer["last_discovered_at"])
            # Les trois requêtes du run retrouvent les mêmes posts : chacune
            # est tracée, sans devenir une découverte supplémentaire.
            self.assertEqual(len(offer["queries_that_found_it"]), 3)

    def test_same_post_found_by_all_queries_lists_every_query(self):
        class RepeatProvider:
            name = "fixture_repeat"

            def search(self, query, freshness=None, **kwargs):
                return [post_result("7351000000000000009")]

        offers, status, _ = scan.fetch_linkedin_posts(
            context=self.context,
            provider=RepeatProvider(),
            rotation_state=XRayRotationState(),
        )
        self.assertEqual(len(offers), 1)
        self.assertEqual(len(offers[0]["queries_that_found_it"]), 3)
        self.assertEqual(offers[0]["discovery_count"], 1)

    def test_fallback_requests_are_counted_not_silent(self):
        class BlockedProvider:
            name = "primary"

            def search(self, *_a, **_k):
                raise XRayBlockedError("challenge", status_code=202)

        class EmptyProvider:
            name = "secondary"

            def search(self, *_a, **_k):
                return []

        provider = FallbackXRayProvider(BlockedProvider(), EmptyProvider(),
                                        breaker_threshold=99)
        offers, status, _ = scan.fetch_linkedin_posts(
            context=self.context,
            provider=provider,
            rotation_state=XRayRotationState(),
        )
        self.assertEqual(offers, [])
        self.assertEqual(status.status, "empty")
        # 3 requêtes × (1 tentative bloquée + 1 fallback) = 6 appels réseau.
        self.assertEqual(status.network_request_count, 6)
        for report in status.query_reports:
            self.assertEqual(report["provider"], "secondary")
            self.assertEqual(
                [attempt["outcome"] for attempt in report["provider_attempts"]],
                ["blocked", "success"],
            )

    def test_linkedin_posts_crash_never_fails_the_scan(self):
        profile = scan.resolve_profile(
            scan.PROFILES, "fast_data_ia", cli_sources="LinkedIn Posts",
            domains=scan.DOMAINS, regions=scan.REGIONS,
            sources=scan.SOURCE_NAMES,
        )
        context = ScanContext(profile, NOW)
        with mock.patch.object(
            scan, "fetch_linkedin_posts",
            side_effect=RuntimeError("panne interne"),
        ):
            offers, statuses = scan.run_all_sources(context=context)
        self.assertEqual(offers, {})
        self.assertEqual(len(statuses), 1)
        self.assertEqual(statuses[0].source, "LinkedIn Posts")
        self.assertEqual(statuses[0].status, "error")
        self.assertIn("RuntimeError", statuses[0].http_error)


class TestNoveltyCounts(unittest.TestCase):
    def test_new_and_known_are_distributed_per_source_and_region(self):
        statuses = [
            scan.SourceStatus(source="LinkedIn", region_id="france",
                              accepted_count=2),
            scan.SourceStatus(source="Indeed", region_id="france",
                              accepted_count=1),
        ]
        offers = {
            "new-1": {"id": "new-1", "source": "LinkedIn",
                      "sources": ["LinkedIn"], "regions": ["france"]},
            "old-1": {"id": "old-1", "source": "LinkedIn",
                      "sources": ["LinkedIn", "Indeed"],
                      "regions": ["france"]},
        }
        scan.assign_novelty_counts(statuses, offers, {"new-1"})
        linkedin, indeed = statuses
        self.assertEqual(linkedin.new_count, 1)
        self.assertEqual(linkedin.known_count, 1)
        self.assertEqual(indeed.new_count, 0)
        self.assertEqual(indeed.known_count, 1)


class TestBenchmarkDefaultReport(unittest.TestCase):
    def test_benchmark_without_output_writes_isolated_default_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            benchmarks = root / "benchmarks"
            (root / "offers.json").write_text("{}\n", encoding="utf-8")
            (root / "seen.json").write_text("[]\n", encoding="utf-8")
            with mock.patch.object(scan, "OFFERS_FILE", root / "offers.json"), \
                 mock.patch.object(scan, "SEEN_FILE", root / "seen.json"), \
                 mock.patch.object(scan, "STATUS_FILE", root / "status.json"), \
                 mock.patch.object(scan, "METRICS_FILE", root / "metrics.json"), \
                 mock.patch.object(scan, "XRAY_STATE_FILE", root / "xray.json"), \
                 mock.patch.object(scan, "DOCS_INDEX", root / "index.html"), \
                 mock.patch.object(scan, "BENCHMARKS_DIR", benchmarks), \
                 mock.patch.object(scan, "PROFILE_STATUS_FILES", {}), \
                 mock.patch.object(scan, "run_all_sources",
                                   return_value=({}, [])), \
                 mock.patch("sys.stdout", new_callable=io.StringIO):
                result = scan.main(["--profile", "fast_data_ia", "--benchmark"])
            self.assertEqual(result, 0)
            reports = list(benchmarks.glob("fast_data_ia-*.json"))
            self.assertEqual(len(reports), 1)
            payload = json.loads(reports[0].read_text(encoding="utf-8"))
            self.assertTrue(payload["benchmark"])
            self.assertFalse(payload["production_mutated"])
            self.assertFalse((root / "metrics.json").exists())
            self.assertFalse((root / "xray.json").exists())

    def test_single_source_flag_is_exclusive_and_mapped(self):
        with self.assertRaises(SystemExit):
            scan.main(["--source", "LinkedIn Posts", "--sources", "APEC"])


RUN_T0 = datetime(2026, 7, 16, 6, 0, tzinfo=timezone.utc)


def profile_context(profile_id, *, domains, regions, run_at,
                    sources=("LinkedIn",), window_hours=24):
    profile = SimpleNamespace(
        id=profile_id, domains=domains, regions=regions,
        sources=sources, window_hours=window_hours,
    )
    return SimpleNamespace(profile=profile, run_at=run_at)


def full_context(run_at):
    return profile_context(
        "full_general",
        domains=("data_ia", "commerce_bizdev", "cybersecurite", "finance",
                 "reseaux_systemes"),
        regions=("france", "belgique", "luxembourg", "maroc"),
        run_at=run_at,
    )


def fast_context(run_at):
    return profile_context(
        "fast_data_ia", domains=("data_ia",), regions=("france",),
        run_at=run_at, window_hours=2,
    )


def store_offer(offer_id, domain, region, title):
    return {
        "id": offer_id,
        "title": title,
        "company": "Entreprise",
        "source": "LinkedIn",
        "sources": ["LinkedIn"],
        "domains": [domain],
        "regions": [region],
        "sort_date": RUN_T0.isoformat(),
        "identity_keys": [f"linkedin:{offer_id}"],
    }


def success_status(profile_id, region_id="france"):
    return SimpleNamespace(
        source="LinkedIn", region_id=region_id, profile_id=profile_id,
        status="success", coverage_complete=True, interrupted=False,
        timeout_count=0, failed_queries=0, blocked_count=0,
    )


def full_statuses():
    return [success_status("full_general", region)
            for region in ("france", "belgique", "luxembourg", "maroc")]


class TestFastFullAlternation(unittest.TestCase):
    """Scénario réaliste FULL → FAST → FAST → FULL du chantier 13."""

    def initial_offers(self):
        return [
            store_offer("o-data", "data_ia", "france", "Stage Data Engineer"),
            store_offer("o-marketing", "commerce_bizdev", "france",
                        "Stage Marketing"),
            store_offer("o-cyber", "cybersecurite", "belgique",
                        "Stage Cybersécurité"),
            store_offer("o-finance", "finance", "luxembourg", "Stage Finance"),
            store_offer("o-telecom", "reseaux_systemes", "maroc",
                        "Stage Télécom"),
        ]

    def test_alternation_never_touches_out_of_scope_offers(self):
        # FULL initial : les cinq offres entrent dans le store.
        store = radar_store.reconcile_offer_store(
            self.initial_offers(),
            full_statuses(),
            full_context(RUN_T0),
            store={},
        )
        self.assertEqual(len(store), 5)
        untouched_ids = ("o-marketing", "o-cyber", "o-finance", "o-telecom")
        before = {offer_id: dict(store[offer_id]) for offer_id in untouched_ids}

        # FAST n°1 : l'offre Data/IA France est mise à jour (nouveau titre).
        updated = store_offer("o-data", "data_ia", "france",
                              "Stage Data Engineer (mis à jour)")
        store = radar_store.reconcile_offer_store(
            [updated], [success_status("fast_data_ia")],
            fast_context(RUN_T0 + timedelta(hours=2)), store=store,
        )
        # FAST n°2 : redécouverte simple de la même offre.
        store = radar_store.reconcile_offer_store(
            [store_offer("o-data", "data_ia", "france",
                         "Stage Data Engineer (mis à jour)")],
            [success_status("fast_data_ia")],
            fast_context(RUN_T0 + timedelta(hours=4)), store=store,
        )

        self.assertEqual(store["o-data"]["title"],
                         "Stage Data Engineer (mis à jour)")
        for offer_id in untouched_ids:
            entry = store[offer_id]
            self.assertTrue(entry["active"], offer_id)
            self.assertFalse(entry["likely_expired"], offer_id)
            self.assertEqual(entry["successful_absence_count_by_profile"], {},
                             offer_id)
            self.assertEqual(entry["missing_since_by_profile"], {}, offer_id)
            self.assertEqual(entry["title"], before[offer_id]["title"])
            self.assertEqual(entry["last_seen_at"], before[offer_id]["last_seen_at"])

        # FULL suivant : le Maroc n'est pas redécouvert, les autres oui.
        second_full = full_context(RUN_T0 + timedelta(hours=24))
        found_again = [offer for offer in self.initial_offers()
                       if offer["id"] != "o-telecom"]
        store = radar_store.reconcile_offer_store(
            found_again, full_statuses(),
            second_full, store=store,
        )
        telecom = store["o-telecom"]
        self.assertEqual(
            telecom["successful_absence_count_by_profile"],
            {"full_general": 1},
        )
        self.assertTrue(telecom["active"])
        self.assertFalse(telecom["likely_expired"])
        for offer_id in ("o-data", "o-marketing", "o-cyber", "o-finance"):
            self.assertEqual(
                store[offer_id]["successful_absence_count_by_profile"], {},
                offer_id,
            )


class TestUnknownDateFreshness(unittest.TestCase):
    def unknown_post(self, discovered_at):
        return {
            "id": "post-1",
            "title": "Stage data — recrutement",
            "company": "Alice Martin",
            "source": "LinkedIn Posts",
            "sources": ["LinkedIn Posts"],
            "domains": ["data_ia"],
            "regions": ["france"],
            "date": "",
            "date_known": False,
            "date_status": "unknown",
            "sort_date": discovered_at.isoformat(),
            "identity_keys": ["linkedin_post:7351000000000000001"],
            "first_discovered_at": discovered_at.isoformat(),
            "last_discovered_at": discovered_at.isoformat(),
            "discovery_count": 1,
            "queries_that_found_it": ["site:linkedin.com/posts A"],
        }

    def test_rediscovery_does_not_refresh_unknown_date_nor_first_discovery(self):
        first_run = fast_context(RUN_T0)
        store = radar_store.reconcile_offer_store(
            [self.unknown_post(RUN_T0)],
            [success_status("fast_data_ia")],
            first_run, store={},
        )
        later = RUN_T0 + timedelta(hours=6)
        rediscovered = self.unknown_post(later)
        rediscovered["queries_that_found_it"] = ["site:fr.linkedin.com/posts B"]
        store = radar_store.reconcile_offer_store(
            [rediscovered], [success_status("fast_data_ia")],
            fast_context(later), store=store,
        )
        entry = store["post-1"]
        self.assertEqual(entry["sort_date"], RUN_T0.isoformat())
        self.assertEqual(entry["first_discovered_at"], RUN_T0.isoformat())
        self.assertEqual(entry["last_discovered_at"], later.isoformat())
        self.assertEqual(entry["discovery_count"], 2)
        self.assertEqual(entry["queries_that_found_it"], [
            "site:linkedin.com/posts A", "site:fr.linkedin.com/posts B",
        ])
        self.assertFalse(entry["date_known"])

    def test_real_date_replaces_unknown_on_rediscovery(self):
        store = radar_store.reconcile_offer_store(
            [self.unknown_post(RUN_T0)],
            [success_status("fast_data_ia")],
            fast_context(RUN_T0), store={},
        )
        dated = self.unknown_post(RUN_T0 + timedelta(hours=1))
        dated.update({
            "date": "2026-07-16", "date_known": True, "date_status": "exact",
            "sort_date": RUN_T0.isoformat(),
        })
        store = radar_store.reconcile_offer_store(
            [dated], [success_status("fast_data_ia")],
            fast_context(RUN_T0 + timedelta(hours=1)), store=store,
        )
        self.assertTrue(store["post-1"]["date_known"])
        self.assertEqual(store["post-1"]["date"], "2026-07-16")

    def test_unknown_date_visibility_window(self):
        offer = self.unknown_post(RUN_T0)
        self.assertFalse(radar_store.unknown_date_is_stale(
            offer, now=RUN_T0 + timedelta(hours=71),
        ))
        self.assertTrue(radar_store.unknown_date_is_stale(
            offer, now=RUN_T0 + timedelta(hours=72),
        ))
        dated = dict(offer, date_known=True, date="2026-07-16")
        self.assertFalse(radar_store.unknown_date_is_stale(
            dated, now=RUN_T0 + timedelta(days=30),
        ))
        orphan = {"id": "x", "date_known": False}
        self.assertTrue(radar_store.unknown_date_is_stale(orphan, now=RUN_T0))


if __name__ == "__main__":
    unittest.main()
