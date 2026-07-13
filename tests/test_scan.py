"""Tests du pipeline scan.py — aucun accès réseau, aucune dépendance jobspy.

Couvre : dates absolues/relatives/inconnues, ciblage Data/IA strict,
filtres natifs, déduplication, tri, champs None/NaN, échappement HTML,
sources sans secrets, circuit breaker 403/429 et interactions du dashboard.
"""

import io
import logging
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scan

TODAY = datetime.now(timezone.utc).date().isoformat()
YESTERDAY = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
TOO_OLD = (datetime.now(timezone.utc) - timedelta(days=30)).date().isoformat()


def offer(title="Stage Data Engineer", company="ACME", location="Paris",
          url="https://example.com/o/1", source="LinkedIn", date=TODAY, **kw):
    return scan.make_offer(title, company, location, url, source, date, **kw)


class TestCleanText(unittest.TestCase):
    def test_none_and_nan(self):
        self.assertEqual(scan.clean_text(None), "")
        self.assertEqual(scan.clean_text("nan"), "")
        self.assertEqual(scan.clean_text("NaT"), "")
        self.assertEqual(scan.clean_text("None"), "")
        self.assertEqual(scan.clean_text(float("nan")), "")

    def test_strip(self):
        self.assertEqual(scan.clean_text("  x "), "x")


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

    def test_too_old_rejected(self):
        with mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
            o, reason = offer(date=TOO_OLD)
        self.assertIsNone(o)
        self.assertEqual(reason, scan.REASON_TOO_OLD)
        self.assertIn(f"date_brute='{TOO_OLD}'", stderr.getvalue())
        self.assertIn("date_parsee=", stderr.getvalue())

    def test_missing_and_unreadable_dates_are_kept(self):
        for date in (None, "", "à confirmer"):
            o, reason = offer(date=date, url=f"https://example.com/{date!r}")
            self.assertIsNone(reason)
            self.assertFalse(o["date_known"])
            self.assertEqual(o["date"], "")

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
        self.assertEqual(reason, scan.REASON_NOT_PROFILE)

    def test_description_cannot_rescue_rh_title(self):
        # Une exclusion forte du titre reste prioritaire sur la description.
        o, reason = offer(
            title="Stagiaire RH Analytics F/H", source="APEC",
            confirmed_internship=True,
            description="Missions : Data Analytics, Business Intelligence, Power BI.",
        )
        self.assertIsNone(o)
        self.assertEqual(reason, scan.REASON_EXCLUDED_DOMAIN)

    def test_generic_description_does_not_rescue(self):
        # « RH » sans contexte data dans le titre : exclusion forte immédiate.
        o, reason = offer(
            title="Stagiaire RH généraliste F/H", source="APEC",
            confirmed_internship=True,
            description="Gestion administrative du personnel, paie et recrutement.",
        )
        self.assertIsNone(o)
        self.assertEqual(reason, scan.REASON_EXCLUDED_DOMAIN)

    def test_description_can_confirm_data_profile(self):
        o, reason = offer(
            title="Stage ingénieur logiciel",
            description="Construction de pipelines ETL en Python et SQL.",
        )
        self.assertIsNone(reason)
        self.assertIsNotNone(o)

    def test_nan_company_defaults(self):
        o, _ = offer(company="nan", location=None)
        self.assertEqual(o["company"], "Entreprise non précisée")
        self.assertEqual(o["location"], "France")


