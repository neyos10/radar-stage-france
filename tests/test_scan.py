"""Tests du pipeline scan.py — aucun accès réseau, aucune dépendance jobspy.

Couvre : dates, scoring multi-domaines, filtres natifs, déduplication, tri,
champs incomplets, sécurité HTML, sources optionnelles, circuit breaker et UI.
"""

import io
import json
import logging
import os
import re
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scan
import radar_xray as xray

TODAY = datetime.now(timezone.utc).date().isoformat()
YESTERDAY = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
TOO_OLD = (datetime.now(timezone.utc) - timedelta(days=30)).date().isoformat()
RECENT_1H = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
RECENT_2H = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
RECENT_3H = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()


def offer(title="Stage Data Engineer", company="ACME", location="Paris",
          url="https://example.com/o/1", source="LinkedIn", date=TODAY, **kw):
    return scan.make_offer(title, company, location, url, source, date, **kw)


# Le catalogue garde les 17 domaines (seul data_ia est actif). Les tests de
# calibration multi-domaine s'exécutent contre le catalogue complet pour ne
# perdre aucune couverture de la version v-multi-domaine.
ALL_DOMAIN_IDS = list(scan.DOMAINS)


def offer_all_domains(**kw):
    return offer(active_ids=ALL_DOMAIN_IDS, **kw)


def fake_response(payload=None, status=200, headers=None):
    response = mock.Mock(status_code=status, headers=headers or {})
    response.json.return_value = payload or {}
    response.raise_for_status.return_value = None
    return response


class TestCleanText(unittest.TestCase):
    def test_none_and_nan(self):
        self.assertEqual(scan.clean_text(None), "")
        self.assertEqual(scan.clean_text("nan"), "")
        self.assertEqual(scan.clean_text("NaT"), "")
        self.assertEqual(scan.clean_text("None"), "")
        self.assertEqual(scan.clean_text(float("nan")), "")

    def test_strip(self):
        self.assertEqual(scan.clean_text("  x "), "x")


class TestDomainCatalogAndScoring(unittest.TestCase):
    def test_catalog_keeps_seventeen_domains_with_only_data_enabled(self):
        # Usage perso : on désactive, on ne supprime pas.
        self.assertEqual(len(scan.DOMAINS), 17)
        with mock.patch.dict(os.environ, {"RADAR_DOMAINS": ""}):
            self.assertEqual(scan.enabled_domain_ids(), ["data_ia"])
        for domain_id, domain in scan.DOMAINS.items():
            self.assertEqual(domain["enabled"], domain_id == "data_ia")
            self.assertGreaterEqual(len(domain["queries"]), 4)
            self.assertGreaterEqual(len(domain["positive"]), 12)
            self.assertEqual(domain["priority"], 3 if domain_id == "data_ia" else 1)
            if domain_id != "data_ia":
                self.assertLessEqual(len(domain["queries"]), 6)
                self.assertLessEqual(len(domain["positive"]), 21)

    def test_data_domain_matches_personal_profile(self):
        data = scan.DOMAINS["data_ia"]
        self.assertEqual(data["queries"], [
            "stage data engineer", "stage data engineering PFE", "stage big data",
            "stage ETL", "stage data analyst", "stage business intelligence",
            "stage data scientist", "stage machine learning",
            "stage ingénieur données",
        ])
        for word in ["data engineer", "data engineering", "big data", "spark",
                     "pyspark", "kafka", "hadoop", "etl", "talend", "airflow",
                     "data pipeline", "data warehouse", "modélisation de données",
                     "business intelligence", "power bi", "sql", "data analyst",
                     "data scientist", "machine learning", "nlp"]:
            self.assertIn(word, data["positive"])
        for word in ["audit", "comptabilité", "contrôle de gestion",
                     "ressources humaines", "juridique", "commercial", "marketing"]:
            self.assertIn(word, data["negative"])
        self.assertIn("recrutement", data["negative_title_only"])
        self.assertNotIn("recrutement", data["negative"])
        self.assertEqual(data["negative_exceptions"], ["marketing analytics"])
        self.assertEqual(data["description_only_min_positives"], 3)
        for domain_id, domain in scan.DOMAINS.items():
            if domain_id != "data_ia":
                self.assertNotIn("description_only_min_positives", domain)

    def test_personal_scope_accepts_data_and_rejects_neighbours(self):
        for title in ["Stage PFE Data Engineer Kafka", "Stage Data Analyst Power BI",
                      "Stagiaire Big Data Hadoop", "Stage Airflow / Talend",
                      "Stage Marketing Analytics", "Stage modélisation de données"]:
            o, reason = offer(title=title, url=f"https://e.com/{title}")
            self.assertIsNone(reason, title)
        for title in ["Stage Audit Data", "Stage contrôle de gestion Power BI",
                      "Stage Data Marketing digital", "Stage recrutement data",
                      "Stagiaire comptabilité SQL", "Stage ressources humaines data"]:
            o, reason = offer(title=title, url=f"https://e.com/{title}")
            self.assertEqual(reason, scan.REASON_NO_DOMAIN, title)

    def test_real_linkedin_data_titles_missed_before_enrichment(self):
        """Intitulés réels LinkedIn France (run du 25/11/2026, sans
        description) rejetés « aucun domaine » avant l'enrichissement."""
        for title in [
            "Stagiaire Data H/F/NB",
            "STAGE - Ingénierie Data et KPI maturité ingénierie Satcom - F/H",
            "Products Analytics Engineer - Internship",
            "Stagiaire Analyste Données & Supervision Technique H/F",
            "Stage - Chargé(e) de Data Management & gouvernance de la donnée F/H",
            "Stage Data Officer - Data Management Pricing H/F",
            "AI Cloud Engineer (stage) - H/F",
            "Internship : Computer Vision - Material properties estimation",
            "Data Automation Specialist Intern",
        ]:
            o, reason = offer(title=title, url=f"https://e.com/{title}")
            self.assertIsNone(reason, title)
        # Les voisins hors profil restent rejetés.
        for title in ["Stage : Marketing Opérationnel / Business Development",
                      "Stage Supply Chain & Logistique (H/F)",
                      "Stage Data Marketing digital", "Stage communication AI"]:
            o, reason = offer(title=title, url=f"https://e.com/{title}")
            self.assertEqual(reason, scan.REASON_NO_DOMAIN, title)

    def test_ai_buzzword_titles_outside_data_are_rejected(self):
        """Bruit réel du run du 25/11/2026 : « IA » dans un titre non data."""
        for title in [
            "Stage - Chargée SEO et GEO SEO, Optimisation Digitale IA F/H",
            "Stage - Développeur/se java augmenté/e par l'IA - Energy & Telecom - Lyon",
            "Animation Commerciale et transformation IA - Stage - F/H",
            "Stagiaire documentation par IA H/F",
            "Stage - Développeur(se) Full Stack - Energie & Télécom - Nantes",
            "Stage – Salesforce, Transformation Digitale & IA – F/H",
        ]:
            o, reason = offer(title=title, url=f"https://e.com/{title}")
            self.assertEqual(reason, scan.REASON_NO_DOMAIN, title)
        # Mots pénalisés dans le titre seulement : une vraie offre data passe.
        for title in ["Stage Data Engineer Java / Spark",
                      "Stage Data Analyst – direction commerciale"]:
            o, reason = offer(title=title, url=f"https://e.com/{title}",
                              description="Documentation Salesforce et Java.")
            self.assertIsNone(reason, title)

    def test_recruitment_is_penalised_in_title_only(self):
        o, reason = offer(
            title="Stage Data Engineer",
            description="Notre processus de recrutement : deux entretiens. Spark, SQL.",
        )
        self.assertIsNone(reason)
        # Titre : « data engineer » + « stage data » (2 × 3) ; description :
        # spark + sql (2 × 1) ; aucune pénalité pour « recrutement ».
        self.assertEqual(o["domain_scores"]["data_ia"], 2 * 3 + 2)

    def test_description_alone_needs_three_distinct_positive_words(self):
        two, reason = offer(title="Stage assistant projet",
                            description="Reporting Power BI et requêtes SQL.")
        self.assertEqual(reason, scan.REASON_NO_DOMAIN)
        three, reason = offer(title="Stage assistant projet",
                              description="Pipelines ETL, SQL et Power BI.",
                              url="https://e.com/three")
        self.assertIsNone(reason)
        self.assertEqual(three["domains"], ["data_ia"])

    def test_marketing_communication_domain_catches_real_linkedin_maroc_titles(self):
        """Titres réels échantillonnés le 17/07/2026 via JobSpy (LinkedIn, région
        Maroc, hours_old=24) qui ressortaient en "aucun domaine" avant l'ajout
        de ce domaine — aucun des 16 domaines précédents ne couvrait
        marketing/communication, uniquement listé en négatif ailleurs."""
        real_titles = [
            "Stagiaire Activations & Marketing",
            "Stagiaire en Marketing Digital",
            "Digital Marketing Intern",
        ]
        for title in real_titles:
            matched, scores = scan.score_offer_domains(
                title, "", scan.DOMAINS, ALL_DOMAIN_IDS)
            self.assertIn("marketing_communication", matched, title)
            # Domaine désactivé : ces titres ne passent plus en usage perso.
            self.assertEqual(scan.score_offer_domains(title, "")[0], [], title)

    def test_data_analysis_and_business_developer_variants_are_covered(self):
        """Autres lacunes de vocabulaire trouvées dans le même échantillon réel :
        variantes de titres non couvertes par les mots-clés existants."""
        matched, _ = scan.score_offer_domains(
            "Stagiaire élève ingénieur Data analysis", "", scan.DOMAINS, ALL_DOMAIN_IDS
        )
        self.assertIn("data_ia", matched)
        matched, _ = scan.score_offer_domains(
            "Stage Pré-Embauche - Business Developer", "", scan.DOMAINS, ALL_DOMAIN_IDS
        )
        self.assertIn("commerce_bizdev", matched)
        matched, _ = scan.score_offer_domains(
            "Stagiaire People & Culture", "", scan.DOMAINS, ALL_DOMAIN_IDS
        )
        self.assertIn("rh", matched)

    def test_linkedin_plan_covers_every_active_query(self):
        for active_ids in (None, ALL_DOMAIN_IDS):
            plan = scan.linkedin_queries(active_ids=active_ids)
            queries = scan.active_queries(active_ids=active_ids)
            self.assertEqual(set(plan), set(queries))
            self.assertEqual(len(plan), len(queries))
        for domain in scan.DOMAINS.values():
            self.assertTrue(set(domain["queries"]).issubset(plan))
        self.assertEqual(scan.linkedin_queries(), scan.DOMAINS["data_ia"]["queries"])

    def test_single_data_domain_has_nominal_budget_and_all_linkedin_queries(self):
        with mock.patch.dict(os.environ, {"RADAR_DOMAINS": "data_ia"}):
            active_ids = scan.enabled_domain_ids()
            budget = scan.calculate_scan_budget(
                len(active_ids), len(scan.enabled_region_ids())
            )
            self.assertEqual(active_ids, ["data_ia"])
            self.assertEqual(budget.scale, 1.0)
            self.assertEqual(budget.results_wanted, scan.JOBSPY_RESULTS_WANTED)
            self.assertEqual(
                scan.linkedin_queries(active_ids=active_ids),
                scan.DOMAINS["data_ia"]["queries"],
            )

    def test_title_positive_and_description_negative_score(self):
        domain = {"positive": ["signal fort"], "negative": ["risque voisin"]}
        score = scan.score_domain(
            "Stage signal fort", "Risque voisin répété : risque voisin", domain
        )
        self.assertEqual(score, scan.TITLE_POSITIVE_WEIGHT
                         + scan.DESCRIPTION_NEGATIVE_WEIGHT)
        self.assertEqual(score, 2)

    def test_offer_can_match_two_active_domains(self):
        catalog = {key: scan.DOMAINS[key] for key in ("data_ia", "finance")}
        candidate, reason = scan.make_offer(
            "Stage Data Analyst Finance", "ACME", "Paris",
            "https://example.com/multi", "Test", TODAY,
            domains=catalog, active_ids=["data_ia", "finance"],
        )
        self.assertIsNone(reason)
        self.assertEqual(set(candidate["domains"]), {"data_ia", "finance"})
        self.assertGreaterEqual(candidate["domain_scores"]["data_ia"],
                                scan.DOMAIN_SCORE_THRESHOLD)
        self.assertGreaterEqual(candidate["domain_scores"]["finance"],
                                scan.DOMAIN_SCORE_THRESHOLD)

    def test_no_active_domain_returns_explicit_reason(self):
        catalog = {"dev_web": scan.DOMAINS["dev_web"]}
        candidate, reason = scan.make_offer(
            "Stage assistant administratif", "ACME", "Paris",
            "https://example.com/no-domain", "Test", TODAY,
            domains=catalog, active_ids=["dev_web"],
        )
        self.assertIsNone(candidate)
        self.assertEqual(reason, "aucun domaine")

    def test_disabled_domain_query_is_never_executed(self):
        import pandas as pd

        catalog = {
            "active": {
                "label": "Actif", "group": "Test", "queries": ["stage actif"],
                "positive": ["actif"], "negative": ["inactif"], "enabled": True,
            },
            "disabled": {
                "label": "Inactif", "group": "Test", "queries": ["stage interdit"],
                "positive": ["interdit"], "negative": ["actif"], "enabled": False,
            },
        }
        calls = []

        def fake_scrape(**kwargs):
            calls.append(kwargs["search_term"])
            return pd.DataFrame()

        with mock.patch.object(scan, "DOMAINS", catalog), \
             mock.patch.dict(os.environ, {"RADAR_DOMAINS": ""}):
            scan.fetch_jobspy_site("indeed", "Indeed", scrape_fn=fake_scrape)
        self.assertEqual(calls, ["stage actif"])

    def test_exact_queries_are_deduplicated(self):
        base = {
            "label": "A", "group": "Test", "queries": ["stage commun", "stage a"],
            "positive": ["a"], "negative": ["x"], "enabled": True,
        }
        other = dict(base, label="B", queries=["stage commun", "stage b"])
        catalog = {"a": base, "b": other}
        self.assertEqual(scan.active_queries(catalog, ["a", "b"]),
                         ["stage commun", "stage a", "stage b"])


