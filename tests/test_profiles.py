"""Tests sans réseau des profils FAST/FULL et de leurs surcharges."""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from radar_profiles import (
    DEFAULT_PROFILE_ID,
    KNOWN_SOURCES,
    ProfileConfigError,
    ScanContext,
    create_scan_context,
    load_profiles,
    resolve_profile,
)


class TestVersionedProfiles(unittest.TestCase):
    def setUp(self):
        self.profiles = load_profiles()

    def test_fast_and_full_resolve_real_catalogs(self):
        self.assertEqual(self.profiles.default_profile_id, "full_general")
        self.assertEqual(DEFAULT_PROFILE_ID, "full_general")
        fast = self.profiles["fast_data_ia"]
        full = self.profiles["full_general"]
        self.assertEqual(fast.domains, ("data_ia",))
        self.assertEqual(fast.regions, ("france",))
        self.assertEqual(fast.window_hours, 2)
        self.assertNotIn("Google Jobs", fast.sources)
        self.assertEqual(len(full.domains), 16)
        self.assertEqual(
            full.regions, ("france", "belgique", "luxembourg", "maroc")
        )
        self.assertEqual(full.window_hours, 24)
        self.assertIn("Google Jobs", full.sources)

    def test_glassdoor_cannot_be_selected(self):
        self.assertNotIn("Glassdoor", KNOWN_SOURCES)
        self.assertTrue(
            all("Glassdoor" not in profile.sources for profile in self.profiles.values())
        )
        with self.assertRaises(ProfileConfigError):
            resolve_profile(
                self.profiles, cli_sources="LinkedIn,Glassdoor", env={}
            )

    def test_profiles_are_deeply_immutable(self):
        fast = self.profiles["fast_data_ia"]
        with self.assertRaises(TypeError):
            fast.results_wanted["linkedin"] = 1
        with self.assertRaises(TypeError):
            self.profiles.profiles["other"] = fast
        with self.assertRaises(AttributeError):
            fast.window_hours = 3
        self.assertEqual(fast.result_limit("LinkedIn"), 100)

    def test_enabled_sentinel_is_resolved_dynamically(self):
        domains = {
            "data_ia": {"enabled": True},
            "disabled": {"enabled": False},
        }
        regions = {
            "france": {"enabled": True},
            "disabled": {"enabled": False},
        }
        catalog = load_profiles(domains=domains, regions=regions)
        self.assertEqual(catalog["full_general"].domains, ("data_ia",))
        self.assertEqual(catalog["full_general"].regions, ("france",))

    def test_cli_profile_wins_over_environment(self):
        selected = resolve_profile(
            self.profiles,
            profile_id="fast_data_ia",
            env={"RADAR_PROFILE": "full_general"},
        )
        self.assertEqual(selected.id, "fast_data_ia")

    def test_environment_wins_over_explicit_and_catalog_defaults(self):
        selected = resolve_profile(
            self.profiles,
            env={"RADAR_PROFILE": "fast_data_ia"},
            default_profile_id="full_general",
        )
        self.assertEqual(selected.id, "fast_data_ia")

    def test_catalog_default_is_retrocompatible_without_environment(self):
        selected = resolve_profile(self.profiles, env={})
        self.assertEqual(selected.id, "full_general")

    def test_environment_and_cli_overrides_are_strict(self):
        from_env = resolve_profile(
            self.profiles,
            env={
                "RADAR_PROFILE": "full_general",
                "RADAR_DOMAINS": "data_ia,dev_web",
                "RADAR_REGIONS": "france,belgique",
                "RADAR_SOURCES": "LinkedIn,Indeed",
                "RADAR_WINDOW_HOURS": "8",
            },
        )
        self.assertEqual(from_env.domains, ("data_ia", "dev_web"))
        self.assertEqual(from_env.regions, ("france", "belgique"))
        self.assertEqual(from_env.sources, ("LinkedIn", "Indeed"))
        self.assertEqual(from_env.window_hours, 8)

        cli = resolve_profile(
            self.profiles,
            profile_id="full_general",
            env={
                "RADAR_DOMAINS": "dev_web",
                "RADAR_WINDOW_HOURS": "8",
            },
            cli_domains="data_ia",
            cli_window_hours=2,
        )
        self.assertEqual(cli.domains, ("data_ia",))
        self.assertEqual(cli.window_hours, 2)

        all_cli = resolve_profile(
            self.profiles,
            profile_id="full_general",
            env={
                "RADAR_REGIONS": "belgique",
                "RADAR_SOURCES": "APEC",
            },
            cli_regions="france",
            cli_sources="LinkedIn,Indeed",
        )
        self.assertEqual(all_cli.regions, ("france",))
        self.assertEqual(all_cli.sources, ("LinkedIn", "Indeed"))

    def test_source_override_hydrates_missing_fast_budgets(self):
        selected = resolve_profile(
            self.profiles,
            profile_id="fast_data_ia",
            cli_sources="Google Jobs,LinkedIn Posts",
            env={},
        )
        self.assertEqual(selected.sources, ("Google Jobs", "LinkedIn Posts"))
        self.assertEqual(selected.results_wanted["google"], 900)
        self.assertEqual(selected.max_pages["Google Jobs"], 30)
        self.assertEqual(selected.query_budgets["LinkedIn Posts"], 3)
        self.assertEqual(selected.detail_budgets["LinkedIn Posts"], 0)

    def test_unknown_profile_domain_region_source_and_window_are_rejected(self):
        cases = [
            ({"RADAR_PROFILE": "absent"}, {}),
            ({"RADAR_DOMAINS": "absent"}, {}),
            ({"RADAR_REGIONS": "absent"}, {}),
            ({"RADAR_SOURCES": "absent"}, {}),
            ({"RADAR_WINDOW_HOURS": "0"}, {}),
            ({"RADAR_WINDOW_HOURS": "2.5"}, {}),
            ({"RADAR_DOMAINS": "data_ia,data_ia"}, {}),
        ]
        for env, kwargs in cases:
            with self.subTest(env=env), self.assertRaises(ProfileConfigError):
                resolve_profile(self.profiles, env=env, **kwargs)

    def test_context_normalizes_once_to_utc(self):
        fast = self.profiles["fast_data_ia"]
        local = datetime(
            2026, 7, 16, 12, 0, tzinfo=timezone(timedelta(hours=2))
        )
        context = ScanContext(fast, local)
        self.assertEqual(
            context.run_at,
            datetime(2026, 7, 16, 10, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(
            context.cutoff,
            datetime(2026, 7, 16, 8, 0, tzinfo=timezone.utc),
        )

    def test_context_factory_uses_resolved_profile_and_fixed_clock(self):
        run_at = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
        context = create_scan_context(
            self.profiles,
            env={"RADAR_PROFILE": "fast_data_ia"},
            run_at=run_at,
            cli_window_hours=3,
        )
        self.assertEqual(context.profile.id, "fast_data_ia")
        self.assertEqual(context.profile.window_hours, 3)
        self.assertEqual(context.cutoff, run_at - timedelta(hours=3))


class TestProfileValidation(unittest.TestCase):
    def write_profiles(self, mutator):
        source = Path("data/profiles.json")
        payload = json.loads(source.read_text(encoding="utf-8"))
        mutator(payload)
        temp = tempfile.TemporaryDirectory()
        path = Path(temp.name) / "profiles.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return temp, path

    def test_unknown_configured_domain_is_rejected(self):
        temp, path = self.write_profiles(
            lambda payload: payload["profiles"]["fast_data_ia"].update(
                domains=["not_a_domain"]
            )
        )
        with temp, self.assertRaises(ProfileConfigError):
            load_profiles(path)

    def test_unknown_configured_source_is_rejected(self):
        temp, path = self.write_profiles(
            lambda payload: payload["profiles"]["fast_data_ia"]["sources"].append(
                "Glassdoor"
            )
        )
        with temp, self.assertRaises(ProfileConfigError):
            load_profiles(path)

    def test_missing_technical_budget_is_migrated(self):
        def remove(payload):
            del payload["profiles"]["fast_data_ia"]["max_pages"]

        temp, path = self.write_profiles(remove)
        with temp:
            profile = load_profiles(path)["fast_data_ia"]
        self.assertEqual(profile.max_pages["LinkedIn"], 40)
        self.assertEqual(profile.max_pages["LinkedIn Posts"], 1)

    def test_plain_legacy_catalog_is_migrated_deterministically(self):
        payload = json.loads(Path("data/profiles.json").read_text(encoding="utf-8"))
        legacy = {}
        for profile_id, raw in payload["profiles"].items():
            legacy[profile_id] = {
                key: raw[key]
                for key in ("domains", "regions", "sources", "window_hours")
            }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy-profiles.json"
            path.write_text(json.dumps(legacy), encoding="utf-8")
            catalog = load_profiles(path)
        self.assertEqual(catalog.schema_version, 1)
        self.assertEqual(catalog.default_profile_id, "full_general")
        self.assertEqual(catalog["fast_data_ia"].request_timeout_s, 20)
        self.assertEqual(catalog["fast_data_ia"].max_stale_pages, 2)
        self.assertEqual(catalog["fast_data_ia"].max_parallel_sources, 4)
        self.assertEqual(catalog["fast_data_ia"].max_parallel_regions, 1)
        self.assertEqual(catalog["full_general"].results_wanted["google"], 900)

    def test_invalid_technical_values_and_unknown_budget_keys_are_rejected(self):
        mutations = [
            lambda payload: payload["profiles"]["fast_data_ia"].update(
                max_parallel_sources=0
            ),
            lambda payload: payload["profiles"]["fast_data_ia"].update(
                request_timeout_s=True
            ),
            lambda payload: payload["profiles"]["fast_data_ia"][
                "max_pages"
            ].update({"Glassdoor": 2}),
            lambda payload: payload["profiles"]["fast_data_ia"][
                "detail_budgets"
            ].update({"APEC": -1}),
        ]
        for mutator in mutations:
            temp, path = self.write_profiles(mutator)
            with self.subTest(mutator=mutator), temp, self.assertRaises(
                ProfileConfigError
            ):
                load_profiles(path)

    def test_future_schema_is_rejected(self):
        temp, path = self.write_profiles(
            lambda payload: payload.update(schema_version=2)
        )
        with temp, self.assertRaises(ProfileConfigError):
            load_profiles(path)

    def test_boolean_schema_and_empty_source_registry_are_rejected(self):
        temp, path = self.write_profiles(
            lambda payload: payload.update(schema_version=True)
        )
        with temp, self.assertRaises(ProfileConfigError):
            load_profiles(path)
        with self.assertRaises(ProfileConfigError):
            load_profiles(sources=[])


if __name__ == "__main__":
    unittest.main()