class TestCVTargetingStrict(unittest.TestCase):
    """Bug 1 : les 4 offres hors-sujet constatées sur le dashboard doivent
    être rejetées ; les vraies offres data doivent toujours passer."""

    def test_gestion_projet_qualite_rejected(self):
        o, reason = offer(
            title="Amélioration des Outils et Processus de Gestion de Projet Qualité F/H",
            company="Proaxian", source="APEC", confirmed_internship=True,
            description="Suivi qualité, reporting, Power BI et tableaux de bord.",
        )
        self.assertIsNone(o)
        self.assertEqual(reason, scan.REASON_EXCLUDED_DOMAIN)

    def test_auditeur_junior_rejected(self):
        # Même une mention Power BI en description ne repêche pas un audit.
        o, reason = offer(
            title="Auditeur junior F/H", company="Proaxian",
            source="APEC", confirmed_internship=True,
            description="Missions d'audit avec restitution sous Power BI.",
        )
        self.assertIsNone(o)
        self.assertEqual(reason, scan.REASON_EXCLUDED_DOMAIN)

    def test_finance_controle_gestion_rejected(self):
        o, reason = offer(
            title="Stagiaire Finance et Contrôle de Gestion H/F",
            company="SONEMA", source="Indeed",
            description="Tableaux de bord Power BI, analyses budgétaires.",
        )
        self.assertIsNone(o)
        self.assertEqual(reason, scan.REASON_EXCLUDED_DOMAIN)

    def test_business_data_analyst_crm_rejected(self):
        # Cas limite tranché selon la consigne : CRM est un domaine
        # commercial/marketing hors du profil Data Engineering ciblé.
        o, reason = offer(title="Stage – Business Data Analyst CRM (H/F)",
                          company="Prosol", source="Indeed")
        self.assertIsNone(o)
        self.assertEqual(reason, scan.REASON_EXCLUDED_DOMAIN)

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
        # « finance » exclu même combiné à un mot positif du titre.
        o, reason = offer(title="Stage analyste financier Power BI")
        self.assertEqual(reason, scan.REASON_EXCLUDED_DOMAIN)
        # « juridique / droit » exclu (bruit APEC constaté).
        o, reason = offer(title="Stagiaire en droit du numérique",
                          confirmed_internship=True)
        self.assertEqual(reason, scan.REASON_EXCLUDED_DOMAIN)

    def test_paid_media_and_fpa_rejected(self):
        # Bruit constaté au premier run après fix : publicité et finance
        # repêchées par une mention Power BI en description.
        o, reason = offer(title="Assistant Consultant Paid Media - SEA et SMA F/H",
                          source="APEC", confirmed_internship=True,
                          description="Reporting des campagnes sous Power BI.")
        self.assertEqual(reason, scan.REASON_EXCLUDED_DOMAIN)
        o, reason = offer(title="FP&A Analyst Intern", source="Indeed",
                          description="Dashboards Power BI, data analytics.")
        self.assertEqual(reason, scan.REASON_EXCLUDED_DOMAIN)

    def test_generic_words_do_not_match(self):
        # Mots génériques (gestion, outils, processus, analyse) : jamais
        # suffisants pour matcher le ciblage.
        o, reason = offer(title="Stage gestion et amélioration des processus",
                          description="Analyse des outils internes.")
        self.assertIsNone(o)
        self.assertEqual(reason, scan.REASON_NOT_PROFILE)

    def test_description_does_not_rescue_non_technical_titles(self):
        # Faux positifs observés au benchmark : une mention SQL/Power BI dans
        # la description ne suffit plus si le titre n'est pas technique.
        for title in [
            "Stage - Analyste Excellence Opérationnelle (F/M/X)",
            "Chargée Relations Écoles & Universités - Stage",
            "Junior Business Analyst - Stage (H/F/N)",
            "Internship - Développement de Carrières & Projets Alumni",
            "Stagiaire Cybersécurité DevSecops",
            "Consultant Digital Engineering - Stage",
        ]:
            o, reason = offer(
                title=title,
                description="Reporting Power BI et extraction SQL de données.",
            )
            self.assertIsNone(o, title)
            self.assertEqual(reason, scan.REASON_NOT_PROFILE, title)

    def test_description_rescues_only_technical_generic_title(self):
        o, reason = offer(
            title="Stage ingénieur logiciel",
            description="Pipelines de données, ETL, Python et SQL.",
        )
        self.assertIsNone(reason)
        self.assertIsNotNone(o)

    def test_ai_requires_technical_context(self):
        o, reason = offer(title="Stage communication IA")
        self.assertIsNone(o)
        self.assertEqual(reason, scan.REASON_NOT_PROFILE)
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

    def test_unknown_date_counted_when_accepted(self):
        st = scan.SourceStatus(source="Test")
        offers = {}
        candidate, reason = offer(date=None)
        scan.register_offer(st, offers, candidate, reason)
        self.assertEqual(st.unknown_date_count, 1)


