"""LinkedIn Posts : uniquement les stages vérifiés des dernières 24 heures.

Couvre le mode strict par domaine (data_ia, domains.json) : filtre stage
obligatoire, fenêtre fixe de 24 h indépendante du profil, rejet des dates
non vérifiables, association multi-domaines, déduplication par URL
canonique, non-rafraîchissement des redécouvertes et regroupement du
dashboard par domaine.
"""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import scan
from radar_profiles import ScanContext
from radar_xray import SearchResult, XRayRotationState, canonicalize_linkedin_post_url


NOW = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)


def post_result(activity="7351000000000000001", *, title=None, snippet=None,
                author="Alice Martin", published_at=NOW - timedelta(hours=1),
                url=None):
    return SearchResult(
        title=title or f"{author} sur LinkedIn : Stage Data Engineer",
        snippet=snippet or "Nous recherchons un stagiaire data engineer. Envoyez votre CV.",
        url=url or f"https://www.linkedin.com/posts/x_stage-activity-{activity}",
        author=author,
        published_at=published_at,
    )


class FixedResultProvider:
    """Renvoie toujours la même liste de résultats, quelle que soit la requête."""

    name = "fixture_fixed"

    def __init__(self, results):
        self.results = list(results)
        self.calls = 0

    def search(self, query, freshness=None, **kwargs):
        self.calls += 1
        return list(self.results)


class TestStrictFreshnessWindow(unittest.TestCase):
    """La fenêtre est fixe à 24 h, indépendamment du profil FAST (2 h)."""

    def setUp(self):
        self.context = ScanContext(scan.PROFILES["fast_data_ia"], NOW)

    def fetch_one(self, published_at):
        provider = FixedResultProvider([post_result(published_at=published_at)])
        offers, status, _ = scan.fetch_linkedin_posts(
            context=self.context, provider=provider,
            rotation_state=XRayRotationState(),
        )
        return offers, status

    def test_stage_published_23h59_ago_is_accepted(self):
        offers, status = self.fetch_one(NOW - timedelta(hours=23, minutes=59))
        self.assertEqual(len(offers), 1)
        self.assertEqual(status.accepted_count, 1)

    def test_stage_published_24h01_ago_is_rejected(self):
        offers, status = self.fetch_one(NOW - timedelta(hours=24, minutes=1))
        self.assertEqual(offers, [])
        self.assertIn("plus de 24 heures", status.reject_reasons)

    def test_stage_published_exactly_24h_ago_is_accepted(self):
        # published_at >= now - 24h : la borne est incluse.
        offers, status = self.fetch_one(NOW - timedelta(hours=24))
        self.assertEqual(len(offers), 1)
        self.assertEqual(status.accepted_count, 1)

    def test_fast_profile_window_of_2h_does_not_shrink_linkedin_posts_window(self):
        # Un post publié il y a 5h (hors fenêtre FAST de 2h) reste accepté :
        # LinkedIn Posts ignore délibérément la fenêtre du profil courant.
        offers, status = self.fetch_one(NOW - timedelta(hours=5))
        self.assertEqual(len(offers), 1)
        self.assertEqual(status.accepted_count, 1)

    def test_unknown_date_is_rejected_with_exact_reason(self):
        offers, status = self.fetch_one(None)
        self.assertEqual(offers, [])
        self.assertIn(scan.REASON_UNVERIFIABLE_RECENT_DATE, status.reject_reasons)
        self.assertEqual(
            scan.REASON_UNVERIFIABLE_RECENT_DATE,
            "date non vérifiable sur les dernières 24 heures",
        )


class TestMandatoryInternshipFilter(unittest.TestCase):
    def setUp(self):
        self.context = ScanContext(scan.PROFILES["fast_data_ia"], NOW)

    def fetch_one(self, title, snippet):
        provider = FixedResultProvider([post_result(title=title, snippet=snippet)])
        return scan.fetch_linkedin_posts(
            context=self.context, provider=provider,
            rotation_state=XRayRotationState(),
        )

    def test_cdi_without_stage_word_is_rejected(self):
        offers, status, _ = self.fetch_one(
            "Recrutement : Data Engineer en CDI",
            "Nous recrutons un Data Engineer en CDI. Envoyez votre CV.",
        )
        self.assertEqual(offers, [])
        self.assertIn(scan.REASON_NOT_INTERNSHIP, status.reject_reasons)

    def test_alternance_without_stage_word_is_rejected(self):
        offers, status, _ = self.fetch_one(
            "Recherche alternant Data Engineer",
            "Nous recherchons un alternant data engineer pour 12 mois.",
        )
        self.assertEqual(offers, [])
        self.assertIn(scan.REASON_NOT_INTERNSHIP, status.reject_reasons)

    def test_explicit_internship_post_is_accepted(self):
        offers, status, _ = self.fetch_one(
            "Alice Martin sur LinkedIn : Stage Data Engineer",
            "Nous recherchons un(e) stagiaire pour un stage de fin d'études "
            "data engineer. Envoyez votre CV.",
        )
        self.assertEqual(len(offers), 1)
        self.assertEqual(status.accepted_count, 1)