class TestRegions(unittest.TestCase):
    def test_catalog_country_names_and_default_region(self):
        self.assertEqual(list(scan.REGIONS),
                         ["france", "belgique", "luxembourg", "maroc"])
        self.assertEqual(
            {region_id: region["country_indeed"]
             for region_id, region in scan.REGIONS.items()},
            {"france": "France", "belgique": "Belgium",
             "luxembourg": "Luxembourg", "maroc": "Morocco"},
        )
        with mock.patch.dict(os.environ, {"RADAR_REGIONS": ""}):
            self.assertEqual(scan.enabled_region_ids(), ["france", "maroc"])
        self.assertFalse(scan.REGIONS["belgique"]["enabled"])
        self.assertFalse(scan.REGIONS["luxembourg"]["enabled"])

    def test_personal_scope_budget_is_nominal(self):
        with mock.patch.dict(os.environ, {"RADAR_DOMAINS": "", "RADAR_REGIONS": ""}):
            budget = scan.calculate_scan_budget(
                len(scan.enabled_domain_ids()), len(scan.enabled_region_ids()))
        self.assertEqual(budget.combination_count, 2)
        self.assertFalse(budget.is_high)
        self.assertEqual(budget.scale, 1.0)
        self.assertEqual(budget.results_wanted, scan.JOBSPY_RESULTS_WANTED)

    def test_morocco_linkedin_adds_english_variants_only_on_linkedin(self):
        import pandas as pd

        calls = []

        def fake_scrape(**kwargs):
            calls.append(kwargs)
            return pd.DataFrame()

        # Les variantes anglaises mesurées le 25/11/2026 (« data engineer
        # intern », « data internship », « PFE data ») ont rapporté 0 offre
        # acceptée pour ~50 s de scan : retirées, le mécanisme reste.
        self.assertEqual(scan.REGIONS["maroc"]["linkedin_extra_queries"], [])
        maroc = dict(scan.REGIONS["maroc"],
                     linkedin_extra_queries=["data engineer intern", "stage data engineer"])
        scan.fetch_jobspy_site("linkedin", "LinkedIn", scrape_fn=fake_scrape,
                               region_id="maroc", region_config=maroc)
        terms = [call["search_term"] for call in calls]
        self.assertEqual(terms[:9], scan.DOMAINS["data_ia"]["queries"])
        # Ajoutée en fin de plan, doublon exact ignoré.
        self.assertEqual(terms[9:], ["data engineer intern"])
        self.assertTrue(all(call["location"] == "Morocco" for call in calls))

        calls.clear()
        scan.fetch_jobspy_site("linkedin", "LinkedIn", scrape_fn=fake_scrape)
        self.assertEqual([c["search_term"] for c in calls],
                         scan.DOMAINS["data_ia"]["queries"])

        calls.clear()
        scan.fetch_jobspy_site("indeed", "Indeed", scrape_fn=fake_scrape,
                               region_id="maroc", region_config=maroc)
        self.assertEqual([c["search_term"] for c in calls],
                         scan.DOMAINS["data_ia"]["queries"])
        self.assertEqual(calls[0]["country_indeed"], "Morocco")

    def test_morocco_offer_without_city_is_located_in_morocco(self):
        import pandas as pd

        frame = pd.DataFrame([{
            "title": "Stage PFE Data Engineer", "company": "ACME Casablanca",
            "city": float("nan"), "state": None, "job_url": "https://e.com/ma",
            "date_posted": TODAY, "description": "",
        }])
        offers, status = scan.fetch_jobspy_site(
            "linkedin", "LinkedIn",
            scrape_fn=lambda **kw: frame if kw["search_term"] == "stage data engineer"
            else frame.iloc[0:0],
            region_id="maroc", region_config=scan.REGIONS["maroc"],
        )
        self.assertEqual(offers[0]["location"], "Maroc")
        self.assertEqual(offers[0]["regions"], ["maroc"])
        self.assertEqual(status.query_yield["stage data engineer"],
                         {"raw": 1, "accepted": 1})

    def test_french_national_sources_are_not_applicable_in_morocco(self):
        with mock.patch.object(scan.requests, "post") as post, \
             mock.patch.object(scan.requests, "get") as get, \
             mock.patch.dict(os.environ, {"ADZUNA_APP_ID": "x", "ADZUNA_APP_KEY": "y",
                                          "FT_CLIENT_ID": "x", "FT_CLIENT_SECRET": "y"}):
            statuses = [
                scan.fetch_apec_all("maroc")[1],
                scan.fetch_adzuna_all("maroc")[1],
                scan.fetch_france_travail_all("maroc")[1],
                scan.fetch_the_muse_all("maroc")[1],
            ]
        post.assert_not_called()
        get.assert_not_called()
        for status in statuses:
            self.assertEqual(status.status, "not_applicable_region", status.source)
            self.assertEqual(status.region_label, "Maroc")

    def test_moroccan_pfe_titles_pass_the_contract_filter(self):
        for title in ["Stage PFE Data Engineer", "Stagiaire PFE Data Analyst",
                      "Projet de fin d'études : Data Engineering",
                      "Projet de Fin d’Etudes - Big Data", "PFE - Data pipelines Spark"]:
            self.assertTrue(scan.TITLE_MUST_MATCH.search(title), title)
            o, reason = offer(title=title, url=f"https://e.com/{title}",
                              region_id="maroc")
            self.assertIsNone(reason, title)
        self.assertIsNone(scan.TITLE_MUST_MATCH.search("Data Engineer CDI"))

    def test_belgium_jobspy_offer_is_tagged_belgium(self):
        import pandas as pd

        calls = []
        frame = pd.DataFrame([{
            "title": "Stage Data Engineer", "company": "ACME",
            "city": "Bruxelles", "state": "", "job_url": "https://e.com/be",
            "date_posted": TODAY, "description": "ETL et data warehouse",
        }])

        def fake_scrape(**kwargs):
            calls.append(kwargs)
            return frame if len(calls) == 1 else frame.iloc[0:0]

        offers, status = scan.fetch_jobspy_site(
            "indeed", "Indeed", scrape_fn=fake_scrape,
            region_id="belgique", region_config=scan.REGIONS["belgique"],
            domains={"data_ia": scan.DOMAINS["data_ia"]},
            active_ids=["data_ia"],
        )
        self.assertEqual(calls[0]["location"], "Belgium")
        self.assertEqual(calls[0]["country_indeed"], "Belgium")
        self.assertEqual(offers[0]["regions"], ["belgique"])
        self.assertEqual(status.region_id, "belgique")

    def test_linkedin_and_indeed_use_region_native_locations(self):
        import pandas as pd

        calls = []

        def fake_scrape(**kwargs):
            calls.append(kwargs)
            return pd.DataFrame()

        data_domain = {"data_ia": scan.DOMAINS["data_ia"]}
        scan.fetch_jobspy_site(
            "linkedin", "LinkedIn", scrape_fn=fake_scrape,
            region_id="belgique", region_config=scan.REGIONS["belgique"],
            domains=data_domain, active_ids=["data_ia"],
        )
        self.assertEqual(calls[0]["location"], "Belgium")
        self.assertEqual(calls[0]["country_indeed"], "Belgium")

        calls.clear()
        scan.fetch_jobspy_site(
            "indeed", "Indeed", scrape_fn=fake_scrape,
            region_id="maroc", region_config=scan.REGIONS["maroc"],
            domains=data_domain, active_ids=["data_ia"],
        )
        self.assertEqual(calls[0]["location"], "Morocco")
        self.assertEqual(calls[0]["country_indeed"], "Morocco")

    def test_french_only_sources_are_not_called_abroad(self):
        with mock.patch.object(scan.requests, "post") as post:
            offers, apec = scan.fetch_apec_all("belgique")
        self.assertEqual(offers, [])
        self.assertEqual(apec.status, "not_applicable_region")
        post.assert_not_called()

        with mock.patch.object(scan, "ft_token") as token, \
             mock.patch.object(scan.requests, "get") as get:
            offers, ft = scan.fetch_france_travail_all("luxembourg")
        self.assertEqual(offers, [])
        self.assertEqual(ft.status, "not_applicable_region")
        token.assert_not_called()
        get.assert_not_called()

    def test_duplicate_remote_offer_merges_regions(self):
        selected = {key: scan.REGIONS[key] for key in ("france", "belgique")}

        def fake_fetch(region_id, _region_config):
            candidate, reason = offer(
                url="https://e.com/remote", region_id=region_id,
                regions=selected,
            )
            self.assertIsNone(reason)
            status = scan.source_status("Remote", region_id, selected,
                                        status="success", accepted_count=1)
            return [candidate], status

        offers, statuses = scan.run_source_across_regions(
            "Remote", fake_fetch, selected)
        self.assertEqual(len(offers), 1)
        self.assertEqual(set(offers[0]["regions"]), {"france", "belgique"})
        self.assertEqual(len(statuses), 2)

    def test_high_combination_budget_never_scales_results(self):
        budget = scan.calculate_scan_budget(4, 2)
        self.assertTrue(budget.is_high)
        self.assertEqual(budget.combination_count, 8)
        self.assertEqual(budget.scale, 1.0)
        self.assertEqual(budget.results_wanted, scan.JOBSPY_RESULTS_WANTED)


class TestParseOfferDate(unittest.TestCase):
    def test_iso_with_z(self):
        parsed = scan.parse_offer_date("2026-07-10T11:03:56.000Z")
        self.assertEqual(parsed.date().isoformat(), "2026-07-10")

    def test_date_only(self):
        self.assertEqual(scan.parse_offer_date(TODAY).date().isoformat(), TODAY)

    def test_invalid(self):
        self.assertIsNone(scan.parse_offer_date("pas une date"))
        self.assertIsNone(scan.parse_offer_date(None))
        self.assertIsNone(scan.parse_offer_date("NaT"))

    def test_fresh_relative_french_and_english(self):
        now = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(scan.parse_offer_date("à l'instant", now), now)
        self.assertEqual(scan.parse_offer_date("aujourd'hui", now), now)
        self.assertEqual(scan.parse_offer_date("Il y a 1 heure", now),
                         now - timedelta(hours=1))
        self.assertEqual(scan.parse_offer_date("Just posted", now), now)
        self.assertEqual(scan.parse_offer_date("2 hours ago", now),
                         now - timedelta(hours=2))


class TestMakeOffer(unittest.TestCase):
    def test_recent_internship_accepted(self):
        o, reason = offer()
        self.assertIsNone(reason)
        self.assertEqual(o["date"], TODAY)
        self.assertEqual(o["source"], "LinkedIn")
        self.assertEqual(o["regions"], ["france"])

    def test_too_old_rejected(self):
        with mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
            o, reason = offer(date=TOO_OLD)
        self.assertIsNone(o)
        self.assertEqual(reason, scan.REASON_TOO_OLD)
        self.assertIn(f"date_brute='{TOO_OLD}'", stderr.getvalue())
        self.assertIn("date_parsee=", stderr.getvalue())

    def test_sliding_24_hour_boundaries_and_utc_normalization(self):
        now = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
        cases = [
            (now - timedelta(hours=23, minutes=59), True),
            (now - timedelta(hours=24), True),
            (now - timedelta(hours=24, minutes=1), False),
        ]
        for index, (published, accepted) in enumerate(cases):
            candidate, reason = offer(
                date=published.isoformat(), now=now,
                url=f"https://example.com/boundary/{index}",
            )
            self.assertEqual(candidate is not None, accepted)
            self.assertEqual(reason, None if accepted else scan.REASON_TOO_OLD)

        naive = (now - timedelta(hours=1)).replace(tzinfo=None).isoformat()
        candidate, reason = offer(date=naive, now=now,
                                  url="https://example.com/naive")
        self.assertIsNone(reason)
        self.assertEqual(candidate["sort_date"],
                         (now - timedelta(hours=1)).isoformat())

        offset_date = (now - timedelta(hours=2)).astimezone(
            timezone(timedelta(hours=2))
        ).isoformat()
        candidate, reason = offer(date=offset_date, now=now,
                                  url="https://example.com/offset")
        self.assertIsNone(reason)
        self.assertEqual(candidate["sort_date"],
                         (now - timedelta(hours=2)).isoformat())

    def test_missing_and_unreadable_dates_are_kept(self):
        for date in (None, "", "à confirmer"):
            with mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
                o, reason = offer(date=date, url=f"https://example.com/{date!r}")
            self.assertIsNone(reason)
            self.assertFalse(o["date_known"])
            self.assertEqual(o["date"], "")
            self.assertIn("DATE_INCONNUE", stderr.getvalue())

    def test_reject_missing_date_rejects_instead_of_keeping(self):
        for date in (None, "", "à confirmer"):
            o, reason = offer(date=date, reject_missing_date=True,
                              url=f"https://example.com/reject/{date!r}")
            self.assertIsNone(o)
            self.assertEqual(reason, scan.REASON_UNVERIFIABLE_RECENT_DATE)
        # Comportement par défaut inchangé pour toutes les autres sources.
        o, reason = offer(date=None, url="https://example.com/default")
        self.assertIsNone(reason)
        self.assertFalse(o["date_known"])

    def test_fresh_relative_date_is_accepted(self):
        o, reason = offer(date="à l'instant")
        self.assertIsNone(reason)
        self.assertTrue(o["date_known"])
        self.assertEqual(o["date"], TODAY)

    def test_not_internship_rejected(self):
        o, reason = offer(title="Data Engineer CDI")
        self.assertIsNone(o)
        self.assertEqual(reason, scan.REASON_NOT_INTERNSHIP)

    def test_ml_accepted(self):
        o, reason = offer(title="Stage Machine Learning Engineer")
        self.assertIsNone(reason)
        self.assertIsNotNone(o)

    def test_data_science_accepted(self):
        o, reason = offer(title="Stagiaire Data Scientist NLP")
        self.assertIsNone(reason)
        self.assertIsNotNone(o)

    def test_off_profile_rejected(self):
        o, reason = offer(title="Stage assistant chef de projet digital")
        self.assertIsNone(o)
        self.assertEqual(reason, scan.REASON_NO_DOMAIN)

    def test_description_cannot_rescue_rh_title(self):
        # Une exclusion forte du titre reste prioritaire sur la description.
        o, reason = offer(
            title="Stagiaire RH Analytics F/H", source="APEC",
            confirmed_internship=True,
            description="Missions : Data Analytics, Business Intelligence, Power BI.",
        )
        self.assertIsNone(o)
        self.assertEqual(reason, scan.REASON_NO_DOMAIN)

    def test_generic_rh_offer_matches_rh_domain(self):
        o, reason = offer_all_domains(
            title="Stagiaire RH généraliste F/H", source="APEC",
            confirmed_internship=True,
            description="Gestion administrative du personnel, paie et recrutement.",
        )
        self.assertIsNone(reason)
        self.assertIn("rh", o["domains"])

    def test_description_can_confirm_data_profile(self):
        o, reason = offer(
            title="Stage ingénieur logiciel",
            description="Construction ETL, Airflow et data warehouse pour les équipes.",
        )
        self.assertIsNone(reason)
        self.assertIsNotNone(o)

    def test_nan_company_defaults(self):
        o, _ = offer(company="nan", location=None)
        self.assertEqual(o["company"], "Entreprise non précisée")
        self.assertEqual(o["location"], "France")


