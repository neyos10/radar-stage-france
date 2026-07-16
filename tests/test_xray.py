import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

import radar_xray as xray


FIXTURE = Path(__file__).parent / "fixtures" / "duckduckgo_results.html"
NOW = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)


class FakeResponse:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, response=None, error=None):
        self.response = response or FakeResponse()
        self.error = error
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        return self.response


class TestDuckDuckGoHtmlParsing(unittest.TestCase):
    def test_fixture_parses_and_deduplicates_linkedin_posts(self):
        results = xray.parse_duckduckgo_html(FIXTURE.read_text(), now=NOW)

        self.assertEqual(len(results), 1)
        result = results[0]
        self.assertEqual(result.author, "Alice Martin")
        self.assertEqual(
            result.url,
            "https://www.linkedin.com/posts/"
            "alice-martin_stage-data-engineer-activity-7351000000000000000",
        )
        self.assertEqual(result.external_id, "linkedin_post:7351000000000000000")
        self.assertEqual(result.published_at, NOW - timedelta(hours=2))
        self.assertIn("Envoyez votre CV", result.snippet)
        self.assertTrue(xray.is_relevant_internship_post(result))

    def test_non_linkedin_and_non_post_urls_are_ignored(self):
        html = """
          <a class='result__a' href='https://example.com/posts/a'>Autre</a>
          <div class='result__snippet'>Stage</div>
          <a class='result__a' href='https://linkedin.com/jobs/view/1'>Job</a>
          <div class='result__snippet'>Stage</div>
        """
        self.assertEqual(xray.parse_duckduckgo_html(html), [])

    def test_lite_classes_are_supported(self):
        html = """
          <a class='result-link'
             href='https://linkedin.com/posts/bob_stage-cloud-activity-12345678901'>
             Bob Dupont sur LinkedIn : Stage Cloud
          </a>
          <td class='result-snippet'>Il y a 30 minutes — Nous recrutons un stagiaire.</td>
        """
        result = xray.parse_duckduckgo_html(html, now=NOW)[0]
        self.assertEqual(result.author, "Bob Dupont")
        self.assertEqual(result.published_at, NOW - timedelta(minutes=30))

    def test_irrelevant_public_post_is_detected_before_business_scoring(self):
        result = xray.SearchResult(
            title="Conférence Data",
            snippet="Retour sur notre conférence annuelle.",
            url="https://linkedin.com/posts/alice_conference-activity-12345678901",
        )
        self.assertFalse(xray.is_relevant_internship_post(result))


class TestCanonicalization(unittest.TestCase):
    def test_tracking_fragment_host_and_slashes_are_normalized(self):
        first = xray.canonicalize_linkedin_post_url(
            "https://fr.linkedin.com//posts//alice_demo-activity-12345678901/"
            "?utm_source=x&trackingId=y#fragment"
        )
        second = xray.canonicalize_linkedin_post_url(
            "https://www.linkedin.com/posts/alice_demo-activity-12345678901?trk=z"
        )
        self.assertEqual(first, second)
        self.assertEqual(
            first,
            "https://www.linkedin.com/posts/alice_demo-activity-12345678901",
        )

    def test_url_without_activity_uses_stable_hash(self):
        url = "https://linkedin.com/posts/alice-stage-data"
        self.assertEqual(
            xray.linkedin_post_identity(url),
            xray.linkedin_post_identity(url + "?utm_source=share"),
        )
        self.assertTrue(xray.linkedin_post_identity(url).startswith("linkedin_post:"))

    def test_duckduckgo_redirect_is_unwrapped_without_network(self):
        redirected = (
            "https://duckduckgo.com/l/?uddg=https%3A%2F%2Flinkedin.com%2Fposts%2F"
            "alice_demo-activity-12345678901%3Futm_source%3Dx"
        )
        self.assertEqual(
            xray.canonicalize_linkedin_post_url(redirected),
            "https://www.linkedin.com/posts/alice_demo-activity-12345678901",
        )