class TestMultiDomainAssociation(unittest.TestCase):
    def setUp(self):
        self.context = ScanContext(scan.PROFILES["fast_data_ia"], NOW)

    def test_post_matching_two_domains_is_stored_once(self):
        result = post_result(
            title="Alice Martin sur LinkedIn : Stage Data Engineer Cloud Engineer",
            snippet=(
                "Nous recrutons un(e) stagiaire data engineer avec des "
                "compétences en machine learning, cloud engineer et "
                "kubernetes. Envoyez votre CV."
            ),
        )
        provider = FixedResultProvider([result])
        offers, status, _ = scan.fetch_linkedin_posts(
            context=self.context,
            domains=scan.DOMAINS,
            active_ids=["data_ia", "devops_cloud"],
            provider=provider,
            rotation_state=XRayRotationState(),
        )
        self.assertEqual(len(offers), 1)
        offer = offers[0]
        self.assertEqual(set(offer["domains"]), {"data_ia", "devops_cloud"})
        self.assertIn("data_ia", offer["domain_scores"])
        self.assertIn("devops_cloud", offer["domain_scores"])
        self.assertTrue(offer["matched_keywords"]["data_ia"])
        self.assertTrue(offer["matched_keywords"]["devops_cloud"])
        # Les deux domaines comptabilisent la même offre finale.
        self.assertEqual(status.domain_reports["data_ia"]["final_offers_count"], 1)
        self.assertEqual(
            status.domain_reports["devops_cloud"]["final_offers_count"], 1
        )


class TestUrlDeduplication(unittest.TestCase):
    def test_tracking_url_and_clean_url_share_identity(self):
        clean = "https://www.linkedin.com/posts/acme_stage-data-activity-7351000000000001"
        tracked = (
            "https://fr.linkedin.com/posts/acme_stage-data-"
            "activity-7351000000000001?utm_source=share&trk=public_profile#comments"
        )
        self.assertEqual(
            canonicalize_linkedin_post_url(clean),
            canonicalize_linkedin_post_url(tracked),
        )

    def test_fetch_deduplicates_same_post_found_with_and_without_tracking(self):
        context = ScanContext(scan.PROFILES["fast_data_ia"], NOW)
        tracked_result = post_result(
            url=(
                "https://fr.linkedin.com/posts/acme_stage-data-"
                "activity-7351000000000000003?utm_source=share&trk=abc"
            ),
        )
        clean_result = post_result(
            url="https://www.linkedin.com/posts/acme_stage-data-activity-7351000000000000003",
        )
        provider = FixedResultProvider([tracked_result, clean_result])
        offers, status, _ = scan.fetch_linkedin_posts(
            context=context, provider=provider,
            rotation_state=XRayRotationState(),
        )
        self.assertEqual(len(offers), 1)
        self.assertGreaterEqual(status.duplicate_count, 1)


class TestRediscoveryNeverBecomesNew(unittest.TestCase):
    def test_rediscovered_known_post_counts_as_known_not_new(self):
        context = ScanContext(scan.PROFILES["fast_data_ia"], NOW)
        published_at = NOW - timedelta(hours=20)
        result = post_result(published_at=published_at)
        known_offer, _ = scan.make_offer(
            result.title, "Alice Martin", "France", result.url,
            "LinkedIn Posts", published_at.isoformat(),
            confirmed_internship=True, active_ids=["data_ia"], context=context,
            external_id=result.external_id,
            strict_window_hours=scan.LINKEDIN_POSTS_STRICT_WINDOW_HOURS,
        )
        known_index = {key: known_offer for key in known_offer["identity_keys"]}
        provider = FixedResultProvider([result])
        offers, status, _ = scan.fetch_linkedin_posts(
            context=context, provider=provider,
            rotation_state=XRayRotationState(),
            known_identity_index=known_index,
        )
        self.assertEqual(len(offers), 1)
        self.assertEqual(status.query_reports[0]["known_count"], 1)
        self.assertEqual(status.query_reports[0]["new_count"], 0)

    def test_old_post_rediscovered_today_is_not_treated_as_freshly_published(self):
        # La date de découverte (aujourd'hui) ne doit jamais se substituer à
        # la date de publication réelle (ici, il y a 30h : hors fenêtre).
        context = ScanContext(scan.PROFILES["fast_data_ia"], NOW)
        old_result = post_result(published_at=NOW - timedelta(hours=30))
        provider = FixedResultProvider([old_result])
        offers, status, _ = scan.fetch_linkedin_posts(
            context=context, provider=provider,
            rotation_state=XRayRotationState(),
        )
        self.assertEqual(offers, [])
        self.assertIn("plus de 24 heures", status.reject_reasons)