class TestDomainTargetingStrict(unittest.TestCase):
    """Les offres doivent être rattachées à un domaine actif explicite et les
    titres génériques sans signal métier doivent rester rejetés."""

    def test_gestion_projet_qualite_rejected(self):
        o, reason = offer(
            title="Amélioration des Outils et Processus de Gestion de Projet Qualité F/H",
            company="Proaxian", source="APEC", confirmed_internship=True,
            description="Suivi qualité, reporting, Power BI et tableaux de bord.",
        )
        self.assertIsNone(o)
        self.assertEqual(reason, scan.REASON_NO_DOMAIN)

    def test_auditeur_junior_matches_audit_domain(self):
        o, reason = offer_all_domains(
            title="Auditeur junior F/H", company="Proaxian",
            source="APEC", confirmed_internship=True,
            description="Missions d'audit avec restitution sous Power BI.",
        )
        self.assertIsNone(reason)
        self.assertIn("audit", o["domains"])

    def test_finance_controle_gestion_matches_business_domain(self):
        o, reason = offer_all_domains(
            title="Stagiaire Finance et Contrôle de Gestion H/F",
            company="SONEMA", source="Indeed",
            description="Tableaux de bord Power BI, analyses budgétaires.",
        )
        self.assertIsNone(reason)
        self.assertIn("compta_gestion", o["domains"])

    def test_business_data_analyst_crm_rejected(self):
        # Cas limite tranché selon la consigne : CRM est un domaine
        # commercial/marketing hors du profil Data Engineering ciblé.
        o, reason = offer(title="Stage – Business Data Analyst CRM (H/F)",
                          company="Prosol", source="Indeed")
        self.assertIsNone(o)
        self.assertEqual(reason, scan.REASON_NO_DOMAIN)

    def test_real_data_offers_still_accepted(self):
        for title in [
            "STAGE - DATA ENGINEER F/H",
            "Stage - Data Analyst - Paris - H/F",
            "Stage Business Intelligence / Power BI",
            "Stage ingénieur Big Data Spark",
            "Stage ETL Talend et Data Warehouse",
            "Stage Machine Learning / NLP",
            "Stage IA Python et SQL",
        ]:
            o, reason = offer(title=title)
            self.assertIsNone(reason, f"'{title}' aurait dû passer : {reason}")
            self.assertEqual(o["title"], title)

    def test_carve_outs(self):
        # « qualité » est exclu… sauf data quality explicite.
        o, reason = offer(title="Stage Data Quality Analyst H/F")
        self.assertIsNone(reason)
        # « marketing » est exclu… sauf marketing analytics (avec missions
        # data réelles en description).
        o, reason = offer(
            title="Stage Marketing Analytics H/F",
            description="Missions : Data Analytics, dashboards Power BI.",
        )
        self.assertIsNone(reason)
        # La finance est un domaine à part entière du catalogue complet…
        o, reason = offer_all_domains(title="Stage analyste financier Power BI")
        self.assertIsNone(reason)
        self.assertIn("finance", o["domains"])
        # … mais hors du périmètre perso Data.
        o, reason = offer(title="Stage analyste financier Power BI")
        self.assertEqual(reason, scan.REASON_NO_DOMAIN)
        # « juridique / droit » exclu (bruit APEC constaté).
        o, reason = offer(title="Stagiaire en droit du numérique",
                          confirmed_internship=True)
        self.assertEqual(reason, scan.REASON_NO_DOMAIN)

    def test_paid_media_and_fpa_rejected(self):
        # Bruit constaté au premier run après fix : publicité et finance
        # repêchées par une mention Power BI en description.
        o, reason = offer(title="Assistant Consultant Paid Media - SEA et SMA F/H",
                          source="APEC", confirmed_internship=True,
                          description="Reporting des campagnes sous Power BI.")
        self.assertEqual(reason, scan.REASON_NO_DOMAIN)
        o, reason = offer(title="FP&A Analyst Intern", source="Indeed",
                          description="Dashboards Power BI, data analytics.")
        self.assertEqual(reason, scan.REASON_NO_DOMAIN)

    def test_generic_words_do_not_match(self):
        # Mots génériques (gestion, outils, processus, analyse) : jamais
        # suffisants pour matcher le ciblage.
        o, reason = offer(title="Stage gestion et amélioration des processus",
                          description="Analyse des outils internes.")
        self.assertIsNone(o)
        self.assertEqual(reason, scan.REASON_NO_DOMAIN)

    def test_description_does_not_rescue_non_technical_titles(self):
        # Faux positifs observés au benchmark : une mention SQL/Power BI dans
        # la description ne suffit plus si le titre n'est pas technique.
        for title in [
            "Stage - Analyste Excellence Opérationnelle (F/M/X)",
            "Chargée Relations Écoles & Universités - Stage",
            "Junior Business Analyst - Stage (H/F/N)",
            "Internship - Développement de Carrières & Projets Alumni",
            "Consultant Digital Engineering - Stage",
        ]:
            o, reason = offer(
                title=title,
                description="Reporting Power BI et extraction SQL de données.",
            )
            self.assertIsNone(o, title)
            self.assertEqual(reason, scan.REASON_NO_DOMAIN, title)

        o, reason = offer_all_domains(
            title="Stagiaire Cybersécurité DevSecops",
            description="Analyse des risques et sécurité des pipelines CI/CD.",
        )
        self.assertIsNone(reason)
        self.assertIn("cybersecurite", o["domains"])

    def test_business_analyst_neighbor_remains_rejected(self):
        candidate, reason = offer(
            title="Junior Business Analyst - Stage (H/F/N)",
            description=("Reporting Power BI, ETL, data warehouse et "
                         "business intelligence."),
        )
        self.assertIsNone(candidate)
        self.assertEqual(reason, scan.REASON_NO_DOMAIN)

    def test_description_rescues_only_technical_generic_title(self):
        # data_ia exige désormais 3 mots positifs en description seule.
        o, reason = offer_all_domains(
            title="Stage ingénieur logiciel",
            description="Missions ETL et data warehouse.",
        )
        self.assertEqual(reason, scan.REASON_NO_DOMAIN)
        o, reason = offer(
            title="Stage ingénieur logiciel",
            description="Missions ETL, Spark et data warehouse.",
        )
        self.assertIsNone(reason)
        self.assertEqual(o["domains"], ["data_ia"])

    def test_ai_requires_technical_context(self):
        o, reason = offer(title="Stage communication IA")
        self.assertIsNone(o)
        self.assertEqual(reason, scan.REASON_NO_DOMAIN)
        o, reason = offer(title="Stage IA", description="Modèles Python et algorithmes.")
        self.assertIsNone(reason)
        self.assertIsNotNone(o)


class TestDedup(unittest.TestCase):
    def test_same_offer_same_id(self):
        a, _ = offer()
        b, _ = offer()
        self.assertEqual(a["id"], b["id"])

    def test_register_offer_counts_duplicates(self):
        st = scan.SourceStatus(source="Test")
        offers = {}
        a, r = offer()
        scan.register_offer(st, offers, a, r)
        scan.register_offer(st, offers, a, r)
        self.assertEqual(st.accepted_count, 1)
        self.assertEqual(st.reject_reasons.get(scan.REASON_DUPLICATE), 1)
        self.assertEqual(st.duplicate_count, 1)

    def test_unknown_date_counted_when_accepted(self):
        st = scan.SourceStatus(source="Test")
        offers = {}
        candidate, reason = offer(date=None)
        scan.register_offer(st, offers, candidate, reason)
        self.assertEqual(st.unknown_date_count, 1)

    def test_duplicate_merges_domain_labels(self):
        catalog = {key: scan.DOMAINS[key] for key in ("data_ia", "finance")}
        first, _ = scan.make_offer(
            "Stage Data Analyst Finance", "ACME", "Paris", "https://e.com/multi",
            "Test", TODAY, domains=catalog, active_ids=["data_ia"],
        )
        second, _ = scan.make_offer(
            "Stage Data Analyst Finance", "ACME", "Paris", "https://e.com/multi",
            "Test", TODAY, domains=catalog, active_ids=["finance"],
        )
        st = scan.SourceStatus(source="Test")
        offers = {}
        scan.register_offer(st, offers, first, None)
        scan.register_offer(st, offers, second, None)
        self.assertEqual(set(next(iter(offers.values()))["domains"]),
                         {"data_ia", "finance"})


class TestSort(unittest.TestCase):
    def test_three_known_dates_newest_first(self):
        old, _ = offer(title="Stage ETL ancien", date=RECENT_3H, url="https://e.com/1")
        middle, _ = offer(title="Stage ETL milieu", date=RECENT_2H, url="https://e.com/2")
        new, _ = offer(title="Stage ETL récent", date=RECENT_1H, url="https://e.com/3")
        ordered = scan.sort_offers([old, new, middle])
        self.assertEqual([o["title"] for o in ordered],
                         ["Stage ETL récent", "Stage ETL milieu", "Stage ETL ancien"])

    def test_unknown_date_is_kept_and_visible_first(self):
        known, _ = offer(date=RECENT_1H, url="https://e.com/known")
        unknown, _ = offer(date=None, url="https://e.com/unknown")
        ordered = scan.sort_offers([known, unknown])
        self.assertEqual(ordered[0]["id"], unknown["id"])


class TestCircuitBreaker(unittest.TestCase):
    def test_blocked_403_stops_hammering(self):
        calls = []

        def fake_scrape(**kwargs):
            calls.append(kwargs["search_term"])
            raise Exception("bad response status code: 403")

        offers, st = scan.fetch_jobspy_site("indeed", "Indeed", scrape_fn=fake_scrape)
        self.assertEqual(len(calls), scan.MAX_CONSECUTIVE_BLOCKS)
        self.assertEqual(st.status, "blocked")
        self.assertEqual(st.http_error, "HTTP 403")
        self.assertEqual(offers, [])
        self.assertIn("circuit breaker", st.note)

    def test_blocked_429_detected(self):
        def fake_scrape(**kwargs):
            raise Exception("429 Response - Blocked for too many requests")

        _, st = scan.fetch_jobspy_site("indeed", "Indeed", scrape_fn=fake_scrape)
        self.assertEqual(st.status, "blocked")
        self.assertEqual(st.http_error, "HTTP 429")

    def test_non_block_error_is_error_status(self):
        def fake_scrape(**kwargs):
            raise Exception("connexion interrompue")

        _, st = scan.fetch_jobspy_site("indeed", "Indeed", scrape_fn=fake_scrape)
        self.assertEqual(st.status, "error")

    def test_swallowed_jobspy_403_is_detected(self):
        import pandas as pd
        calls = 0

        def fake_scrape(**_kwargs):
            nonlocal calls
            calls += 1
            logging.getLogger("JobSpy:Indeed").error(
                "Indeed: response status code 403")
            return pd.DataFrame()

        offers, st = scan.fetch_jobspy_site("indeed", "Indeed", scrape_fn=fake_scrape)
        self.assertEqual(calls, scan.MAX_CONSECUTIVE_BLOCKS)
        self.assertEqual(st.status, "blocked")
        self.assertEqual(st.http_error, "HTTP 403")
        self.assertEqual(offers, [])

    def test_swallowed_generic_jobspy_error_is_not_reported_as_empty(self):
        import pandas as pd

        def fake_scrape(**_kwargs):
            logging.getLogger("JobSpy:Indeed").error(
                "Indeed: Error encountered in API response")
            return pd.DataFrame()

        offers, st = scan.fetch_jobspy_site("indeed", "Indeed", scrape_fn=fake_scrape)
        self.assertEqual(st.status, "error")
        self.assertIn("API response", st.http_error)
        self.assertEqual(offers, [])


class TestJobspyAccounting(unittest.TestCase):
    def test_raw_accepted_rejected_counts(self):
        import pandas as pd

        df = pd.DataFrame([
            {"title": "Stage Data Engineer", "company": "ACME", "city": "Paris",
             "state": "", "job_url": "https://e.com/1", "date_posted": TODAY,
             "description": ""},
            {"title": "Stage Data Engineer (vieux)", "company": "ACME", "city": "Lyon",
             "state": "", "job_url": "https://e.com/2", "date_posted": TOO_OLD,
             "description": ""},
            {"title": "Stage communication digitale", "company": "ACME", "city": "Nice",
             "state": "", "job_url": "https://e.com/3", "date_posted": TODAY,
             "description": ""},
        ])
        seen_queries = []

        def fake_scrape(**kwargs):
            seen_queries.append(kwargs)
            return df if len(seen_queries) == 1 else df.iloc[0:0]

        offers, st = scan.fetch_jobspy_site("linkedin", "LinkedIn", scrape_fn=fake_scrape)
        self.assertEqual(st.raw_count, 3)
        # JobSpy a déjà appliqué son filtre natif 24 h ; une date réduite au
        # jour par la librairie n'est pas réinterprétée comme une heure exacte.
        self.assertEqual(st.accepted_count, 2)
        self.assertEqual(st.rejected_count, 1)
        self.assertIsNone(st.reject_reasons.get(scan.REASON_TOO_OLD))
        self.assertEqual(st.reject_reasons.get(scan.REASON_NO_DOMAIN), 1)
        self.assertEqual(st.status, "success")
        self.assertEqual(len(offers), 2)
        self.assertGreaterEqual(st.pages_fetched, 1)

    def test_results_wanted_per_site(self):
        import pandas as pd
        seen = {}

        def make_fake(site):
            def fake_scrape(**kwargs):
                seen[site] = kwargs
                return pd.DataFrame()
            return fake_scrape

        scan.fetch_jobspy_site("linkedin", "LinkedIn", scrape_fn=make_fake("linkedin"))
        scan.fetch_jobspy_site("indeed", "Indeed", scrape_fn=make_fake("indeed"))
        self.assertEqual(seen["linkedin"]["results_wanted"], 1000)
        self.assertEqual(seen["indeed"]["results_wanted"], 1000)
        self.assertEqual(seen["linkedin"]["hours_old"], 24)
        self.assertEqual(seen["indeed"]["hours_old"], 24)
        self.assertEqual(seen["indeed"]["country_indeed"], "France")
        self.assertNotIn("job_type", seen["linkedin"])
        self.assertNotIn("job_type", seen["indeed"])

    def test_native_queries_and_linkedin_description_budget(self):
        import pandas as pd
        seen = []

        def fake_scrape(**kwargs):
            seen.append(kwargs)
            return pd.DataFrame()

        scan.fetch_jobspy_site("linkedin", "LinkedIn", scrape_fn=fake_scrape)
        self.assertEqual([call["search_term"] for call in seen], scan.linkedin_queries())
        self.assertEqual(
            [call["search_term"] for call in seen if call["linkedin_fetch_description"]],
            scan.linkedin_queries()[:scan.MAX_LINKEDIN_DESCRIPTION_QUERIES],
        )
        self.assertTrue(any(not call["linkedin_fetch_description"] for call in seen))
        self.assertTrue(all("f_SAL" not in str(call) for call in seen))
        self.assertTrue(all("publié au cours" not in call["search_term"].lower()
                            for call in seen))

        seen.clear()
        scan.fetch_jobspy_site("indeed", "Indeed", scrape_fn=fake_scrape)
        self.assertEqual([call["search_term"] for call in seen], scan.active_queries())


