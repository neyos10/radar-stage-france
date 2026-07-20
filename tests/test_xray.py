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

    def test_candidate_open_to_work_post_is_rejected(self):
        # Faux positif réel de production : post « Open to Work » d'une
        # candidate — le vocabulaire de recherche est présent mais l'auteure
        # cherche un poste, elle n'en propose pas.
        result = xray.SearchResult(
            title=("Open to Work | Ingénieure Méthodes • Industrialisation • "
                   "Conception Mécanique Je suis aujourd'hui à la recherche "
                   "d'une nouvelle opportunité"),
            snippet="Je recherche un stage en industrialisation. Voici mon CV.",
            url="https://linkedin.com/posts/k_opentowork-activity-12345678902",
        )
        self.assertFalse(xray.is_relevant_internship_post(result))

    def test_candidate_seeking_variants_are_rejected_but_employers_pass(self):
        candidate_texts = [
            "#OpenToWork disponible dès maintenant",
            "Je suis actuellement à la recherche d'un stage PFE",
            "Je cherche un stage en data science, voici mon cv",
            "I am currently looking for an internship in finance",
        ]
        for snippet in candidate_texts:
            result = xray.SearchResult(
                title="Stage data", snippet=snippet,
                url="https://linkedin.com/posts/c_stage-activity-12345678903",
            )
            self.assertFalse(xray.is_relevant_internship_post(result), snippet)

        employers = [
            "Nous recherchons un stagiaire data engineer. Envoyez votre CV !",
            "Stage de 6 mois à pourvoir dès septembre, postulez ici.",
            "Notre équipe recrute un stagiaire en mécanique. Candidature ouverte.",
        ]
        for snippet in employers:
            result = xray.SearchResult(
                title="Offre de stage", snippet=snippet,
                url="https://linkedin.com/posts/e_stage-activity-12345678904",
            )
            self.assertTrue(xray.is_relevant_internship_post(result), snippet)


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


class FakeJsonResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class FakePostSession:
    def __init__(self, response=None, error=None):
        self.response = response or FakeJsonResponse()
        self.error = error
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        return self.response


def exa_item(url="https://www.linkedin.com/posts/acme_stage-activity-7351000000000000001",
            title="Alice Martin sur LinkedIn : Stage Data Engineer",
            published_date="2026-07-16", author="Alice Martin",
            text="Nous recherchons un stagiaire data engineer.", highlights=None):
    item = {"url": url, "title": title, "publishedDate": published_date,
            "author": author, "text": text}
    if highlights is not None:
        item["highlights"] = highlights
    return item


class TestExaProviderConstruction(unittest.TestCase):
    def test_rejects_empty_api_key(self):
        with self.assertRaises(ValueError):
            xray.ExaProvider("")

    def test_rejects_non_exa_endpoint(self):
        with self.assertRaises(ValueError):
            xray.ExaProvider("key", endpoint="https://evil.example.com/search")