class TestDomainDiagnostics(unittest.TestCase):
    def test_domain_with_no_result_shows_zero_without_error(self):
        context = ScanContext(scan.PROFILES["fast_data_ia"], NOW)
        provider = FixedResultProvider([])
        offers, status, _ = scan.fetch_linkedin_posts(
            context=context, provider=provider,
            rotation_state=XRayRotationState(),
        )
        self.assertEqual(offers, [])
        self.assertIn("data_ia", status.domain_reports)
        bucket = status.domain_reports["data_ia"]
        self.assertEqual(bucket["final_offers_count"], 0)
        self.assertGreater(bucket["queries_executed"], 0)
        self.assertNotEqual(status.status, "error")

    def test_domain_reports_track_each_required_field(self):
        context = ScanContext(scan.PROFILES["fast_data_ia"], NOW)
        provider = FixedResultProvider([
            post_result("7351000000000000010", published_at=NOW - timedelta(hours=1)),
            post_result("7351000000000000011",
                       title="Recrutement CDI Data Engineer",
                       snippet="Nous recrutons en CDI. Envoyez votre CV."),
            post_result("7351000000000000012", published_at=None),
            post_result("7351000000000000013",
                       published_at=NOW - timedelta(hours=30)),
        ])
        offers, status, _ = scan.fetch_linkedin_posts(
            context=context, provider=provider,
            rotation_state=XRayRotationState(),
        )
        bucket = status.domain_reports["data_ia"]
        for field_name in (
            "queries_executed", "raw_count", "stage_detected_count",
            "rejected_not_internship_count", "rejected_too_old_count",
            "rejected_unknown_date_count", "duplicate_count",
            "final_offers_count", "duration_s",
        ):
            self.assertIn(field_name, bucket)
        self.assertGreaterEqual(bucket["rejected_not_internship_count"], 1)
        self.assertGreaterEqual(bucket["rejected_unknown_date_count"], 1)
        self.assertGreaterEqual(bucket["rejected_too_old_count"], 1)
        self.assertGreaterEqual(bucket["final_offers_count"], 1)
        self.assertEqual(len(offers), bucket["final_offers_count"])


class TestDashboardDomainGrouping(unittest.TestCase):
    def test_offers_are_grouped_by_domain_in_dashboard(self):
        context = ScanContext(scan.PROFILES["fast_data_ia"], NOW)
        multi_domain_offer, _ = scan.make_offer(
            "Alice Martin sur LinkedIn : Stage Data Cloud",
            "Alice Martin", "France",
            "https://www.linkedin.com/posts/x_stage-activity-7351000000000000020",
            "LinkedIn Posts", (NOW - timedelta(hours=2)).isoformat(),
            confirmed_internship=True,
            description="stage data engineer machine learning cloud engineer kubernetes",
            domains=scan.DOMAINS, active_ids=["data_ia", "devops_cloud"],
            context=context, contact_direct=True,
            strict_window_hours=scan.LINKEDIN_POSTS_STRICT_WINDOW_HOURS,
        )
        self.assertIsNotNone(multi_domain_offer)
        self.assertEqual(
            set(multi_domain_offer["domains"]), {"data_ia", "devops_cloud"}
        )
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "index.html"
            scan.render_dashboard(
                [multi_domain_offer], {multi_domain_offer["id"]},
                context=context, output_path=output,
            )
            page = output.read_text(encoding="utf-8")
        self.assertIn("Stages LinkedIn Posts vérifiés (24 h)", page)
        data_ia_index = page.index("Data / IA <span")
        devops_index = page.index("DevOps / Cloud <span")
        section_index = page.index('class="linkedin-posts-section"')
        self.assertGreater(data_ia_index, section_index)
        self.assertGreater(devops_index, section_index)
        # La même offre doit apparaître dans les deux groupes de domaine.
        self.assertEqual(page.count('class="job linkedin-post-job"'), 2)

    def test_stale_beyond_24h_offer_is_excluded_from_the_section_at_render_time(self):
        context = ScanContext(scan.PROFILES["fast_data_ia"], NOW)
        stale_offer, _ = scan.make_offer(
            "Alice Martin sur LinkedIn : Stage Data Engineer",
            "Alice Martin", "France",
            "https://www.linkedin.com/posts/x_stage-activity-7351000000000000030",
            "LinkedIn Posts", (NOW - timedelta(hours=1)).isoformat(),
            confirmed_internship=True, description="stage data engineer",
            domains=scan.DOMAINS, active_ids=["data_ia"], context=context,
        )
        # Le rendu a lieu 25h plus tard : l'offre est toujours "active" dans
        # le store (rétention générale) mais n'a plus sa place dans la
        # section stricte 24h.
        later_context = ScanContext(
            scan.PROFILES["fast_data_ia"], NOW + timedelta(hours=25)
        )
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "index.html"
            scan.render_dashboard(
                [stale_offer], set(), context=later_context, output_path=output,
            )
            page = output.read_text(encoding="utf-8")
        self.assertIn("Stages LinkedIn Posts vérifiés (24 h)", page)
        self.assertIn("0 stages", page)


if __name__ == "__main__":
    unittest.main()