class TestRetiredNoiseSources(unittest.TestCase):
    """Glassdoor et Google Jobs ont renvoyé 0 résultat sur plusieurs runs de
    production consécutifs (bloqué 403 / réponse vide) : leur code a été
    retiré entièrement (voir l'historique Git), pas seulement désactivé."""

    def test_source_names_no_longer_include_noisy_zero_yield_sources(self):
        self.assertNotIn("Glassdoor", scan.SOURCE_NAMES)
        self.assertNotIn("Google Jobs", scan.SOURCE_NAMES)
        self.assertNotIn("Jooble", scan.SOURCE_NAMES)
        self.assertEqual(
            scan.SOURCE_NAMES,
            ["LinkedIn", "Indeed", "APEC", "Adzuna", "The Muse",
             "France Travail", "LinkedIn Posts", "Dreamjob", "Stagiaires.ma",
             "Sites Maroc"],
        )
        # APEC et The Muse (anti-bot) et Adzuna (clé) restent dans le code
        # mais ne sont ni interrogés ni affichés.
        self.assertEqual(
            scan.active_source_names(),
            ["LinkedIn", "Indeed", "France Travail", "LinkedIn Posts", "Stagiaires.ma",
             "Sites Maroc"],
        )

    def test_run_all_sources_never_threads_glassdoor_or_google(self):
        catalog = {
            "data": {
                "label": "Data", "group": "Test",
                "queries": ["stage data engineer"],
                "positive": ["data engineer"], "negative": [],
                "enabled": True,
            }
        }
        with mock.patch("scan.fetch_jobspy_site") as fetch_mock, \
             mock.patch.object(scan, "fetch_apec_all",
                               return_value=([], scan.SourceStatus(source="APEC"))), \
             mock.patch.object(scan, "fetch_adzuna_all",
                               return_value=([], scan.SourceStatus(source="Adzuna"))), \
             mock.patch.object(scan, "fetch_the_muse_all",
                               return_value=([], scan.SourceStatus(source="The Muse"))), \
             mock.patch.object(scan, "fetch_linkedin_posts_all",
                               return_value=([], scan.SourceStatus(source="LinkedIn Posts"))), \
             mock.patch.object(scan, "fetch_france_travail_all",
                               return_value=([], scan.SourceStatus(source="France Travail"))):
            fetch_mock.return_value = ([], scan.SourceStatus(source="LinkedIn"))
            scan.run_all_sources(
                domains=catalog, active_domain_ids=["data"],
                regions={"france": scan.REGIONS["france"]},
                active_region_ids=["france"],
            )
            sites_called = {call.args[0] for call in fetch_mock.call_args_list}
        self.assertEqual(sites_called, {"linkedin", "indeed"})
        self.assertNotIn("glassdoor", sites_called)
        self.assertNotIn("google", sites_called)


class TestApecNativeFilters(unittest.TestCase):
    def test_payload_has_native_filters(self):
        p = scan.apec_search_payload("data")
        # Ces codes décrivent les annonceurs, pas des conventions : entreprise,
        # cabinet, agence, SSII/ESN et partenaire. Le partenaire est conservé
        # car il porte aussi des offres Data fraîches.
        self.assertEqual(p["typesConvention"], [143684, 143685, 143686, 143687, 143706])
        self.assertEqual(p["typesContrat"], [597171])
        self.assertEqual(p["niveauEtude"], 20019)  # Bac+5 ; 20020 signifie > Bac+5.
        self.assertEqual(p["sorts"], [{"type": "DATE", "direction": "DESCENDING"}])


class TestSourcePagination(unittest.TestCase):
    CATALOG = {
        "data": {
            "label": "Data", "group": "Test",
            "queries": ["stage data engineer"],
            "positive": ["data engineer"], "negative": ["hors sujet"],
            "enabled": True,
        }
    }

    @staticmethod
    def apec_item(index):
        return {
            "numeroOffre": str(index), "intitule": "Stage Data Engineer",
            "nomCommercial": "ACME", "lieuTexte": "Paris",
            "datePublication": RECENT_1H, "texteOffre": "data engineer",
        }

    def apec_generic_item(self, index):
        # Titre générique et snippet hors domaine actif : déclenche le
        # fallback vers la lecture de détail.
        return {
            "numeroOffre": str(index), "intitule": "Stage polyvalent",
            "nomCommercial": "ACME", "lieuTexte": "Paris",
            "datePublication": RECENT_1H, "texteOffre": "mission générale",
        }

    def test_apec_detail_circuit_breaker_stops_after_first_403(self):
        forbidden = mock.Mock(status_code=403)
        forbidden.raise_for_status.side_effect = Exception("403 Client Error: Forbidden")
        search_response = fake_response(
            {"resultats": [self.apec_generic_item(i) for i in range(5)]}
        )
        with mock.patch.object(scan.requests, "post", return_value=search_response), \
             mock.patch.object(scan.requests, "get", return_value=forbidden) as get:
            offers, status = scan.fetch_apec_all(
                domains=self.CATALOG, active_ids=["data"]
            )
        # Un seul appel de détail est tenté : le circuit breaker évite les
        # quatre échecs identiques restants.
        self.assertEqual(get.call_count, 1)
        self.assertEqual(offers, [])
        self.assertIn("bloquées", status.note)

    def test_apec_search_circuit_breaker_stops_after_two_403(self):
        forbidden = mock.Mock(status_code=403)
        forbidden.raise_for_status.side_effect = Exception("403 Client Error: Forbidden")
        catalog = dict(self.CATALOG)
        catalog["data"] = dict(catalog["data"], queries=[
            "stage data engineer", "stage etl", "stage big data", "stage spark"])
        with mock.patch.object(scan.requests, "post", return_value=forbidden) as post:
            offers, status = scan.fetch_apec_all(domains=catalog, active_ids=["data"])
        self.assertEqual(post.call_count, scan.MAX_CONSECUTIVE_BLOCKS)
        self.assertEqual(offers, [])
        self.assertEqual(status.status, "blocked")
        self.assertEqual(status.http_error, "HTTP 403")
        self.assertIn("circuit breaker", status.note)

    def test_apec_detail_success_is_not_affected_by_breaker(self):
        # Titre générique : la description seule doit citer au moins
        # DESCRIPTION_ONLY_MIN_POSITIVES mots positifs distincts.
        catalog = {
            "data": {
                "label": "Data", "group": "Test",
                "queries": ["stage data engineer"],
                "positive": ["data engineer", "pipelines", "spark"], "negative": [],
                "enabled": True,
            }
        }
        search_response = fake_response(
            {"resultats": [self.apec_generic_item(0)]}
        )
        detail_response = mock.Mock(status_code=200)
        detail_response.raise_for_status.return_value = None
        detail_response.json.return_value = {
            "texteHtml": "<p>stage data engineer sur des pipelines Spark</p>",
        }
        with mock.patch.object(scan.requests, "post", return_value=search_response), \
             mock.patch.object(scan.requests, "get", return_value=detail_response) as get:
            offers, status = scan.fetch_apec_all(
                domains=catalog, active_ids=["data"]
            )
        self.assertEqual(get.call_count, 1)
        self.assertEqual(len(offers), 1)
        self.assertEqual(status.note, "")

    def test_apec_paginates_until_short_page(self):
        responses = [
            fake_response({"resultats": [self.apec_item(i) for i in range(100)]}),
            fake_response({"resultats": [self.apec_item(100)]}),
        ]
        with mock.patch.object(scan.requests, "post", side_effect=responses) as post:
            offers, status = scan.fetch_apec_all(
                domains=self.CATALOG, active_ids=["data"]
            )
        self.assertEqual(len(offers), 101)
        self.assertEqual(status.pages_fetched, 2)
        self.assertEqual(status.raw_count, 101)
        self.assertEqual(
            [call.kwargs["json"]["pagination"]["startIndex"]
             for call in post.call_args_list],
            [0, 100],
        )

    def test_apec_old_publication_date_is_still_accepted(self):
        # La recherche APEC ne renvoie que des offres ouvertes (filtre stage
        # serveur) : une datePublication ancienne ne doit jamais valoir rejet
        # "plus de 24 heures" (régression réelle : 181/645 rejetées ainsi à
        # chaque run, puis considérées absentes par le store).
        item = dict(self.apec_item(0), datePublication=TOO_OLD)
        response = fake_response({"resultats": [item]})
        with mock.patch.object(scan.requests, "post", return_value=response):
            offers, status = scan.fetch_apec_all(
                domains=self.CATALOG, active_ids=["data"]
            )
        self.assertEqual(len(offers), 1)
        self.assertNotIn(scan.REASON_TOO_OLD, status.reject_reasons)

    def test_adzuna_paginates_and_uses_native_one_day_filter(self):
        def item(index):
            return {
                "id": str(index), "title": "Stage Data Engineer",
                "company": {"display_name": "ACME"},
                "location": {"display_name": "Paris"},
                "redirect_url": f"https://e.com/adzuna/{index}",
                "created": RECENT_1H, "description": "data engineer",
            }

        responses = [
            fake_response({"count": 51, "results": [item(i) for i in range(50)]}),
            fake_response({"count": 51, "results": [item(50)]}),
        ]
        with mock.patch.dict(os.environ, {"ADZUNA_APP_ID": "id",
                                          "ADZUNA_APP_KEY": "key"}), \
             mock.patch.object(scan.requests, "get", side_effect=responses) as get:
            offers, status = scan.fetch_adzuna_all(
                domains=self.CATALOG, active_ids=["data"]
            )
        self.assertEqual(len(offers), 51)
        self.assertEqual(status.pages_fetched, 2)
        self.assertEqual(get.call_args_list[0].kwargs["params"]["max_days_old"], 1)
        self.assertTrue(get.call_args_list[1].args[0].endswith("/2"))

    def test_the_muse_paginates_using_page_count_and_applies_internship_filter(self):
        def item(job_id, name="Stage Data Engineer"):
            return {
                "id": job_id, "name": name,
                "company": {"name": "ACME"},
                "locations": [{"name": "Paris, France"}],
                "levels": [{"name": "Internship"}],
                "publication_date": RECENT_1H,
                "contents": "<p>data engineer</p>",
                "refs": {"landing_page": f"https://www.themuse.com/jobs/acme/{job_id}"},
            }

        responses = [
            fake_response({"page_count": 2, "page": 0,
                           "results": [item(f"job-{i}") for i in range(20)]}),
            fake_response({"page_count": 2, "page": 1,
                           "results": [item("job-20")]}),
        ]
        with mock.patch.object(scan.requests, "get",
                               side_effect=responses) as get:
            offers, status = scan.fetch_the_muse_all(
                domains=self.CATALOG, active_ids=["data"],
            )
        self.assertEqual(len(offers), 21)
        self.assertEqual(status.pages_fetched, 2)
        self.assertEqual(get.call_args_list[0].kwargs["params"]["level"],
                         "Internship")
        self.assertEqual(get.call_args_list[0].kwargs["params"]["location"],
                         "France")
        self.assertEqual(get.call_args_list[0].kwargs["params"]["page"], 0)
        self.assertEqual(get.call_args_list[1].kwargs["params"]["page"], 1)
        self.assertNotIn("api_key", get.call_args_list[0].kwargs["params"])

    def test_the_muse_stops_when_page_count_reached(self):
        response = fake_response({
            "page_count": 1, "page": 0,
            "results": [{
                "id": "solo", "name": "Stage Data Engineer",
                "company": {"name": "ACME"},
                "locations": [{"name": "Paris, France"}],
                "levels": [{"name": "Internship"}],
                "publication_date": RECENT_1H,
                "contents": "data engineer",
                "refs": {"landing_page": "https://www.themuse.com/jobs/acme/solo"},
            }],
        })
        with mock.patch.object(scan.requests, "get",
                               return_value=response) as get:
            offers, status = scan.fetch_the_muse_all(
                domains=self.CATALOG, active_ids=["data"],
            )
        self.assertEqual(len(offers), 1)
        self.assertEqual(get.call_count, 1)
        self.assertEqual(status.status, "success")

    def test_the_muse_old_publication_date_is_still_accepted(self):
        # The Muse ne propose pas de hours_old : son catalogue "Internship"
        # reflète des offres toujours ouvertes indépendamment de leur date de
        # publication d'origine. Une offre à ce jour ne doit jamais être
        # rejetée "plus de 24 heures" (régression réelle constatée en
        # production : 100 % des offres étaient rejetées ainsi).
        response = fake_response({
            "page_count": 1, "page": 0,
            "results": [{
                "id": "old", "name": "Stage Data Engineer",
                "company": {"name": "ACME"},
                "locations": [{"name": "Paris, France"}],
                "levels": [{"name": "Internship"}],
                "publication_date": TOO_OLD,
                "contents": "data engineer",
                "refs": {"landing_page": "https://www.themuse.com/jobs/acme/old"},
            }],
        })
        with mock.patch.object(scan.requests, "get", return_value=response):
            offers, status = scan.fetch_the_muse_all(
                domains=self.CATALOG, active_ids=["data"],
            )
        self.assertEqual(len(offers), 1)
        self.assertEqual(status.accepted_count, 1)
        self.assertNotIn(scan.REASON_TOO_OLD, status.reject_reasons)

    def test_the_muse_uses_optional_api_key_when_present(self):
        response = fake_response({"page_count": 1, "page": 0, "results": []})
        with mock.patch.dict(os.environ, {"THEMUSE_API_KEY": "muse-key"}), \
             mock.patch.object(scan.requests, "get",
                               return_value=response) as get:
            scan.fetch_the_muse_all(domains=self.CATALOG, active_ids=["data"])
        self.assertEqual(get.call_args.kwargs["params"]["api_key"], "muse-key")

    def test_the_muse_limited_to_france(self):
        with mock.patch.object(scan.requests, "get") as get:
            offers, status = scan.fetch_the_muse_all(region_id="belgique")
        self.assertEqual(offers, [])
        self.assertEqual(status.status, "not_applicable_region")
        get.assert_not_called()

    def test_the_muse_http_error_reports_blocked(self):
        with mock.patch.object(
            scan.requests, "get",
            side_effect=Exception("403 Client Error: Forbidden"),
        ):
            offers, status = scan.fetch_the_muse_all(
                domains=self.CATALOG, active_ids=["data"],
            )
        self.assertEqual(offers, [])
        self.assertEqual(status.status, "blocked")

    def test_france_travail_paginates_ranges_and_filters_one_day(self):
        def item(index):
            return {
                "id": str(index), "intitule": "Stage Data Engineer",
                "entreprise": {"nom": "ACME"},
                "lieuTravail": {"libelle": "Paris"},
                "dateCreation": RECENT_1H, "description": "data engineer",
            }

        responses = [
            fake_response(
                {"resultats": [item(i) for i in range(150)]},
                headers={"Content-Range": "offres 0-149/151"},
            ),
            fake_response(
                {"resultats": [item(150)]},
                headers={"Content-Range": "offres 150-150/151"},
            ),
        ]
        with mock.patch.dict(os.environ, {"FT_CLIENT_ID": "id",
                                          "FT_CLIENT_SECRET": "key"}), \
             mock.patch.object(scan, "ft_token", return_value="token"), \
             mock.patch.object(scan.requests, "get", side_effect=responses) as get:
            offers, status = scan.fetch_france_travail_all(
                domains=self.CATALOG, active_ids=["data"]
            )
        self.assertEqual(len(offers), 151)
        self.assertEqual(status.pages_fetched, 2)
        self.assertEqual(
            [call.kwargs["params"]["range"] for call in get.call_args_list],
            ["0-149", "150-299"],
        )
        self.assertTrue(all(call.kwargs["params"]["publieeDepuis"] == 1
                            for call in get.call_args_list))

    def test_france_travail_old_creation_date_is_still_accepted(self):
        # 68/71 offres rejetées "plus de 24 heures" sur un run sain avant le
        # correctif : l'API ne renvoie que des offres publiées et ouvertes,
        # dateCreation ancienne ne vaut jamais rejet.
        response = fake_response(
            {"resultats": [{
                "id": "old", "intitule": "Stage Data Engineer",
                "entreprise": {"nom": "ACME"},
                "lieuTravail": {"libelle": "Paris"},
                "dateCreation": TOO_OLD, "description": "data engineer",
            }]},
            headers={"Content-Range": "offres 0-0/1"},
        )
        with mock.patch.dict(os.environ, {"FT_CLIENT_ID": "id",
                                          "FT_CLIENT_SECRET": "key"}), \
             mock.patch.object(scan, "ft_token", return_value="token"), \
             mock.patch.object(scan.requests, "get", return_value=response):
            offers, status = scan.fetch_france_travail_all(
                domains=self.CATALOG, active_ids=["data"]
            )
        self.assertEqual(len(offers), 1)
        self.assertNotIn(scan.REASON_TOO_OLD, status.reject_reasons)

    def test_adzuna_boundary_date_is_still_accepted(self):
        # max_days_old=1 filtre déjà côté serveur en jours calendaires : une
        # offre limite (36 h) reste garantie récente par la source et ne doit
        # pas être re-rejetée localement (85 rejets constatés sur un run sain).
        boundary = (datetime.now(timezone.utc) - timedelta(hours=36)).isoformat()
        response = fake_response({"count": 1, "results": [{
            "id": "b", "title": "Stage Data Engineer",
            "company": {"display_name": "ACME"},
            "location": {"display_name": "Paris"},
            "redirect_url": "https://e.com/adzuna/b",
            "created": boundary, "description": "data engineer",
        }]})
        with mock.patch.dict(os.environ, {"ADZUNA_APP_ID": "id",
                                          "ADZUNA_APP_KEY": "key"}), \
             mock.patch.object(scan.requests, "get", return_value=response):
            offers, status = scan.fetch_adzuna_all(
                domains=self.CATALOG, active_ids=["data"]
            )
        self.assertEqual(len(offers), 1)
        self.assertNotIn(scan.REASON_TOO_OLD, status.reject_reasons)


