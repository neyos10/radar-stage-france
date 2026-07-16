"""Tests purs de la persistance consciente des profils."""

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from radar_store import (
    active_dashboard_offers,
    dashboard_offer_sections,
    expired_dashboard_offers,
    migrate_offer_store,
    migrate_seen_memory,
    offer_is_pertinent_absence,
    purge_offer_store,
    purge_seen_memory,
    reconcile_offer_store,
    update_seen_memory,
)


RUN_AT = datetime(2026, 7, 16, 10, 0, tzinfo=timezone.utc)


def context(
    *, profile_id="fast_data_ia", domains=("data_ia",),
    regions=("france",), sources=("LinkedIn",), window_hours=2,
    run_at=RUN_AT,
):
    profile = SimpleNamespace(
        id=profile_id,
        domains=domains,
        regions=regions,
        sources=sources,
        window_hours=window_hours,
    )
    return SimpleNamespace(profile=profile, run_at=run_at)


def status(
    state="success", *, source="LinkedIn", region_id="france",
    profile_id="fast_data_ia", coverage_complete=True,
    interrupted=False, timeout_count=0, failed_queries=0, blocked_count=0,
):
    return SimpleNamespace(
        source=source,
        region_id=region_id,
        profile_id=profile_id,
        status=state,
        coverage_complete=coverage_complete,
        interrupted=interrupted,
        timeout_count=timeout_count,
        failed_queries=failed_queries,
        blocked_count=blocked_count,
    )


def offer(offer_id="offer-1", **updates):
    result = {
        "id": offer_id,
        "title": "Stage Data Analyst",
        "company": "Example",
        "source": "LinkedIn",
        "sources": ["LinkedIn"],
        "domains": ["data_ia"],
        "regions": ["france"],
        "sort_date": RUN_AT.isoformat(),
        "identity_keys": ["linkedin:123"],
    }
    result.update(updates)
    return result


class TestOfferMigration(unittest.TestCase):
    def test_migration_preserves_key_and_id_and_reactivates_legacy_absence(self):
        payload = {
            "legacy-id": {
                **offer("legacy-id"),
                "active": False,
                "first_seen": "2026-07-15T09:00:00Z",
            }
        }
        migrated = migrate_offer_store(payload, run_at=RUN_AT)

        self.assertEqual(list(migrated), ["legacy-id"])
        self.assertEqual(migrated["legacy-id"]["id"], "legacy-id")
        self.assertEqual(
            migrated["legacy-id"]["first_seen"],
            "2026-07-15T09:00:00+00:00",
        )
        self.assertTrue(migrated["legacy-id"]["active"])
        self.assertEqual(migrated["legacy-id"]["store_schema_version"], 2)

    def test_glassdoor_is_retired_inactive_and_hidden_without_deletion(self):
        payload = {
            "glassdoor-id": offer(
                "glassdoor-id", source="Glassdoor", sources=["Glassdoor"]
            )
        }
        migrated = migrate_offer_store(payload, run_at=RUN_AT)
        retired = migrated["glassdoor-id"]

        self.assertTrue(retired["source_retired"])
        self.assertFalse(retired["active"])
        self.assertIn("Glassdoor", retired["retired_sources"])
        self.assertEqual(active_dashboard_offers(migrated), [])
        self.assertEqual(expired_dashboard_offers(migrated), [])

    def test_glassdoor_evidence_does_not_retire_a_multi_source_offer(self):
        payload = {
            "shared": offer(
                "shared", source="Glassdoor", sources=["Glassdoor", "Indeed"]
            )
        }
        migrated = migrate_offer_store(payload, run_at=RUN_AT)
        self.assertFalse(migrated["shared"]["source_retired"])
        self.assertTrue(migrated["shared"]["active"])