class TestDuckDuckGoProvider(unittest.TestCase):
    def test_zero_results_is_empty_and_never_contacts_linkedin(self):
        session = FakeSession(FakeResponse(200, "<html><p>No results.</p></html>"))
        provider = xray.DuckDuckGoHtmlProvider(session=session)

        self.assertEqual(provider.search("site:linkedin.com/posts stage"), [])
        self.assertEqual(len(session.calls), 1)
        url, kwargs = session.calls[0]
        self.assertEqual(url, xray.DUCKDUCKGO_HTML_ENDPOINT)
        self.assertNotIn("linkedin.com", url)
        self.assertFalse(kwargs["allow_redirects"])

    def test_http_202_challenge_is_blocked(self):
        session = FakeSession(FakeResponse(202, "<form class='challenge-form'>captcha</form>"))
        provider = xray.DuckDuckGoHtmlProvider(session=session)
        with self.assertRaises(xray.XRayBlockedError) as caught:
            provider.search("site:linkedin.com/posts stage")
        self.assertEqual(caught.exception.status_code, 202)

    def test_challenge_marker_on_200_is_blocked(self):
        session = FakeSession(FakeResponse(200, "Bots use DuckDuckGo"))
        provider = xray.DuckDuckGoHtmlProvider(session=session)
        with self.assertRaises(xray.XRayBlockedError):
            provider.search("site:linkedin.com/posts stage")

    def test_redirect_is_blocked_instead_of_counted_as_empty(self):
        session = FakeSession(FakeResponse(302, ""))
        provider = xray.DuckDuckGoHtmlProvider(session=session)
        with self.assertRaises(xray.XRayBlockedError) as caught:
            provider.search("site:linkedin.com/posts stage")
        self.assertEqual(caught.exception.status_code, 302)

    def test_timeout_has_typed_error(self):
        provider = xray.DuckDuckGoHtmlProvider(
            session=FakeSession(error=requests.Timeout("slow"))
        )
        with self.assertRaises(xray.XRayTimeoutError):
            provider.search("site:linkedin.com/posts stage")

    def test_provider_endpoint_cannot_be_linkedin(self):
        with self.assertRaises(ValueError):
            xray.DuckDuckGoHtmlProvider(endpoint="https://linkedin.com/posts/")


def sample_domains():
    domains = {
        "data_ia": {
            "priority": 3,
            "queries": [
                "stage data engineer",
                "stage data scientist",
                "stage machine learning",
                "stage business intelligence",
            ],
        }
    }
    for index in range(1, 16):
        domains[f"domain_{index}"] = {
            "priority": 1,
            "queries": [f"stage métier {index}"],
        }
    return domains


class TestXRayQueryPlan(unittest.TestCase):
    def test_fast_budget_is_globally_capped_at_three(self):
        domains = sample_domains()
        plan, state = xray.build_xray_query_plan(
            profile_id="fast_data_ia",
            domains=domains,
            domain_ids=["data_ia"],
            query_budget=99,
        )
        self.assertEqual(len(plan), 3)
        self.assertEqual({item.domain_id for item in plan}, {"data_ia"})
        self.assertEqual(len({item.query for item in plan}), 3)
        self.assertEqual(state.ring_cursor, 3)

    def test_full_rotation_is_deterministic_prioritized_and_complete(self):
        domains = sample_domains()
        domain_ids = list(domains)
        state = xray.XRayRotationState()
        seen = []
        for _ in range(3):
            plan, state = xray.build_xray_query_plan(
                profile_id="full_general",
                domains=domains,
                domain_ids=domain_ids,
                query_budget=6,
                state=state,
            )
            self.assertEqual(len(plan), 6)
            seen.extend(item.domain_id for item in plan)

        # Un cycle pondéré: priorité 3 pour Data/IA, 1 pour chacun des 15
        # autres domaines. Tous sont couverts sans multiplier le budget/run.
        self.assertEqual(set(seen), set(domain_ids))
        self.assertEqual(seen.count("data_ia"), 3)
        for domain_id in domain_ids[1:]:
            self.assertEqual(seen.count(domain_id), 1)

        first, first_state = xray.build_xray_query_plan(
            profile_id="full_general", domains=domains,
            domain_ids=domain_ids, query_budget=6,
        )
        again, again_state = xray.build_xray_query_plan(
            profile_id="full_general", domains=domains,
            domain_ids=domain_ids, query_budget=6,
        )
        self.assertEqual(first, again)
        self.assertEqual(first_state, again_state)

    def test_state_round_trip_is_pure_and_serializable(self):
        domains = sample_domains()
        original = {"ring_cursor": 4, "host_cursor": 2,
                    "query_cursors": {"data_ia": 1}}
        plan, next_state = xray.build_xray_query_plan(
            profile_id="full_general",
            domains=domains,
            domain_ids=list(domains),
            query_budget=2,
            state=original,
        )
        self.assertEqual(len(plan), 2)
        self.assertEqual(original, {"ring_cursor": 4, "host_cursor": 2,
                                    "query_cursors": {"data_ia": 1}})
        self.assertEqual(
            xray.XRayRotationState.from_mapping(next_state.as_dict()), next_state
        )

    def test_unknown_domain_and_negative_budget_are_rejected(self):
        domains = sample_domains()
        with self.assertRaises(ValueError):
            xray.build_xray_query_plan(
                profile_id="full_general", domains=domains,
                domain_ids=["missing"], query_budget=1,
            )
        with self.assertRaises(ValueError):
            xray.build_xray_query_plan(
                profile_id="full_general", domains=domains,
                domain_ids=["data_ia"], query_budget=-1,
            )


if __name__ == "__main__":
    unittest.main()