class TestQueryRotation(unittest.TestCase):
    """Rotation générique du catalogue, utilisée par LinkedIn Posts."""

    CATALOG = {
        f"domain_{i}": {
            "label": f"D{i}", "group": "Test",
            "queries": [f"stage query {i}"],
            "positive": ["query"], "negative": [], "enabled": True,
        }
        for i in range(25)
    }

    def test_plan_is_bounded_by_budget(self):
        plan, next_index = scan.linkedin_posts_query_plan(
            self.CATALOG, list(self.CATALOG), start_index=0, budget=10,
        )
        self.assertEqual(len(plan), 10)
        self.assertEqual(next_index, 10)

    def test_plan_wraps_around_the_catalog(self):
        plan, next_index = scan.linkedin_posts_query_plan(
            self.CATALOG, list(self.CATALOG), start_index=20, budget=10,
        )
        self.assertEqual(len(plan), 10)
        self.assertEqual(next_index, 5)
        full_catalog = scan.active_queries(self.CATALOG, list(self.CATALOG))
        self.assertEqual(plan, full_catalog[20:25] + full_catalog[0:5])

    def test_budget_larger_than_catalog_returns_whole_catalog_once(self):
        small = {"data": self.CATALOG["domain_0"]}
        plan, next_index = scan.linkedin_posts_query_plan(
            small, ["data"], start_index=0, budget=10,
        )
        self.assertEqual(plan, ["stage query 0"])
        self.assertEqual(next_index, 0)

    def test_missing_or_corrupt_rotation_file_restarts_at_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "absent.json"
            self.assertEqual(scan.load_rotation_index(missing), 0)
            corrupt = Path(tmp) / "corrupt.json"
            corrupt.write_text("{not json", encoding="utf-8")
            self.assertEqual(scan.load_rotation_index(corrupt), 0)


SCHEDULED_XRAY_HOUR = datetime.now(timezone.utc).replace(
    hour=scan.LINKEDIN_POSTS_RUN_HOURS_UTC[0], minute=5, second=0, microsecond=0,
)


def xray_result(title="Alice Martin sur LinkedIn : Stage Data Engineer",
                snippet="Nous recherchons un stagiaire data engineer. Postulez !",
                url="https://www.linkedin.com/posts/alice_stage-activity-7351000000000000001",
                author="Alice Martin", published_at=None):
    return xray.SearchResult(title=title, snippet=snippet, url=url,
                             author=author, published_at=published_at)