class TestProfileAwareReconciliation(unittest.TestCase):
    def discovered_store(self):
        return reconcile_offer_store(
            [offer()], [status()], context(), store={}
        )

    def test_fast_data_does_not_modify_marketing_offer(self):
        base = migrate_offer_store({
            "marketing": offer("marketing", domains=["commerce_bizdev"])
        }, run_at=RUN_AT)
        actual = reconcile_offer_store(
            [], [status()], context(), store=base
        )
        self.assertEqual(actual["marketing"], base["marketing"])

    def test_fast_france_does_not_modify_belgium_offer(self):
        base = migrate_offer_store({
            "belgium": offer("belgium", regions=["belgique"])
        }, run_at=RUN_AT)
        actual = reconcile_offer_store(
            [], [status(region_id="france")], context(), store=base
        )
        self.assertEqual(actual["belgium"], base["belgium"])

    def test_blocked_or_partial_source_never_counts_an_absence(self):
        base = self.discovered_store()
        later = RUN_AT + timedelta(hours=1)
        for source_status in (
            status("blocked"),
            status("success", coverage_complete=False),
            status("success", interrupted=True),
            status("success", timeout_count=1),
            status("success", failed_queries=1),
            status("success", blocked_count=1),
        ):
            with self.subTest(source_status=source_status):
                actual = reconcile_offer_store(
                    [], [source_status], context(run_at=later), store=base
                )
                self.assertEqual(actual["offer-1"], base["offer-1"])

    def test_empty_successfully_covered_source_counts_one_absence_only(self):
        base = self.discovered_store()
        later = RUN_AT + timedelta(hours=1)
        actual = reconcile_offer_store(
            [], [status("empty")], context(run_at=later), store=base
        )
        entry = actual["offer-1"]
        self.assertEqual(
            entry["successful_absence_count_by_profile"]["fast_data_ia"], 1
        )
        self.assertFalse(entry["likely_expired"])
        self.assertTrue(entry["active"])

        replay = reconcile_offer_store(
            [], [status("empty")], context(run_at=later), store=actual
        )
        self.assertEqual(
            replay["offer-1"]["successful_absence_count_by_profile"]
            ["fast_data_ia"],
            1,
        )

    def test_72_hours_and_three_absences_marks_likely_expired(self):
        current = self.discovered_store()
        for offset in (1, 37, 73):
            current = reconcile_offer_store(
                [], [status("empty")],
                context(run_at=RUN_AT + timedelta(hours=offset)),
                store=current,
            )
        entry = current["offer-1"]
        self.assertEqual(
            entry["successful_absence_count_by_profile"]["fast_data_ia"], 3
        )
        self.assertTrue(entry["likely_expired"])
        self.assertTrue(entry["active"])
        active, expired = dashboard_offer_sections(current)
        self.assertEqual(active, [])
        self.assertEqual([item["id"] for item in expired], ["offer-1"])

    def test_three_fast_absences_before_72_hours_do_not_expire(self):
        current = self.discovered_store()
        for offset in (1, 2, 3):
            current = reconcile_offer_store(
                [], [status("empty")],
                context(run_at=RUN_AT + timedelta(hours=offset)),
                store=current,
            )
        self.assertFalse(current["offer-1"]["likely_expired"])

    def test_rediscovery_by_another_source_resets_all_absence_evidence(self):
        current = self.discovered_store()
        original_discovered_at = current["offer-1"]["discovered_at"]
        for offset in (1, 37, 73):
            current = reconcile_offer_store(
                [], [status("empty")],
                context(run_at=RUN_AT + timedelta(hours=offset)), store=current,
            )
        rediscovered = offer(
            "new-native-id",
            source="Indeed",
            sources=["Indeed"],
            identity_keys=["linkedin:123"],
        )
        full_context = context(
            profile_id="full_general", sources=("LinkedIn", "Indeed"),
            window_hours=24, run_at=RUN_AT + timedelta(hours=74),
        )
        actual = reconcile_offer_store(
            [rediscovered],
            [status(source="Indeed", profile_id="full_general")],
            full_context,
            store=current,
        )
        self.assertIn("offer-1", actual)
        self.assertNotIn("new-native-id", actual)
        entry = actual["offer-1"]
        self.assertFalse(entry["likely_expired"])
        self.assertEqual(entry["missing_since_by_profile"], {})
        self.assertEqual(entry["successful_absence_count_by_profile"], {})
        self.assertIn("Indeed", entry["sources"])
        self.assertIn("new-native-id", entry["legacy_ids"])
        self.assertEqual(entry["discovered_at"], original_discovered_at)

    def test_unqueried_second_source_prevents_global_absence(self):
        base = self.discovered_store()
        base["offer-1"]["sources"] = ["LinkedIn", "Google Jobs"]
        full = context(
            profile_id="full_general",
            sources=("LinkedIn", "Google Jobs"),
            window_hours=24,
            run_at=RUN_AT + timedelta(hours=1),
        )
        self.assertFalse(offer_is_pertinent_absence(
            base["offer-1"],
            [status(profile_id="full_general")],
            full,
        ))
        actual = reconcile_offer_store(
            [], [status(profile_id="full_general")], full, store=base
        )
        self.assertEqual(actual["offer-1"], base["offer-1"])


