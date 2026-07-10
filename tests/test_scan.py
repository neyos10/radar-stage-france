"""Tests du pipeline scan.py — aucun accès réseau, aucune dépendance jobspy.

Couvre : dates récentes/anciennes, titres de stage, exclusions DS/ML,
repêchage par description (APEC), déduplication, tri, champs None/NaN,
échappement HTML, sources sans secrets, circuit breaker 403/429.
"""

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


class TestMakeOffer(unittest.TestCase):
    def test_recent_internship_accepted(self):
        o, reason = offer()
        self.assertIsNone(reason)
        self.assertEqual(o["date"], TODAY)
        self.assertEqual(o["source"], "LinkedIn")

    def test_too_old_rejected(self):
        o, reason = offer(date=TOO_OLD)
        self.assertIsNone(o)
        self.assertEqual(reason, scan.REASON_TOO_OLD)

    def test_missing_date_rejected(self):
        o, reason = offer(date="")
        self.assertIsNone(o)
        self.assertEqual(reason, scan.REASON_NO_DATE)

    def test_not_internship_rejected(self):
        o, reason = offer(title="Data Engineer CDI")
        self.assertIsNone(o)
        self.assertEqual(reason, scan.REASON_NOT_INTERNSHIP)

    def test_ml_excluded(self):
        o, reason = offer(title="Stage Machine Learning Engineer")
        self.assertIsNone(o)
        self.assertEqual(reason, scan.REASON_EXCLUDED)

    def test_data_science_excluded(self):
        o, reason = offer(title="Stagiaire Data Scientist NLP")
        self.assertIsNone(o)
        self.assertEqual(reason, scan.REASON_EXCLUDED)

    def test_off_profile_rejected(self):
        o, reason = offer(title="Stage assistant chef de projet digital")
        self.assertIsNone(o)
        self.assertEqual(reason, scan.REASON_NOT_PROFILE)

    def test_description_rescues_rh_analytics(self):
        # Cas réel APEC : titre RH mais missions Data Analytics / BI.
        o, reason = offer(
            title="Stagiaire RH Analytics F/H", source="APEC",
            confirmed_internship=True,
            description="Missions : Data Analytics, Business Intelligence, Power BI.",
        )
        self.assertIsNone(reason)
        self.assertEqual(o["title"], "Stagiaire RH Analytics F/H")

    def test_generic_description_does_not_rescue(self):
        # « RH » sans contexte data dans le titre : exclusion forte immédiate.
        o, reason = offer(
            title="Stagiaire RH généraliste F/H", source="APEC",
            confirmed_internship=True,
            description="Gestion administrative du personnel, paie et recrutement.",
        )
        self.assertIsNone(o)
        self.assertEqual(reason, scan.REASON_EXCLUDED_DOMAIN)

    def test_description_cannot_override_exclusion(self):
        o, reason = offer(
            title="Stage Deep Learning",
            description="Beaucoup de Power BI et d'ETL dans la description.",
        )
        self.assertIsNone(o)
        self.assertEqual(reason, scan.REASON_EXCLUDED)

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

    def test_business_data_analyst_crm_accepted(self):
        # Cas limite évalué : « Data Analyst » est explicitement dans le
        # ciblage CV ; CRM n'est que le domaine d'application → accepté.
        o, reason = offer(title="Stage – Business Data Analyst CRM (H/F)",
                          company="Prosol", source="Indeed")
        self.assertIsNone(reason)
        self.assertEqual(o["title"], "Stage – Business Data Analyst CRM (H/F)")

    def test_real_data_offers_still_accepted(self):
        for title in [
            "STAGE - DATA ENGINEER F/H",
            "Stage - Data Analyst - Paris - H/F",
            "Stage Business Intelligence / Power BI",
            "Stage ingénieur Big Data Spark",
            "Stage ETL Talend et Data Warehouse",
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


class TestSort(unittest.TestCase):
    def test_newest_first(self):
        old, _ = offer(title="Stage ETL ancien", date=YESTERDAY, url="https://e.com/1")
        new, _ = offer(title="Stage ETL récent", date=TODAY, url="https://e.com/2")
        ordered = scan.sort_offers([old, new])
        self.assertEqual([o["date"] for o in ordered], [TODAY, YESTERDAY])


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
            {"title": "Stage Machine Learning", "company": "ACME", "city": "Nice",
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
        self.assertEqual(st.reject_reasons.get(scan.REASON_EXCLUDED), 1)
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
        self.assertEqual(first["google_search_term"], "stage data engineer France")
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
        self.assertEqual(seen["linkedin"]["job_type"], "internship")


class TestApecNativeFilters(unittest.TestCase):
    def test_payload_has_native_filters(self):
        p = scan.apec_search_payload("data")
        # 143684-143687 = annonceurs directs (entreprise, cabinet, agence,
        # SSII) ; 143706 (offres partenaires, source du bruit) exclu.
        self.assertEqual(p["typesConvention"], [143684, 143685, 143686, 143687])
        self.assertNotIn(143706, p["typesConvention"])
        self.assertEqual(p["typesContrat"], [597171])
        # niveauEtude volontairement absent (mono-valeur, trop restrictif).
        self.assertNotIn("niveauEtude", p)


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
        self.assertIn('onclick="location.reload()"', page)
        # le bouton reset est toujours visible, désactivé par défaut
        self.assertIn("Réinitialiser les filtres", page)
        self.assertIn('onclick="resetFilters()" disabled', page)
        self.assertIn("Bloqué temporairement — HTTP 403", page)
        self.assertIn("Non configuré — identifiants manquants", page)
        # une source jamais appelée n'affiche pas un simple « 0 »
        self.assertIn("non configuré", page)

    def test_offers_sorted_newest_first(self):
        older, _ = offer(title="Stage ETL B", date=YESTERDAY, url="https://e.com/b")
        newer, _ = offer(title="Stage ETL A", date=TODAY, url="https://e.com/a")
        page = self.render([older, newer])
        self.assertLess(page.index("https://e.com/a"), page.index("https://e.com/b"))


if __name__ == "__main__":
    unittest.main()