class FakeXRayProvider:
    """Double de provider X-ray : une liste de réponses/erreurs par appel."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def search(self, query, freshness=None, *, page=1, max_results=10, now=None):
        self.calls.append(query)
        if not self.responses:
            return []
        outcome = self.responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class TestLinkedInPostsSource(unittest.TestCase):
    """Source X-ray : jamais linkedin.com directement (voir radar_xray.py)."""

    CATALOG = {
        f"domain_{i}": {
            "label": f"D{i}", "group": "Test",
            "queries": [f"stage query {i}"],
            "positive": ["query"], "negative": [], "enabled": True,
        }
        for i in range(12)
    }

    def test_query_plan_is_bounded_and_wraps(self):
        plan, next_index = scan.linkedin_posts_query_plan(
            self.CATALOG, list(self.CATALOG), start_index=10, budget=3,
        )
        self.assertEqual(len(plan), 3)
        self.assertEqual(next_index, 1)

    def test_provider_selection_prefers_exa_when_key_present(self):
        with mock.patch.dict(os.environ, {"EXA_API_KEY": "secret"}):
            provider = scan._build_linkedin_posts_provider(5.0)
        self.assertIn("exa", provider.name)

    def test_provider_selection_falls_back_to_duckduckgo_without_key(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            provider = scan._build_linkedin_posts_provider(5.0)
        self.assertNotIn("exa", provider.name)

    DATA_CATALOG = {
        "data_ia": {
            "label": "Data / IA", "group": "Tech",
            "queries": ["stage data engineer"],
            "positive": ["data engineer"], "negative": [], "enabled": True,
        },
    }

    def test_relevant_post_with_verified_date_is_accepted(self):
        now = datetime.now(timezone.utc)
        provider = FakeXRayProvider([[xray_result(published_at=now - timedelta(hours=1))]])
        offers, status = scan.fetch_linkedin_posts_all(
                "maroc",
            domains=self.DATA_CATALOG, active_ids=list(self.DATA_CATALOG),
            provider=provider,
            rotation_path=Path(tempfile.mkdtemp()) / "rotation.json",
            now=SCHEDULED_XRAY_HOUR,
        )
        self.assertEqual(len(offers), 1)
        self.assertEqual(offers[0]["source"], "LinkedIn Posts")
        self.assertEqual(status.accepted_count, 1)

    def test_post_without_recruiting_intent_is_rejected(self):
        provider = FakeXRayProvider([[xray_result(
            title="Alice sur LinkedIn : conférence data",
            snippet="Retour sur notre conférence annuelle, merci à tous.",
        )]])
        offers, status = scan.fetch_linkedin_posts_all(
                "maroc",
            domains=self.DATA_CATALOG, active_ids=list(self.DATA_CATALOG),
            provider=provider,
            rotation_path=Path(tempfile.mkdtemp()) / "rotation.json",
            now=SCHEDULED_XRAY_HOUR,
        )
        self.assertEqual(offers, [])
        self.assertEqual(status.reject_reasons.get(scan.REASON_NOT_INTERNSHIP), 1)

    def test_post_without_verifiable_date_is_rejected_not_kept(self):
        provider = FakeXRayProvider([[xray_result(published_at=None)]])
        offers, status = scan.fetch_linkedin_posts_all(
                "maroc",
            domains=self.DATA_CATALOG, active_ids=list(self.DATA_CATALOG),
            provider=provider,
            rotation_path=Path(tempfile.mkdtemp()) / "rotation.json",
            now=SCHEDULED_XRAY_HOUR,
        )
        self.assertEqual(offers, [])
        self.assertEqual(
            status.reject_reasons.get(scan.REASON_UNVERIFIABLE_RECENT_DATE), 1
        )

    def test_blocked_provider_trips_circuit_breaker(self):
        provider = FakeXRayProvider([
            xray.XRayBlockedError("bloqué", status_code=202),
            xray.XRayBlockedError("bloqué", status_code=202),
        ])
        offers, status = scan.fetch_linkedin_posts_all(
                "maroc",
            domains=self.CATALOG, active_ids=list(self.CATALOG),
            provider=provider,
            rotation_path=Path(tempfile.mkdtemp()) / "rotation.json",
            now=SCHEDULED_XRAY_HOUR,
        )
        self.assertEqual(offers, [])
        self.assertEqual(status.status, "blocked")
        # Le budget restant est ignoré après deux blocages consécutifs :
        # jamais un troisième appel.
        self.assertEqual(len(provider.calls), 2)

    def test_blocked_status_surfaces_primary_provider_error_not_fallback(self):
        # Constaté en production : Exa 402 (crédits épuisés) suivi du repli
        # DuckDuckGo 202 → dashboard « bloqué 202 », cause réelle invisible.
        class FailingProvider:
            name = "fake"

            def search(self, *args, **kwargs):
                raise xray.XRayBlockedError("bloqué", status_code=202)

        chain = xray.FallbackXRayProvider(
            xray.ExaProvider(
                "key",
                session=type("S", (), {
                    "post": lambda self, url, **kw: type("R", (), {
                        "status_code": 402,
                        "raise_for_status": lambda self: None,
                        "json": lambda self: {},
                    })()
                })(),
            ),
            FailingProvider(),
        )
        offers, status = scan.fetch_linkedin_posts_all(
                "maroc",
            domains=self.CATALOG, active_ids=list(self.CATALOG),
            provider=chain,
            rotation_path=Path(tempfile.mkdtemp()) / "rotation.json",
            now=SCHEDULED_XRAY_HOUR,
        )
        self.assertEqual(status.status, "blocked")
        self.assertIn("crédits Exa épuisés", status.note)
        self.assertIn("crédits Exa épuisés", scan.status_label(status))

    def test_off_schedule_run_is_deferred_without_any_network_call(self):
        # 1000 crédits Tavily/mois : hors créneau, aucune requête, rotation
        # intacte, et le statut "deferred" ne confirme jamais une absence.
        provider = FakeXRayProvider([[xray_result()]])
        off_schedule = SCHEDULED_XRAY_HOUR.replace(hour=12)
        self.assertNotIn(12, scan.LINKEDIN_POSTS_RUN_HOURS_UTC)
        with tempfile.TemporaryDirectory() as tmp:
            rotation_path = Path(tmp) / "rotation.json"
            offers, status = scan.fetch_linkedin_posts_all(
                "maroc",
                domains=self.CATALOG, active_ids=list(self.CATALOG),
                provider=provider, rotation_path=rotation_path,
                now=off_schedule,
            )
            self.assertEqual(offers, [])
            self.assertEqual(status.status, "deferred")
            self.assertEqual(provider.calls, [])
            self.assertFalse(rotation_path.exists())
        self.assertIn("créneaux", status.note)
        self.assertIn("différé", scan.chip_hint(status, 0))

    def test_source_fresh_result_without_date_is_accepted_not_rejected(self):
        # Tavily applique time_range=day côté serveur : la fenêtre est
        # garantie par le moteur, même sans date précise dans l'extrait —
        # même logique que hours_old de JobSpy.
        fresh = xray.SearchResult(
            title="Post de Alice Martin | Stage Data Engineer",
            snippet="Nous recherchons un stagiaire data engineer. Postulez !",
            url="https://www.linkedin.com/posts/alice_stage-activity-7351000000000000042",
            author="Alice Martin", published_at=None, source_fresh=True,
        )
        provider = FakeXRayProvider([[fresh]])
        offers, status = scan.fetch_linkedin_posts_all(
                "maroc",
            domains=self.DATA_CATALOG, active_ids=list(self.DATA_CATALOG),
            provider=provider,
            rotation_path=Path(tempfile.mkdtemp()) / "rotation.json",
            now=SCHEDULED_XRAY_HOUR,
        )
        self.assertEqual(len(offers), 1)
        self.assertFalse(offers[0]["date_known"])
        self.assertTrue(offers[0]["freshness_verified_at"])

    def test_rotating_source_never_confirms_absence(self):
        offer_entry = {"source": "LinkedIn Posts", "regions": ["france"]}
        healthy = [scan.SourceStatus(source="LinkedIn Posts", region_id="france",
                                     status="success")]
        self.assertFalse(scan.source_succeeded_for_offer(offer_entry, healthy))

    def test_unknown_date_offer_is_kept_for_days_window_after_first_sighting(self):
        now = datetime.now(timezone.utc)
        window = scan.MAX_OFFER_AGE_DAYS
        # Revue dans les 24 h : visible quel que soit son âge.
        fresh = {"id": "a", "active": True, "date_known": False, "date": "",
                 "freshness_verified_at": (now - timedelta(hours=2)).isoformat(),
                 "first_seen": (now - timedelta(days=window + 3)).isoformat()}
        # Plus revue, mais apparue il y a moins de days_window : conservée.
        recent = {"id": "b", "active": True, "date_known": False, "date": "",
                  "freshness_verified_at": (now - timedelta(hours=25)).isoformat(),
                  "first_seen": (now - timedelta(hours=25)).isoformat()}
        # Plus revue et apparue avant la fenêtre : masquée.
        stale = {"id": "c", "active": True, "date_known": False, "date": "",
                 "freshness_verified_at": (now - timedelta(days=window + 1)).isoformat(),
                 "first_seen": (now - timedelta(days=window + 1)).isoformat()}
        visible = scan.dashboard_offers_from_store(
            {"a": fresh, "b": recent, "c": stale}, now=now)
        self.assertEqual([o["id"] for o in visible], ["a", "b"])

    def test_linkedin_posts_only_runs_where_the_region_enables_it(self):
        provider = FakeXRayProvider([[xray_result()]])
        offers, status = scan.fetch_linkedin_posts_all(
            "france", provider=provider, now=SCHEDULED_XRAY_HOUR,
            rotation_path=Path(tempfile.mkdtemp()) / "rotation.json")
        self.assertEqual(offers, [])
        self.assertEqual(status.status, "not_applicable_region")
        self.assertIn("mesuré", status.note)
        self.assertIn("LinkedIn Posts", scan.REGIONS["maroc"]["local_sources"])

    def test_real_moroccan_posts_scoring(self):
        """Posts réels Tavily (Maroc, 26/11/2026)."""
        accept = [
            ("Post de CIRCET MOROCCO - LinkedIn",
             "We are hiring | Stage PFE – Data Tu termines tes études et tu veux un PFE "
             "qui ait un vrai impact ? CIRCET MOROCCO ouvre ses portes aux"),
            ("#pfe2027 #sofrecom #internship #artificialintelligence #data",
             "Sofrecom Maroc is launching its PFE 2027 recruitment campaign, with 20 "
             "internship topics across data, AI and software."),
        ]
        reject = [
            ("#drh #maroc #rh | DRH.ma - LinkedIn",
             "PFE 2027 : enquête auprès de 189 entreprises, ressources humaines, recrutement."),
            ("#pfe #stagepfe #géniedesprocédés #énergie",
             "Recherche de Stage PFE (4 à 6 mois) – Génie des Procédés, de l'Énergie."),
        ]
        for title, text in accept:
            self.assertEqual(scan.score_offer_domains(
                title, text, overrides=scan.LINKEDIN_POSTS_SCORING)[0], ["data_ia"], title)
        for title, text in reject:
            self.assertEqual(scan.score_offer_domains(
                title, text, overrides=scan.LINKEDIN_POSTS_SCORING)[0], [], title)
        self.assertEqual(scan.DOMAIN_SCORE_THRESHOLD, 2)

    def test_post_display_title_replaces_hashtags(self):
        self.assertEqual(scan.post_display_title(
            "Post de CIRCET MOROCCO - LinkedIn",
            "We are hiring | Stage PFE – Data"), "We are hiring | Stage PFE – Data")
        long = scan.post_display_title(
            "#expleomaroc #pfe2027 #stagepfe",
            "Aucune description alternative pour cette image " + "mot " * 60)
        self.assertTrue(long.startswith("mot mot") and long.endswith("…"))
        self.assertLessEqual(len(long), 111)
        self.assertEqual(scan.post_display_title("Stage PFE Data chez X", "y"),
                         "Stage PFE Data chez X")

    def test_posts_provider_uses_advanced_tavily_search(self):
        with mock.patch.dict(os.environ, {"TAVILY_API_KEY": "tvly-x", "EXA_API_KEY": ""}):
            provider = scan._build_linkedin_posts_provider(5, region_hint="Maroc")
        self.assertEqual(provider.providers[0].search_depth, "advanced")

    def test_rotation_cursor_persists_and_advances_across_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            rotation_path = Path(tmp) / "linkedin_posts_rotation.json"
            scan.fetch_linkedin_posts_all(
                "maroc",
                domains=self.CATALOG, active_ids=list(self.CATALOG),
                provider=FakeXRayProvider([]), rotation_path=rotation_path,
                now=SCHEDULED_XRAY_HOUR,
            )
            first_index = json.loads(rotation_path.read_text())["next_index"]
            scan.fetch_linkedin_posts_all(
                "maroc",
                domains=self.CATALOG, active_ids=list(self.CATALOG),
                provider=FakeXRayProvider([]), rotation_path=rotation_path,
                now=SCHEDULED_XRAY_HOUR,
            )
            second_index = json.loads(rotation_path.read_text())["next_index"]
            self.assertNotEqual(first_index, second_index)


class TestMissingCredentials(unittest.TestCase):
    def test_adzuna_disabled_without_secrets(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            offers, st = scan.fetch_adzuna_all()
        self.assertEqual(offers, [])
        self.assertEqual(st.status, "disabled_missing_credentials")
        self.assertIn("ADZUNA_APP_ID", st.note)

    def test_france_travail_disabled_without_secrets(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            offers, st = scan.fetch_france_travail_all()
        self.assertEqual(offers, [])
        self.assertEqual(st.status, "disabled_missing_credentials")
        self.assertIn("FT_CLIENT_ID", st.note)

    def test_adzuna_http_error_never_leaks_credentials(self):
        """Régression : un 503 Adzuna a écrit app_key en clair dans
        data/source_status.json (URL complète dans le message requests)."""
        failing = mock.Mock(status_code=503)
        failing.raise_for_status.side_effect = Exception(
            "503 Server Error: Service Temporarily Unavailable for url: "
            "https://api.adzuna.com/v1/api/jobs/fr/search/1?app_id=ID-SECRET"
            "&app_key=KEY-SECRET&what=stage+data")
        with mock.patch.dict(os.environ, {"ADZUNA_APP_ID": "ID-SECRET",
                                          "ADZUNA_APP_KEY": "KEY-SECRET"}), \
             mock.patch.object(scan.requests, "get", return_value=failing), \
             mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
            _, status = scan.fetch_adzuna_all()
        dumped = json.dumps(scan.asdict(status)) + stderr.getvalue()
        self.assertNotIn("KEY-SECRET", dumped)
        self.assertNotIn("ID-SECRET", dumped)
        self.assertIn("app_key=***", status.http_error)
        self.assertEqual(scan.safe_error("x?api_key=abc&page=2"), "x?api_key=***&page=2")

    def test_france_travail_auth_failure_never_leaks_secret(self):
        with mock.patch.dict(os.environ, {"FT_CLIENT_ID": "id-secret-xyz",
                                          "FT_CLIENT_SECRET": "pass-secret-xyz"}), \
             mock.patch.object(scan.requests, "post",
                               side_effect=Exception("403 Client Error")):
            offers, st = scan.fetch_france_travail_all()
        self.assertEqual(offers, [])
        self.assertEqual(st.status, "error")
        dump = str(st)
        self.assertNotIn("id-secret-xyz", dump)
        self.assertNotIn("pass-secret-xyz", dump)


class TestOfferStore(unittest.TestCase):
    # Horloge fraîche à chaque exécution : ces tests ne doivent jamais expirer
    # simplement parce que le calendrier réel a avancé.
    RUN_N = datetime.now(timezone.utc).replace(microsecond=0)

    @staticmethod
    def source_state(status="success"):
        return [scan.SourceStatus(
            source="LinkedIn", region_id="france", region_label="France",
            status=status,
        )]

    def candidate(self):
        candidate, reason = offer(
            date=self.RUN_N.isoformat(), now=self.RUN_N,
        )
        self.assertIsNone(reason)
        return candidate

    def test_confirmed_absence_is_hidden_after_successful_run(self):
        # APEC renvoie la liste complète des offres ouvertes : une absence
        # après un run sain signifie une offre fermée.
        candidate, reason = offer(
            date=self.RUN_N.isoformat(), now=self.RUN_N, source="APEC",
            confirmed_internship=True, url="https://e.com/apec-closed",
        )
        self.assertIsNone(reason)
        apec_ok = [scan.SourceStatus(source="APEC", region_id="france",
                                     region_label="France", status="success")]
        store = scan.reconcile_offer_store(
            [candidate], apec_ok, store={}, run_at=self.RUN_N
        )
        next_run = self.RUN_N + timedelta(hours=1)
        store = scan.reconcile_offer_store(
            [], apec_ok, store=store, run_at=next_run
        )

        self.assertFalse(store[candidate["id"]]["active"])
        self.assertEqual(
            store[candidate["id"]]["last_seen_success"],
            next_run.isoformat(timespec="seconds"),
        )
        self.assertEqual(scan.dashboard_offers_from_store(store, next_run), [])

    def test_windowed_source_offer_stays_visible_for_days_window(self):
        # LinkedIn ne renvoie que les dernières 24 h : sortir de sa fenêtre
        # ne ferme pas l'offre, elle reste affichée days_window jours.
        candidate = self.candidate()
        store = scan.reconcile_offer_store(
            [candidate], self.source_state(), store={}, run_at=self.RUN_N
        )
        later = self.RUN_N + timedelta(days=2)
        store = scan.reconcile_offer_store(
            [], self.source_state(), store=store, run_at=later
        )
        self.assertFalse(store[candidate["id"]]["active"])
        self.assertEqual(
            [o["id"] for o in scan.dashboard_offers_from_store(store, later)],
            [candidate["id"]],
        )
        expired = self.RUN_N + timedelta(days=scan.MAX_OFFER_AGE_DAYS, minutes=1)
        self.assertEqual(scan.dashboard_offers_from_store(store, expired), [])

    def test_unknown_date_sort_key_is_first_sighting_not_last_scan(self):
        first_run = self.RUN_N - timedelta(hours=5)
        old, _ = offer(date=None, now=first_run, url="https://e.com/old",
                       freshness_verified=True)
        store = scan.reconcile_offer_store([old], self.source_state(), store={},
                                           run_at=first_run)
        seen_again, _ = offer(date=None, now=self.RUN_N, url="https://e.com/old",
                              freshness_verified=True)
        newer, _ = offer(date=None, now=self.RUN_N - timedelta(minutes=1),
                         url="https://e.com/new", freshness_verified=True)
        store = scan.reconcile_offer_store([seen_again, newer], self.source_state(),
                                           store=store, run_at=self.RUN_N)
        self.assertEqual(store[old["id"]]["sort_date"], first_run.isoformat())
        ordered = scan.sort_offers(scan.dashboard_offers_from_store(store, self.RUN_N))
        self.assertEqual([o["url"] for o in ordered],
                         ["https://e.com/new", "https://e.com/old"])

    def test_offers_of_disabled_domains_or_regions_are_hidden(self):
        base = dict(self.candidate(), active=True,
                    first_seen=self.RUN_N.isoformat())
        store = {
            "data": dict(base, id="data", domains=["data_ia"], regions=["france"]),
            "data_rh": dict(base, id="data_rh", domains=["data_ia", "rh"],
                            regions=["maroc"]),
            "rh": dict(base, id="rh", domains=["rh"], regions=["france"]),
            "compta": dict(base, id="compta", domains=["compta_gestion"],
                           regions=["france"]),
            "belgique": dict(base, id="belgique", domains=["data_ia"],
                             regions=["belgique"]),
        }
        visible = scan.dashboard_offers_from_store(
            store, self.RUN_N, active_domain_ids=["data_ia"],
            active_region_ids=["france", "maroc"],
        )
        self.assertEqual(sorted(o["id"] for o in visible), ["data", "data_rh"])
        # Sans filtre explicite, rien n'est masqué (compatibilité).
        self.assertEqual(len(scan.dashboard_offers_from_store(store, self.RUN_N)), 5)

    def test_entries_never_seen_again_are_purged_after_retention(self):
        # Une région désactivée ne produit plus aucun statut : son offre ne
        # peut plus être confirmée absente, elle était conservée à vie.
        candidate = dict(self.candidate(), regions=["belgique"])
        old_run = self.RUN_N - timedelta(days=scan.OFFER_RETENTION_DAYS + 1)
        store = scan.reconcile_offer_store(
            [candidate], [], store={}, run_at=old_run
        )
        self.assertTrue(store[candidate["id"]]["active"])
        store = scan.reconcile_offer_store(
            [], self.source_state(), store=store, run_at=self.RUN_N
        )
        self.assertNotIn(candidate["id"], store)

    def test_blocked_source_keeps_offer_visible_and_unchanged(self):
        candidate = self.candidate()
        store = scan.reconcile_offer_store(
            [candidate], self.source_state(), store={}, run_at=self.RUN_N
        )
        before = dict(store[candidate["id"]])
        next_run = self.RUN_N + timedelta(hours=1)
        store = scan.reconcile_offer_store(
            [], self.source_state("blocked"), store=store, run_at=next_run
        )

        self.assertEqual(store[candidate["id"]], before)
        self.assertEqual(
            [item["id"] for item in scan.dashboard_offers_from_store(store, next_run)],
            [candidate["id"]],
        )

    def test_native_freshness_verification_expires_after_24_hours(self):
        # Publication ancienne (republiée) mais confirmée par le filtre natif
        # de la source : visible tant que la vérification a moins de 24 h.
        checked_at = self.RUN_N
        published = checked_at - timedelta(days=scan.MAX_OFFER_AGE_DAYS + 3)
        candidate, reason = offer(
            date=published.isoformat(), now=checked_at, freshness_verified=True,
            url="https://e.com/native-window",
        )
        self.assertIsNone(reason)
        candidate["active"] = True
        candidate["first_seen"] = checked_at.isoformat()
        store = {candidate["id"]: candidate}
        self.assertEqual(len(scan.dashboard_offers_from_store(store, checked_at)), 1)
        self.assertEqual(
            scan.dashboard_offers_from_store(
                store, checked_at + timedelta(hours=24, minutes=1)
            ),
            [],
        )

    def test_old_confirmed_absent_offer_is_purged(self):
        candidate = self.candidate()
        old_run = self.RUN_N - timedelta(days=scan.OFFER_RETENTION_DAYS + 1)
        store = scan.reconcile_offer_store(
            [candidate], self.source_state(), store={}, run_at=old_run
        )
        store = scan.reconcile_offer_store(
            [], self.source_state(), store=store, run_at=self.RUN_N
        )
        self.assertNotIn(candidate["id"], store)

    def test_missing_store_migrates_without_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "offers.json"
            self.assertEqual(scan.load_offer_store(path), {})
            candidate = self.candidate()
            store = scan.reconcile_offer_store(
                [candidate], self.source_state(),
                store=scan.load_offer_store(path), run_at=self.RUN_N,
            )
            scan.save_offer_store(store, path)
            self.assertIn(candidate["id"], scan.load_offer_store(path))


class TestStatusLabel(unittest.TestCase):
    def test_labels(self):
        disabled = scan.SourceStatus(source="Adzuna", status="disabled_missing_credentials")
        self.assertEqual(scan.status_label(disabled), "Non configuré — identifiants manquants")

        unavailable = scan.SourceStatus(
            source="APEC", region_id="belgique", region_label="Belgique",
            status="not_applicable_region",
        )
        self.assertEqual(scan.status_label(unavailable), "Non applicable à Belgique")

        blocked = scan.SourceStatus(source="Indeed", status="blocked", http_error="HTTP 403")
        self.assertEqual(scan.status_label(blocked), "Bloqué temporairement — HTTP 403")

        empty = scan.SourceStatus(source="France Travail", status="empty", raw_count=12)
        self.assertEqual(scan.status_label(empty), "0 accepté / 12 reçus")

        ok = scan.SourceStatus(source="LinkedIn", status="success", accepted_count=11)
        self.assertEqual(scan.status_label(ok, final_count=11), "11 offres")


class TestDashboard(unittest.TestCase):
    def setUp(self):
        self._orig = scan.DOCS_INDEX
        self._tmp = tempfile.TemporaryDirectory()
        scan.DOCS_INDEX = Path(self._tmp.name) / "index.html"

    def tearDown(self):
        scan.DOCS_INDEX = self._orig
        self._tmp.cleanup()

    def render(self, offers, statuses=None, **kwargs):
        scan.render_dashboard(offers, set(), statuses, scan_duration_s=42, **kwargs)
        return scan.DOCS_INDEX.read_text(encoding="utf-8")

    def dashboard_config(self, page):
        match = re.search(
            r'<script id="radarConfig" type="application/json">(.*?)</script>',
            page,
            re.DOTALL,
        )
        self.assertIsNotNone(match)
        return json.loads(match.group(1))

    def dashboard_javascript(self):
        return (Path(scan.__file__).resolve().parent / "docs/assets/dashboard.js").read_text(
            encoding="utf-8"
        )

    def test_national_source_chip_shows_france_result_not_non_applicable(self):
        # Constaté en production : France Travail en succès en France (71
        # reçus) mais chip « non applicable » — le non-statut des trois autres
        # régions écrasait le résultat réel de la seule région où la source
        # tourne.
        statuses = [
            scan.SourceStatus(source="France Travail", region_id="france",
                              region_label="France", status="empty",
                              raw_count=71),
        ] + [
            scan.SourceStatus(source="France Travail", region_id=r,
                              region_label=r.title(),
                              status="not_applicable_region")
            for r in ("belgique", "luxembourg", "maroc")
        ]
        page = self.render([], statuses)
        # Sans offre affichée, plus de puce de filtre : l'état réel reste
        # dans « État des connecteurs », jamais masqué par « non applicable ».
        self.assertIn("<b>France Travail · France</b> · 0 accepté / 71 reçus", page)
        self.assertNotIn('data-src="France Travail"', page)

    def test_product_config_controls_dashboard(self):
        candidate, _ = offer()
        candidate["highlighted"] = True
        config = {
            "app_name": "Stage Atlas",
            "tagline": "Stages multi-sources configurables",
            "highlight_companies": ["acme"],
            "highlight_label": "Grand compte",
            "days_window": 5,
        }
        page = self.render([candidate], config=config)
        self.assertIn("Stage Atlas — Dashboard", page)
        self.assertIn("Stages multi-sources configurables", page)
        self.assertIn("Grand compte", page)
        self.assertIn("sur les 5 derniers jours", page)

    def test_html_escaping_and_url_safety(self):
        evil, _ = offer(title='Stage ETL <script>alert(1)</script>',
                        company='A&B "quotes"', url="javascript:alert(1)")
        self.assertIsNotNone(evil)
        page = self.render([evil])
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertNotIn('href="javascript:', page)
        self.assertIn("A&amp;B", page)

    def test_security_and_ui_attributes(self):
        o, _ = offer()
        statuses = [scan.SourceStatus(source=n) for n in scan.SOURCE_NAMES]
        by_name = {st.source: st for st in statuses}
        by_name["Indeed"].status = "blocked"
        by_name["Indeed"].http_error = "HTTP 403"
        by_name["France Travail"].status = "disabled_missing_credentials"
        # Source désactivée par la config : jamais affichée.
        by_name["APEC"].status = "blocked"
        by_name["APEC"].note = "APEC-DESACTIVEE"
        page = self.render([o], statuses)
        javascript = self.dashboard_javascript()
        self.assertIn('rel="noopener noreferrer"', page)
        self.assertIn('<meta name="google" content="notranslate">', page)
        self.assertIn('translate="no"', page)
        self.assertIn("Actualiser les offres", page)
        self.assertIn('href="assets/dashboard.css"', page)
        self.assertIn('src="assets/dashboard.js" defer', page)
        self.assertNotIn("api.github.com", page)
        self.assertNotIn("onclick=", page)
        self.assertIn('elements.refreshButton.addEventListener("click"', javascript)
        self.assertIn('$$(".source-chip").forEach', javascript)
        self.assertIn('$$(".status-chip").forEach', javascript)
        self.assertIn('elements.highlightFilter.addEventListener("click"', javascript)
        self.assertIn('$$(".domain-chip").forEach', javascript)
        self.assertIn('data-domain="data_ia"', page)
        self.assertIn('$$(".region-chip").forEach', javascript)
        self.assertIn('data-region="france"', page)
        self.assertIn('data-regions="france"', page)
        self.assertIn('$$(".apply-btn").forEach', javascript)
        # le bouton reset est toujours visible, désactivé par défaut
        self.assertIn("Réinitialiser les filtres", page)
        self.assertIn('id="resetBtn" class="fchip reset-btn" disabled', page)
        self.assertIn('elements.resetButton.addEventListener("click", resetFilters)',
                      javascript)
        self.assertIn("Bloqué temporairement — HTTP 403", page)
        self.assertIn("Non configuré — identifiants manquants", page)
        # une source sans offre n'a pas de puce de filtre
        self.assertNotIn('data-src="France Travail"', page)
        self.assertIn('data-src="LinkedIn"', page)
        self.assertNotIn("APEC-DESACTIVEE", page)
        self.assertNotIn('data-src="APEC"', page)
        self.assertNotIn('data-src="The Muse"', page)

    def test_manual_scan_controls_and_github_api_code_are_removed(self):
        candidate, _ = offer()
        page = self.render([candidate])
        javascript = self.dashboard_javascript()
        config = self.dashboard_config(page)

        for key in ("workflowFile", "workflowRef", "repository", "pageGeneratedAt"):
            self.assertNotIn(key, config)
        for removed in ("scanTriggerBtn", "scanDialog", "githubToken", "runNowBtn"):
            self.assertNotIn(removed, page)
            self.assertNotIn(removed, javascript)
        self.assertNotIn("api.github.com", javascript)
        self.assertNotIn("sessionStorage", javascript)
        self.assertIn("window.location.reload()", javascript)

    def test_workflow_runs_hourly_daytime_and_keeps_manual_github_dispatch(self):
        workflow = (Path(scan.__file__).resolve().parent
                    / ".github/workflows/scan.yml").read_text(encoding="utf-8")
        # Lignes actives seulement : l'ancien test passait sur un cron commenté.
        active = [line.split("#", 1)[0] for line in workflow.splitlines()
                  if not line.lstrip().startswith("#")]
        crons = [line.split("cron:", 1)[1].strip().strip('"')
                 for line in active if "cron:" in line]
        self.assertEqual(crons, ["7 6-21 * * *"])
        self.assertTrue(any("workflow_dispatch" in line for line in active))
        self.assertTrue(any("cancel-in-progress: false" in line for line in active))
        self.assertTrue(any("timeout-minutes:" in line for line in active))
        self.assertIn("data/offers.json", workflow)

    def test_search_indexes_title_company_and_location(self):
        candidate, _ = offer(
            title="Stage Data Engineer", company="ACME Analytics", location="Lyon"
        )
        page = self.render([candidate])
        javascript = self.dashboard_javascript()
        self.assertIn('id="searchInput" type="search"', page)
        self.assertIn('data-search="Stage Data Engineer ACME Analytics Lyon"', page)
        self.assertIn("normalizeText(job.dataset.search).includes(searchTerm)", javascript)

    def test_offers_sorted_newest_first(self):
        older, _ = offer(title="Stage ETL B", date=RECENT_2H, url="https://e.com/b")
        newer, _ = offer(title="Stage ETL A", date=RECENT_1H, url="https://e.com/a")
        page = self.render([older, newer])
        self.assertLess(page.index("https://e.com/a"), page.index("https://e.com/b"))

    def test_unknown_date_badge(self):
        o, reason = offer(date=None)
        self.assertIsNone(reason)
        page = self.render([o])
        self.assertIn("Date de publication inconnue", page)
        self.assertIn("date inconnue", page)

    def test_region_filter_defaults_to_all_and_combines_client_side(self):
        france, _ = offer(url="https://e.com/france")
        maroc, _ = offer(
            url="https://e.com/maroc", location="Casablanca",
            region_id="maroc",
        )
        page = self.render([france, maroc])
        self.assertIn('class="fchip region-chip on" data-region="*" aria-pressed="true"', page)
        self.assertIn('class="fchip region-chip" data-region="france"', page)
        self.assertIn('class="fchip region-chip" data-region="maroc"', page)
        self.assertNotIn('data-region="belgique"', page)
        self.assertNotIn("single-region", page)
        self.assertEqual(self.dashboard_config(page)["defaultRegion"], "*")
        self.assertIn('<span class="tag region">Maroc</span>', page)
        javascript = self.dashboard_javascript()
        self.assertIn(
            'const regionMatches = activeRegion === "*" || regions.includes(activeRegion);',
            javascript,
        )
        self.assertIn(
            "const show = sourceMatches && domainMatches && regionMatches && statusMatches &&",
            javascript,
        )

    def test_visa_badge_only_on_french_offers(self):
        paris, _ = offer(company="Capgemini", url="https://e.com/cap-fr")
        casa, _ = offer(company="Capgemini Maroc", url="https://e.com/cap-ma",
                        location="Casablanca", region_id="maroc")
        self.assertTrue(paris["highlighted"] and casa["highlighted"])
        self.assertTrue(scan.shows_highlight(paris))
        self.assertFalse(scan.shows_highlight(casa))
        page = self.render([paris, casa])
        cards = {match.group(1): match.group(0) for match in re.finditer(
            r'<article class="job[^"]*" data-id="([^"]+)".*?</article>', page, re.DOTALL)}
        self.assertIn('data-highlight="1"', cards[paris["id"]])
        self.assertIn("✓ Visa-friendly probable</span>", cards[paris["id"]])
        self.assertIn('data-highlight="0"', cards[casa["id"]])
        self.assertNotIn("Visa-friendly probable</span>", cards[casa["id"]])
        # Sans highlight_regions, comportement historique : toutes régions.
        legacy = {k: v for k, v in scan.APP_CONFIG.items() if k != "highlight_regions"}
        self.assertTrue(scan.shows_highlight(casa, legacy))

    def test_highlight_company_matches_whole_words_only(self):
        self.assertTrue(scan.is_highlighted_company("OCTO Technology"))
        self.assertTrue(scan.is_highlighted_company("Société Générale"))
        self.assertTrue(scan.is_highlighted_company("Sopra Steria Next"))
        self.assertFalse(scan.is_highlighted_company("Octopus Energy"))
        self.assertFalse(scan.is_highlighted_company("Saxa Conseil"))

    def test_single_domain_filter_group_is_hidden(self):
        candidate, _ = offer()
        page = self.render([candidate])
        self.assertIn('class="filter-group single-domain"', page)
        css = (Path(scan.__file__).resolve().parent
               / "docs/assets/dashboard.css").read_text(encoding="utf-8")
        rule = re.search(r"\.filter-group\.single-domain\s*\{([^}]*)\}", css)
        self.assertIsNotNone(rule)
        self.assertIn("display: none", rule.group(1))
        self.assertIn("<strong>1 domaine</strong> et <strong>2 régions</strong>", page)

    def test_single_region_filter_is_hidden_but_france_stays_active(self):
        candidate, _ = offer()
        page = self.render([candidate], active_region_ids=["france"])
        self.assertIn('class="filter-group single-region"', page)
        self.assertEqual(self.dashboard_config(page)["defaultRegion"], "france")
        self.assertNotIn('data-region="belgique"', page)

    def test_every_active_offer_is_rendered_as_a_card_beyond_150(self):
        """Régression : les chips source/domaine/région comptaient le total
        de `offers`, mais seules les 150 premières cartes étaient rendues —
        une source classée au-delà de ce rang affichait un chip non-nul sans
        aucune carte filtrable, menant à "0 offre" quel que soit le filtre."""
        many = [offer(url=f"https://example.com/o/{i}", date=TODAY)[0]
                for i in range(160)]
        page = self.render(many)
        self.assertEqual(page.count('<article class="job'), 160)
        self.assertIn('data-src="LinkedIn" aria-pressed="false">LinkedIn <b>(160)</b>',
                      page)


DREAMJOB_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/"><channel>
<item><title>2 offres de stage PFE en data chez Circet Morocco à Casablanca</title>
<link>https://www.dreamjob.ma/stage/2-offres-de-stage-pfe-en-data-chez-circet-morocco-a-casablanca/</link>
<pubDate>{recent}</pubDate><dc:creator><![CDATA[Dreamjob.ma]]></dc:creator>
<category><![CDATA[Stages]]></category><category><![CDATA[Circet Emploi et Recrutement]]></category>
<description><![CDATA[Circet Morocco recrute 2 profils de stagiaires en Stage PFE à Casablanca: un Data Engineer et un Data Analyst (H/F). L'article 2 offres de stage PFE en data chez Circet Morocco à Casablanca est apparu en premier sur Dreamjob.ma]]></description></item>
<item><title>Élèves ingénieurs: Saham Bank recrute des stagiaires PFE en IT, data et risques</title>
<link>https://www.dreamjob.ma/stage/saham-bank-pfe/</link><pubDate>{recent}</pubDate>
<category><![CDATA[Stages]]></category><description><![CDATA[Saham Bank recrute.]]></description></item>
<item><title>4 offres de stage PFE chez Groupe BCP en finance à Casablanca</title>
<link>https://www.dreamjob.ma/stage/bcp-finance/</link><pubDate>{recent}</pubDate>
<category><![CDATA[Stages]]></category><description><![CDATA[Stages en finance et contrôle de gestion.]]></description></item>
<item><title>SQORUS Maroc renforce ses équipes data et web avec des stagiaires PFE à Casablanca</title>
<link>https://www.dreamjob.ma/stage/sqorus-old/</link><pubDate>{old}</pubDate>
<category><![CDATA[Stages]]></category><description><![CDATA[Data et web.]]></description></item>
</channel></rss>"""