class TestSort(unittest.TestCase):
    def test_three_known_dates_newest_first(self):
        oldest_date = (datetime.now(timezone.utc) - timedelta(days=2)).date().isoformat()
        old, _ = offer(title="Stage ETL ancien", date=oldest_date, url="https://e.com/1")
        middle, _ = offer(title="Stage ETL milieu", date=YESTERDAY, url="https://e.com/2")
        new, _ = offer(title="Stage ETL récent", date=TODAY, url="https://e.com/3")
        ordered = scan.sort_offers([old, new, middle])
        self.assertEqual([o["date"] for o in ordered], [TODAY, YESTERDAY, oldest_date])

    def test_unknown_date_is_kept_and_visible_first(self):
        known, _ = offer(date=YESTERDAY, url="https://e.com/known")
        unknown, _ = offer(date=None, url="https://e.com/unknown")
        ordered = scan.sort_offers([known, unknown])
        self.assertEqual(ordered[0]["id"], unknown["id"])


class TestCircuitBreaker(unittest.TestCase):
    def test_blocked_403_stops_hammering(self):
        calls = []

        def fake_scrape(**kwargs):
            calls.append(kwargs["search_term"])
            raise Exception("bad response status code: 403")

        offers, st = scan.fetch_jobspy_site("glassdoor", "Glassdoor", scrape_fn=fake_scrape)
        self.assertEqual(len(calls), scan.MAX_CONSECUTIVE_BLOCKS)
        self.assertEqual(st.status, "blocked")
        self.assertEqual(st.http_error, "HTTP 403")
        self.assertEqual(offers, [])
        self.assertIn("circuit breaker", st.note)

    def test_blocked_429_detected(self):
        def fake_scrape(**kwargs):
            raise Exception("429 Response - Blocked for too many requests")

        _, st = scan.fetch_jobspy_site("glassdoor", "Glassdoor", scrape_fn=fake_scrape)
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
            logging.getLogger("JobSpy:Glassdoor").error(
                "Glassdoor: response status code 403")
            return pd.DataFrame()

        offers, st = scan.fetch_jobspy_site("glassdoor", "Glassdoor", scrape_fn=fake_scrape)
        self.assertEqual(calls, scan.MAX_CONSECUTIVE_BLOCKS)
        self.assertEqual(st.status, "blocked")
        self.assertEqual(st.http_error, "HTTP 403")
        self.assertEqual(offers, [])

    def test_swallowed_generic_jobspy_error_is_not_reported_as_empty(self):
        import pandas as pd

        def fake_scrape(**_kwargs):
            logging.getLogger("JobSpy:Glassdoor").error(
                "Glassdoor: Error encountered in API response")
            return pd.DataFrame()

        offers, st = scan.fetch_jobspy_site("glassdoor", "Glassdoor", scrape_fn=fake_scrape)
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
        self.assertEqual(st.accepted_count, 1)
        self.assertEqual(st.rejected_count, 2)
        self.assertEqual(st.reject_reasons.get(scan.REASON_TOO_OLD), 1)
        self.assertEqual(st.reject_reasons.get(scan.REASON_NOT_PROFILE), 1)
        self.assertEqual(st.status, "success")
        self.assertEqual(len(offers), 1)

    def test_google_search_term_not_redundant(self):
        import pandas as pd
        seen_kwargs = []

        def fake_scrape(**kwargs):
            seen_kwargs.append(kwargs)
            return pd.DataFrame()

        scan.fetch_jobspy_site("google", "Google Jobs", scrape_fn=fake_scrape)
        first = seen_kwargs[0]
        self.assertEqual(first["google_search_term"], "stage data France")
        self.assertNotIn("job_type", first)
        # jamais deux fois « stage » dans la requête Google
        self.assertEqual(first["google_search_term"].lower().count("stage"), 1)

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
        self.assertEqual(seen["linkedin"]["results_wanted"], 25)
        self.assertEqual(seen["indeed"]["results_wanted"], 30)
        self.assertEqual(seen["linkedin"]["hours_old"], 168)
        self.assertEqual(seen["indeed"]["hours_old"], 168)
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
        self.assertEqual([call["search_term"] for call in seen], scan.LINKEDIN_QUERIES)
        self.assertEqual(
            [call["search_term"] for call in seen if call["linkedin_fetch_description"]],
            [scan.LINKEDIN_DESCRIPTION_QUERY],
        )
        self.assertTrue(any(not call["linkedin_fetch_description"] for call in seen))
        self.assertTrue(all("f_SAL" not in str(call) for call in seen))
        self.assertTrue(all("publié au cours" not in call["search_term"].lower()
                            for call in seen))

        seen.clear()
        scan.fetch_jobspy_site("indeed", "Indeed", scrape_fn=fake_scrape)
        self.assertIn('"stage 6 mois" data engineer',
                      [call["search_term"] for call in seen])


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