class TestExaProviderRequest(unittest.TestCase):
    def test_request_targets_exa_with_api_key_header_and_domain_filter(self):
        session = FakePostSession(FakeJsonResponse(200, {"results": []}))
        provider = xray.ExaProvider("secret-key", session=session)
        provider.search('site:linkedin.com/posts "stage" "data"', now=NOW)
        url, kwargs = session.calls[0]
        self.assertEqual(url, xray.EXA_SEARCH_ENDPOINT)
        self.assertEqual(kwargs["headers"]["x-api-key"], "secret-key")
        body = kwargs["json"]
        self.assertEqual(body["includeDomains"], list(xray.LINKEDIN_POST_HOSTS))
        # L'opérateur site: est retiré : Exa restreint déjà via includeDomains.
        self.assertNotIn("site:", body["query"])
        self.assertIn('"stage"', body["query"])
        self.assertEqual(body["type"], "fast")

    def test_query_reformatted_as_quoted_terms_plus_region(self):
        # Format mesuré comme le plus pertinent en conditions réelles :
        # termes entre guillemets suivis du nom de la région en clair,
        # jamais l'opérateur site: brut ni la requête DuckDuckGo telle quelle.
        session = FakePostSession(FakeJsonResponse(200, {"results": []}))
        provider = xray.ExaProvider("key", session=session, region_hint="France")
        provider.search('site:linkedin.com/posts "stage" "data engineer"', now=NOW)
        body = session.calls[0][1]["json"]
        self.assertEqual(body["query"], '"stage" "data engineer" France')

    def test_single_quoted_variant_still_gets_region_appended(self):
        session = FakePostSession(FakeJsonResponse(200, {"results": []}))
        provider = xray.ExaProvider("key", session=session, region_hint="France")
        provider.search('site:linkedin.com/posts "stage data scientist"', now=NOW)
        body = session.calls[0][1]["json"]
        self.assertEqual(body["query"], '"stage data scientist" France')

    def test_region_hint_is_configurable_per_provider_instance(self):
        session = FakePostSession(FakeJsonResponse(200, {"results": []}))
        provider = xray.ExaProvider("key", session=session, region_hint="Belgique")
        provider.search('site:linkedin.com/posts "stage" "data"', now=NOW)
        body = session.calls[0][1]["json"]
        self.assertTrue(body["query"].endswith("Belgique"))

    def test_native_window_defaults_to_48h_around_now(self):
        session = FakePostSession(FakeJsonResponse(200, {"results": []}))
        provider = xray.ExaProvider("key", session=session)
        provider.search("stage data", now=NOW)
        body = session.calls[0][1]["json"]
        start = datetime.fromisoformat(body["startPublishedDate"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(body["endPublishedDate"].replace("Z", "+00:00"))
        self.assertEqual(end, NOW)
        self.assertEqual(NOW - start, timedelta(hours=48))

    def test_second_page_returns_empty_without_network_call(self):
        session = FakePostSession(FakeJsonResponse(200, {"results": []}))
        provider = xray.ExaProvider("key", session=session)
        results = provider.search("stage data", page=2, now=NOW)
        self.assertEqual(results, [])
        self.assertEqual(session.calls, [])

    def test_numresults_clamped_to_hundred(self):
        session = FakePostSession(FakeJsonResponse(200, {"results": []}))
        provider = xray.ExaProvider("key", session=session)
        provider.search("stage data", max_results=500, now=NOW)
        self.assertEqual(session.calls[0][1]["json"]["numResults"], 100)


class TestExaProviderResponseParsing(unittest.TestCase):
    def test_parses_title_url_author_snippet(self):
        session = FakePostSession(FakeJsonResponse(200, {"results": [
            exa_item(highlights=["Nous recherchons un stagiaire data engineer."]),
        ]}))
        provider = xray.ExaProvider("key", session=session)
        results = provider.search("stage data", now=NOW)
        self.assertEqual(len(results), 1)
        result = results[0]
        self.assertEqual(result.provider, "exa")
        self.assertIn("Stage Data Engineer", result.title)
        self.assertIn("stagiaire", result.snippet)
        self.assertTrue(result.url.startswith("https://www.linkedin.com/posts/"))

    def test_non_linkedin_result_is_discarded(self):
        session = FakePostSession(FakeJsonResponse(200, {"results": [
            exa_item(url="https://example.com/not-linkedin"),
        ]}))
        provider = xray.ExaProvider("key", session=session)
        self.assertEqual(provider.search("stage data", now=NOW), [])

    def test_day_only_published_date_without_time_hint_is_unverifiable(self):
        # publishedDate = simple date calendaire, aucune heure dans le texte :
        # non vérifiable à l'heure près, jamais ramené silencieusement à minuit.
        session = FakePostSession(FakeJsonResponse(200, {"results": [
            exa_item(published_date="2026-07-16",
                     text="Nous recherchons un stagiaire data engineer."),
        ]}))
        provider = xray.ExaProvider("key", session=session)
        result = provider.search("stage data", now=NOW)[0]
        self.assertIsNone(result.published_at)

    def test_relative_time_in_text_is_resolved_precisely(self):
        session = FakePostSession(FakeJsonResponse(200, {"results": [
            exa_item(text="Nous recherchons un stagiaire. Publié il y a 3 heures."),
        ]}))
        provider = xray.ExaProvider("key", session=session)
        result = provider.search("stage data", now=NOW)[0]
        self.assertIsNotNone(result.published_at)
        self.assertEqual(result.published_at, NOW - timedelta(hours=3))

    def test_published_date_with_explicit_time_is_resolved_precisely(self):
        session = FakePostSession(FakeJsonResponse(200, {"results": [
            exa_item(published_date="2026-07-16T09:30:00.000Z",
                     text="Nous recherchons un stagiaire data engineer."),
        ]}))
        provider = xray.ExaProvider("key", session=session)
        result = provider.search("stage data", now=NOW)[0]
        self.assertEqual(
            result.published_at,
            datetime(2026, 7, 16, 9, 30, tzinfo=timezone.utc),
        )

    def test_malformed_results_are_skipped_not_fatal(self):
        session = FakePostSession(FakeJsonResponse(200, {"results": [
            "not-a-dict", {"url": ""}, exa_item(),
        ]}))
        provider = xray.ExaProvider("key", session=session)
        self.assertEqual(len(provider.search("stage data", now=NOW)), 1)


def tavily_item(url="https://fr.linkedin.com/posts/alice_recrutement-stage-activity-7475000000000000001",
                title="Post de Alice Martin",
                content="Nous recherchons un stagiaire data engineer. Postulez !"):
    return {"url": url, "title": title, "content": content, "score": 0.8}


class TestTavilyProviderConstruction(unittest.TestCase):
    def test_rejects_empty_api_key(self):
        with self.assertRaises(ValueError):
            xray.TavilyProvider("")

    def test_rejects_non_tavily_endpoint(self):
        with self.assertRaises(ValueError):
            xray.TavilyProvider("key", endpoint="https://evil.example.com/search")


class TestTavilyProviderRequest(unittest.TestCase):
    def test_request_shape_bearer_domains_and_day_window(self):
        session = FakePostSession(FakeJsonResponse(200, {"results": []}))
        provider = xray.TavilyProvider("tvly-key", session=session)
        provider.search('site:linkedin.com/posts "stage" "data engineer"', now=NOW)
        url, kwargs = session.calls[0]
        self.assertEqual(url, xray.TAVILY_SEARCH_ENDPOINT)
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer tvly-key")
        body = kwargs["json"]
        self.assertEqual(body["include_domains"], list(xray.LINKEDIN_POST_HOSTS))
        self.assertEqual(body["time_range"], "day")
        self.assertEqual(body["search_depth"], "basic")
        # Format mesuré en conditions réelles : « post » + termes cités +
        # « recrutement » + région, jamais l'opérateur site: brut.
        self.assertNotIn("site:", body["query"])
        self.assertEqual(body["query"],
                         'post "stage" "data engineer" recrutement France')

    def test_second_page_returns_empty_without_spending_a_credit(self):
        session = FakePostSession(FakeJsonResponse(200, {"results": []}))
        provider = xray.TavilyProvider("key", session=session)
        self.assertEqual(provider.search("stage data", page=2, now=NOW), [])
        self.assertEqual(session.calls, [])


class TestTavilyProviderResponseParsing(unittest.TestCase):
    def test_post_parsed_with_source_fresh_and_french_author(self):
        session = FakePostSession(FakeJsonResponse(200, {"results": [tavily_item()]}))
        provider = xray.TavilyProvider("key", session=session)
        results = provider.search("stage data", now=NOW)
        self.assertEqual(len(results), 1)
        result = results[0]
        self.assertEqual(result.provider, "tavily")
        self.assertEqual(result.author, "Alice Martin")
        self.assertTrue(result.source_fresh)
        self.assertIsNone(result.published_at)
        self.assertTrue(result.url.startswith("https://www.linkedin.com/posts/"))

    def test_non_post_linkedin_urls_are_discarded(self):
        # Constaté en conditions réelles : Tavily renvoie surtout des pages
        # /jobs/ (déjà couvertes par JobSpy) et du bruit SEO — seuls les
        # /posts/ sont conservés.
        session = FakePostSession(FakeJsonResponse(200, {"results": [
            tavily_item(url="https://fr.linkedin.com/jobs/view/stagiaire-at-acme-4441448547"),
            tavily_item(url="https://www.linkedin.com/in/alice-martin"),
            tavily_item(),
        ]}))
        provider = xray.TavilyProvider("key", session=session)
        self.assertEqual(len(provider.search("stage data", now=NOW)), 1)

    def test_precise_relative_date_in_content_is_parsed(self):
        session = FakePostSession(FakeJsonResponse(200, {"results": [
            tavily_item(content="Il y a 3 heures — nous recrutons un stagiaire."),
        ]}))
        provider = xray.TavilyProvider("key", session=session)
        result = provider.search("stage data", now=NOW)[0]
        self.assertEqual(result.published_at, NOW - timedelta(hours=3))


class TestTavilyProviderErrors(unittest.TestCase):
    def test_432_plan_limit_has_actionable_message(self):
        session = FakePostSession(FakeJsonResponse(432, {}))
        provider = xray.TavilyProvider("key", session=session)
        with self.assertRaises(xray.XRayBlockedError) as caught:
            provider.search("stage data", now=NOW)
        self.assertIn("crédits Tavily épuisés", str(caught.exception))

    def test_401_and_429_are_blocked(self):
        for code in (401, 429):
            provider = xray.TavilyProvider(
                "key", session=FakePostSession(FakeJsonResponse(code, {}))
            )
            with self.assertRaises(xray.XRayBlockedError):
                provider.search("stage data", now=NOW)

    def test_timeout_is_typed(self):
        provider = xray.TavilyProvider(
            "key", session=FakePostSession(error=requests.Timeout("slow"))
        )
        with self.assertRaises(xray.XRayTimeoutError):
            provider.search("stage data", now=NOW)


class TestExaProviderErrors(unittest.TestCase):
    def test_401_is_blocked_not_silently_empty(self):
        session = FakePostSession(FakeJsonResponse(401, {}))
        provider = xray.ExaProvider("bad-key", session=session)
        with self.assertRaises(xray.XRayBlockedError):
            provider.search("stage data", now=NOW)

    def test_402_no_more_credits_has_actionable_message(self):
        # Constaté en production : crédits Exa épuisés après ~19 runs
        # horaires. Le message doit dire quoi faire, pas juste un code.
        session = FakePostSession(FakeJsonResponse(402, {}))
        provider = xray.ExaProvider("key", session=session)
        with self.assertRaises(xray.XRayBlockedError) as caught:
            provider.search("stage data", now=NOW)
        self.assertEqual(caught.exception.status_code, 402)
        self.assertIn("crédits Exa épuisés", str(caught.exception))
        self.assertIn("dashboard.exa.ai", str(caught.exception))

    def test_fallback_attempts_record_each_provider_error(self):
        # La chaîne relance l'erreur du DERNIER repli : les messages de
        # chaque tentative doivent rester consultables pour le diagnostic.
        exa = xray.ExaProvider("key", session=FakePostSession(FakeJsonResponse(402, {})))
        ddg = xray.DuckDuckGoHtmlProvider(
            session=FakeSession(FakeResponse(202, "challenge-form"))
        )
        chain = xray.FallbackXRayProvider(exa, ddg)
        with self.assertRaises(xray.XRayBlockedError) as caught:
            chain.search("stage data", now=NOW)
        # L'exception propagée est celle du repli (202)…
        self.assertEqual(caught.exception.status_code, 202)
        # …mais la cause primaire (Exa 402) reste dans les tentatives.
        self.assertEqual(len(chain.last_attempts), 2)
        self.assertIn("crédits Exa épuisés", chain.last_attempts[0]["error"])
        self.assertIn("DuckDuckGo", chain.last_attempts[1]["error"])

    def test_429_is_blocked(self):
        session = FakePostSession(FakeJsonResponse(429, {}))
        provider = xray.ExaProvider("key", session=session)
        with self.assertRaises(xray.XRayBlockedError):
            provider.search("stage data", now=NOW)

    def test_timeout_is_typed(self):
        session = FakePostSession(error=requests.Timeout("slow"))
        provider = xray.ExaProvider("key", session=session)
        with self.assertRaises(xray.XRayTimeoutError):
            provider.search("stage data", now=NOW)

    def test_other_http_error_is_provider_error(self):
        session = FakePostSession(FakeJsonResponse(500, {}))
        provider = xray.ExaProvider("key", session=session)
        with self.assertRaises(xray.XRayProviderError):
            provider.search("stage data", now=NOW)


if __name__ == "__main__":
    unittest.main()