class TestMoroccanLocalSources(unittest.TestCase):
    NOW = datetime.now(timezone.utc).replace(microsecond=0)

    def rss(self):
        from email.utils import format_datetime
        return DREAMJOB_RSS.format(
            recent=format_datetime(self.NOW - timedelta(days=2)),
            old=format_datetime(self.NOW - timedelta(days=scan.MAX_OFFER_AGE_DAYS + 3)),
        )

    def test_plural_contract_words_are_recognised(self):
        for title in ["Stagiaires PFE Data", "Stages data 2027", "Data Interns",
                      "Summer internships - data"]:
            self.assertTrue(scan.TITLE_MUST_MATCH.search(title), title)

    def test_scoring_overrides_never_mutate_the_catalog(self):
        before = json.dumps(scan.DOMAINS["data_ia"], sort_keys=True)
        title = "Saham Bank recrute des stagiaires PFE en IT, data et risques"
        self.assertEqual(scan.score_offer_domains(title, "")[0], [])
        matched, _ = scan.score_offer_domains(title, "", overrides=scan.MAROC_LOCAL_SCORING)
        self.assertEqual(matched, ["data_ia"])
        self.assertEqual(json.dumps(scan.DOMAINS["data_ia"], sort_keys=True), before)

    def test_dreamjob_feed_parsing_and_selection(self):
        items = scan.parse_dreamjob_feed(self.rss())
        self.assertEqual(items[0]["company"], "Circet")
        self.assertNotIn("apparu en premier", items[0]["description"])
        with mock.patch.object(scan.requests, "get",
                               return_value=mock.Mock(text=self.rss(), status_code=200,
                                                      raise_for_status=lambda: None)) as get:
            offers, status = scan.fetch_dreamjob_all("maroc", now=self.NOW)
        titles = sorted(o["title"] for o in offers)
        self.assertEqual(titles, [
            "2 offres de stage PFE en data chez Circet Morocco à Casablanca",
            "Élèves ingénieurs: Saham Bank recrute des stagiaires PFE en IT, data et risques",
        ])
        circet = next(o for o in offers if "Circet" in o["title"])
        self.assertEqual(circet["location"], "Casablanca, Maroc")
        self.assertEqual(circet["regions"], ["maroc"])
        self.assertTrue(circet["date_known"])
        self.assertEqual(status.reject_reasons, {
            scan.REASON_NO_DOMAIN: 1, f"plus de {scan.MAX_OFFER_AGE_DAYS} jours": 1})
        self.assertEqual(status.too_old_count, 1)
        self.assertIn("radar-stage", get.call_args.kwargs["headers"]["User-Agent"])
        # Une seule requête : la page 1 contient déjà une annonce hors fenêtre.
        self.assertEqual(get.call_count, 1)

    def test_local_sources_are_not_applicable_in_france(self):
        with mock.patch.object(scan.requests, "get") as get, \
             mock.patch.object(scan.requests, "post") as post:
            _, dreamjob = scan.fetch_dreamjob_all("france")
            _, sites = scan.fetch_maroc_sites_all("france")
        get.assert_not_called()
        post.assert_not_called()
        self.assertEqual(dreamjob.status, "not_applicable_region")
        self.assertEqual(sites.status, "not_applicable_region")

    def test_maroc_sites_keeps_offer_pages_only(self):
        keep = scan.parse_maroc_site_result({
            "url": "https://www.stagiaires.ma/stage-emploi-maroc/8186-stages-pfe-2027",
            "title": "Stages PFE 2027 - Profils Ingénieurs data chez Saham Bank | Stagiaires.ma",
            "content": "Saham Bank recrute des stagiaires PFE en data et IT."})
        self.assertEqual(keep["title"], "Stages PFE 2027 - Profils Ingénieurs data")
        self.assertEqual(keep["company"], "Saham Bank")
        for url in ["https://www.marocannonces.com/categorie/311/Demandes-de-stage/annonce/10484",
                    "https://www.rekrute.com/en/emploi-Ing%C3%A9nieur-PFE",
                    "https://www.stagiaires.ma/"]:
            self.assertIsNone(scan.parse_maroc_site_result({"url": url, "title": "x"}), url)
        self.assertIsNotNone(scan.parse_maroc_site_result({
            "url": "https://www.rekrute.com/offre-emploi-stage-pfe-data-engineer-casablanca-178000.html",
            "title": "Stage PFE Data Engineer - ReKrute.com"}))

    def test_dreamjob_html_answer_is_reported_blocked_not_xml_error(self):
        page = mock.Mock(text="<!DOCTYPE html><html><body>Access denied", status_code=200,
                         raise_for_status=lambda: None)
        with mock.patch.object(scan.requests, "get", return_value=page), \
             mock.patch("sys.stderr", new_callable=io.StringIO):
            offers, status = scan.fetch_dreamjob_all("maroc", now=self.NOW)
        self.assertEqual(offers, [])
        self.assertEqual(status.status, "blocked")
        self.assertIn("Sites Maroc", status.note)

    def test_maroc_sites_also_covers_dreamjob_articles(self):
        parsed = scan.parse_maroc_site_result({
            "url": "https://www.dreamjob.ma/stage/2-offres-de-stage-pfe-en-data-chez-circet-morocco-a-casablanca/",
            "title": "2 offres de stage PFE en data chez Circet Morocco à Casablanca - Dreamjob",
            "content": "Un Data Engineer et un Data Analyst."})
        self.assertEqual(parsed["title"],
                         "2 offres de stage PFE en data chez Circet Morocco à Casablanca")
        self.assertEqual(parsed["company"], "")
        self.assertIsNone(scan.parse_maroc_site_result(
            {"url": "https://www.dreamjob.ma/stage/", "title": "Stages"}))

    def test_maroc_sites_fetch_slot_budget_and_credentials(self):
        off_slot = self.NOW.replace(hour=12)
        _, status = scan.fetch_maroc_sites_all("maroc", now=off_slot)
        self.assertEqual(status.status, "deferred")
        slot = self.NOW.replace(hour=scan.LINKEDIN_POSTS_RUN_HOURS_UTC[0])
        with mock.patch.dict(os.environ, {"TAVILY_API_KEY": ""}):
            _, status = scan.fetch_maroc_sites_all("maroc", now=slot)
        self.assertEqual(status.status, "disabled_missing_credentials")
        session = mock.Mock()
        session.post.return_value = mock.Mock(status_code=200, raise_for_status=lambda: None)
        session.post.return_value.json.return_value = {"results": [
            {"url": "https://www.stagiaires.ma/stage-emploi-maroc/8200",
             "title": "Stage PFE Data Engineer chez CGI | Stagiaires.ma",
             "content": "Pipelines Spark à Rabat."},
            {"url": "https://stagiaires.ma/stage-emploi-maroc/8200/",
             "title": "Stage PFE Data Engineer chez ...",
             "content": "Pipelines Spark."},
            {"url": "https://www.stagiaires.ma/stage-emploi-maroc/8300",
             "title": "Stage pré-embauche – Service client - ozon express",
             "content": "Data, SQL, Power BI."},
            {"url": "https://www.marocannonces.com/categorie/311/Demandes-de-stage/annonce/1",
             "title": "Je cherche un stage data"},
        ]}
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.dict(os.environ, {"TAVILY_API_KEY": "tvly-test"}):
            offers, status = scan.fetch_maroc_sites_all(
                "maroc", session=session, now=slot,
                rotation_path=Path(tmp) / "rotation.json")
        self.assertEqual(session.post.call_count, scan.MAROC_SITES_QUERY_BUDGET)
        body = session.post.call_args.kwargs["json"]
        self.assertEqual(body["include_domains"], list(scan.MAROC_SITES_DOMAINS))
        self.assertEqual(len(offers), 1)
        self.assertEqual(offers[0]["company"], "CGI")
        self.assertGreaterEqual(status.duplicate_count, 1)
        self.assertEqual(scan.parse_maroc_site_result({
            "url": "https://www.rekrute.com/offre-emploi-stage-pfe-data-casablanca-178000.html",
            "title": "Offre d'emploi Stage PFE Data - ReKrute.com"})["title"], "Stage PFE Data")
        self.assertEqual(offers[0]["location"], "Rabat, Maroc")
        self.assertIn("Sites Maroc", scan.ROTATING_COVERAGE_SOURCES)