class TestRetentionAndSeenMemory(unittest.TestCase):
    def test_retired_offer_is_purged_only_after_normal_retention(self):
        migrated = migrate_offer_store({
            "old": offer("old", source="Glassdoor", sources=["Glassdoor"])
        }, run_at=RUN_AT)
        self.assertIn(
            "old", purge_offer_store(
                migrated, now=RUN_AT + timedelta(days=14), retention_days=14
            )
        )
        self.assertNotIn(
            "old", purge_offer_store(
                migrated, now=RUN_AT + timedelta(days=14, seconds=1),
                retention_days=14,
            )
        )

    def test_legacy_seen_list_migrates_without_false_new_notifications(self):
        document, new_ids = update_seen_memory(
            ["known-a", "known-b"],
            ["known-a", "known-b", "actually-new"],
            run_at=RUN_AT,
            profile_id="fast_data_ia",
        )
        self.assertEqual(new_ids, {"actually-new"})
        self.assertEqual(document["schema_version"], 2)
        self.assertEqual(set(document["offers"]), {
            "known-a", "known-b", "actually-new"
        })
        self.assertEqual(
            document["offers"]["known-a"]["profiles"], ["fast_data_ia"]
        )

    def test_seen_v2_preserves_first_seen_and_adds_profile(self):
        payload = {
            "schema_version": 2,
            "offers": {
                "known": {
                    "first_seen": "2026-06-01T12:00:00Z",
                    "last_seen": "2026-07-15T12:00:00Z",
                    "profiles": ["full_general"],
                }
            },
        }
        document, new_ids = update_seen_memory(
            payload, ["known"], run_at=RUN_AT, profile_id="fast_data_ia"
        )
        meta = document["offers"]["known"]
        self.assertEqual(new_ids, set())
        self.assertEqual(meta["first_seen"], "2026-06-01T12:00:00+00:00")
        self.assertEqual(meta["last_seen"], "2026-07-16T10:00:00+00:00")
        self.assertEqual(meta["profiles"], ["full_general", "fast_data_ia"])

    def test_seen_purge_uses_last_seen_and_keeps_exact_45_day_boundary(self):
        payload = migrate_seen_memory({
            "schema_version": 2,
            "offers": {
                "old-z": {
                    "first_seen": "2020-01-01T00:00:00Z",
                    "last_seen": (RUN_AT - timedelta(days=45, seconds=1)).isoformat(),
                    "profiles": ["full_general"],
                },
                "boundary-a": {
                    "first_seen": "2020-01-01T00:00:00Z",
                    "last_seen": (RUN_AT - timedelta(days=45)).isoformat(),
                    "profiles": ["full_general"],
                },
                "recent-m": {
                    "first_seen": "2020-01-01T00:00:00Z",
                    "last_seen": (RUN_AT - timedelta(days=1)).isoformat(),
                    "profiles": ["fast_data_ia"],
                },
            },
        }, run_at=RUN_AT)
        purged = purge_seen_memory(payload, now=RUN_AT, retention_days=45)
        self.assertNotIn("old-z", purged["offers"])
        self.assertIn("boundary-a", purged["offers"])
        self.assertIn("recent-m", purged["offers"])


if __name__ == "__main__":
    unittest.main()