class TestStatusLabel(unittest.TestCase):
    def test_labels(self):
        disabled = scan.SourceStatus(source="Adzuna", status="disabled_missing_credentials")
        self.assertEqual(scan.status_label(disabled), "Non configuré — identifiants manquants")

        blocked = scan.SourceStatus(source="Glassdoor", status="blocked", http_error="HTTP 403")
        self.assertEqual(scan.status_label(blocked), "Bloqué temporairement — HTTP 403")

        empty = scan.SourceStatus(source="Google Jobs", status="empty", raw_count=12)
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

    def render(self, offers, statuses=None):
        scan.render_dashboard(offers, set(), statuses, scan_duration_s=42)
        return scan.DOCS_INDEX.read_text(encoding="utf-8")

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
        statuses[2].status = "blocked"
        statuses[2].http_error = "HTTP 403"
        statuses[5].status = "disabled_missing_credentials"
        page = self.render([o], statuses)
        self.assertIn('rel="noopener noreferrer"', page)
        self.assertIn('<meta name="google" content="notranslate">', page)
        self.assertIn('translate="no"', page)
        self.assertIn("Ouvrir GitHub Actions", page)
        self.assertIn("Run workflow", page)
        self.assertIn("Actualiser les offres", page)
        self.assertNotIn("onclick=", page)
        self.assertIn("refreshBtn').addEventListener('click'", page)
        self.assertIn("source-chip').forEach", page)
        self.assertIn("status-chip').forEach", page)
        self.assertIn("visaFilter').addEventListener('click'", page)
        self.assertIn("apply-btn').forEach", page)
        # le bouton reset est toujours visible, désactivé par défaut
        self.assertIn("Réinitialiser les filtres", page)
        self.assertIn('id="resetBtn" class="fchip reset-btn" disabled', page)
        self.assertIn("resetBtn').addEventListener('click',resetFilters)", page)
        self.assertIn("Bloqué temporairement — HTTP 403", page)
        self.assertIn("Non configuré — identifiants manquants", page)
        # une source jamais appelée n'affiche pas un simple « 0 »
        self.assertIn("non configuré", page)

    def test_offers_sorted_newest_first(self):
        older, _ = offer(title="Stage ETL B", date=YESTERDAY, url="https://e.com/b")
        newer, _ = offer(title="Stage ETL A", date=TODAY, url="https://e.com/a")
        page = self.render([older, newer])
        self.assertLess(page.index("https://e.com/a"), page.index("https://e.com/b"))

    def test_unknown_date_badge(self):
        o, reason = offer(date=None)
        self.assertIsNone(reason)
        page = self.render([o])
        self.assertIn("Date de publication inconnue", page)
        self.assertIn("date inconnue", page)


if __name__ == "__main__":
    unittest.main()