def stagiaires_page(title, company="ACME", city="Casablanca", description="", date=None,
                    valid_through=None):
    posting = {"@context": "https://schema.org", "@type": "JobPosting", "title": title,
               "description": description,
               "datePosted": date or datetime.now(timezone.utc).isoformat(),
               "hiringOrganization": {"@type": "Organization", "name": company},
               "jobLocation": {"@type": "Place", "address": {"addressLocality": city}},
               "employmentType": "INTERN"}
    if valid_through:
        posting["validThrough"] = valid_through
    return ('<html><head><script type="application/ld+json">' + json.dumps(posting)
            + "</script></head><body>x</body></html>")


class TestStagiairesMa(unittest.TestCase):
    NOW = datetime.now(timezone.utc).replace(microsecond=0)

    def sitemap(self, entries):
        urls = "".join(f"<url><loc>{u}</loc><lastmod>{m}</lastmod></url>" for u, m in entries)
        return ('<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                + urls + "</urlset>")

    def session(self, pages):
        calls = []

        def get(url, headers=None, timeout=None):
            calls.append(url)
            body = pages[url]
            if isinstance(body, Exception):
                resp = mock.Mock(status_code=403)
                resp.raise_for_status.side_effect = body
                return resp
            return mock.Mock(text=body, status_code=200, raise_for_status=lambda: None)
        return mock.Mock(get=get), calls

    def test_sitemap_only_offer_urls_without_query_strings(self):
        entries = scan.parse_stagiaires_sitemap(self.sitemap([
            ("https://www.stagiaires.ma/stage-emploi-maroc/8226-stage-pfe-data-h-f", "2026-11-25T10:00:00+00:00"),
            ("https://www.stagiaires.ma/entreprise/deloitte", "2026-11-25T10:00:00+00:00"),
            ("https://www.stagiaires.ma/stage-emploi-maroc/1?x=1", "2026-11-25T10:00:00+00:00"),
        ]))
        self.assertEqual([u for u, _ in entries],
                         ["https://www.stagiaires.ma/stage-emploi-maroc/8226-stage-pfe-data-h-f"])

    def test_fetch_scores_caches_and_respects_delay(self):
        base = "https://www.stagiaires.ma/stage-emploi-maroc/"
        recent = (self.NOW - timedelta(hours=3)).isoformat()
        old = (self.NOW - timedelta(days=scan.MAX_OFFER_AGE_DAYS + 2)).isoformat()
        pages = {
            scan.STAGIAIRES_SITEMAP: self.sitemap([
                (base + "1-stage-pfe-data", recent), (base + "2-marketing", recent),
                (base + "3-expiree", recent), (base + "4-ancienne", old)]),
            base + "1-stage-pfe-data": stagiaires_page(
                "Stage PFE – Data (H/F)", "CIRCET MOROCCO",
                description="Circet recrute un stagiaire PFE data engineer, pipelines ETL et Power BI."),
            base + "2-marketing": stagiaires_page("Stage en Marketing Digital", "Maamora"),
            base + "3-expiree": stagiaires_page(
                "Stage PFE Data Engineer", valid_through=(self.NOW - timedelta(days=1)).isoformat()),
        }
        session, calls = self.session(pages)
        sleeps = []
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "cache.json"
            offers, status = scan.fetch_stagiaires_all(
                "maroc", session=session, cache_path=cache, now=self.NOW, sleep=sleeps.append)
            self.assertEqual([o["title"] for o in offers], ["Stage PFE – Data (H/F)"])
            self.assertEqual(offers[0]["company"], "CIRCET MOROCCO")
            self.assertEqual(offers[0]["location"], "Casablanca, Maroc")
            self.assertEqual(offers[0]["regions"], ["maroc"])
            self.assertEqual(status.reject_reasons,
                             {scan.REASON_NO_DOMAIN: 1, "offre expirée": 1})
            self.assertNotIn(base + "4-ancienne", calls)
            self.assertEqual(sleeps, [scan.STAGIAIRES_DELAY_S] * 2)
            # Run suivant : seules les pages nouvelles ou modifiées sont relues.
            calls.clear()
            scan.fetch_stagiaires_all("maroc", session=session, cache_path=cache, now=self.NOW,
                                      sleep=sleeps.append)
            self.assertEqual(calls, [scan.STAGIAIRES_SITEMAP])

    def test_blocked_pages_trip_the_circuit_breaker(self):
        base = "https://www.stagiaires.ma/stage-emploi-maroc/"
        recent = self.NOW.isoformat()
        urls = [base + f"{i}-x" for i in range(5)]
        pages = {scan.STAGIAIRES_SITEMAP: self.sitemap([(u, recent) for u in urls])}
        pages.update({u: Exception("403 Client Error: Forbidden") for u in urls})
        session, calls = self.session(pages)
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch("sys.stderr", new_callable=io.StringIO):
            offers, status = scan.fetch_stagiaires_all(
                "maroc", session=session, cache_path=Path(tmp) / "c.json",
                now=self.NOW, sleep=lambda _s: None)
        self.assertEqual(len(calls), 1 + scan.MAX_CONSECUTIVE_BLOCKS)
        self.assertEqual(status.status, "blocked")

    def test_not_applicable_in_france(self):
        with mock.patch.object(scan.requests, "Session") as session:
            _, status = scan.fetch_stagiaires_all("france")
        session.assert_not_called()
        self.assertEqual(status.status, "not_applicable_region")


if __name__ == "__main__":
    unittest.main()
