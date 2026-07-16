"""Preuve de non-accès direct à LinkedIn et contrat du fallback X-ray."""

import unittest
from datetime import datetime, timezone

from radar_xray import (
    DUCKDUCKGO_LITE_ENDPOINT,
    DuckDuckGoHtmlProvider,
    DuckDuckGoLiteProvider,
    FallbackXRayProvider,
    LinkedInDestinationError,
    SearchResult,
    XRayBlockedError,
    XRayProviderError,
    XRayTimeoutError,
    assert_allowed_search_destination,
)


NOW = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)


class RecordingSession:
    """Session factice : enregistre les URL demandées sans réseau."""

    def __init__(self, status_code=200, text=""):
        self.requested_urls = []
        self.status_code = status_code
        self.text = text

    def get(self, url, **kwargs):
        self.requested_urls.append(url)
        session = self

        class _Response:
            status_code = session.status_code
            text = session.text

            def raise_for_status(self):
                return None

        return _Response()


class SuccessProvider:
    name = "primary_ok"

    def __init__(self):
        self.calls = 0

    def search(self, query, freshness=None, **kwargs):
        self.calls += 1
        return [SearchResult(
            title="Alice Martin sur LinkedIn : Stage Data Engineer",
            snippet="Nous recherchons un stagiaire data engineer. Postulez !",
            url=("https://www.linkedin.com/posts/alice_stage-data-"
                 "activity-7351000000000000000"),
            author="Alice Martin",
            published_at=NOW,
            provider=self.name,
        )]


class FailingProvider:
    def __init__(self, name, error_factory):
        self.name = name
        self.error_factory = error_factory
        self.calls = 0

    def search(self, *_args, **_kwargs):
        self.calls += 1
        raise self.error_factory()


class TestLinkedInDirectAccessGuard(unittest.TestCase):
    def test_linkedin_destinations_are_rejected(self):
        for url in (
            "https://linkedin.com/posts/xyz-activity-123",
            "https://fr.linkedin.com/posts/xyz",
            "https://www.linkedin.com/posts/xyz",
        ):
            with self.assertRaises(LinkedInDestinationError):
                assert_allowed_search_destination(url)

    def test_search_engine_destination_is_allowed(self):
        assert_allowed_search_destination("https://html.duckduckgo.com/html/")
        assert_allowed_search_destination(DUCKDUCKGO_LITE_ENDPOINT)

    def test_collector_cannot_open_a_linkedin_url_even_if_misconfigured(self):
        # Ceinture : même en contournant la validation du constructeur, la
        # garde par requête refuse d'émettre l'appel réseau vers LinkedIn.
        session = RecordingSession()
        provider = DuckDuckGoHtmlProvider(session)
        provider.endpoint = "https://www.linkedin.com/posts/target"
        with self.assertRaises(LinkedInDestinationError):
            provider.search("site:linkedin.com/posts stage data")
        self.assertEqual(session.requested_urls, [])

    def test_constructor_still_rejects_non_duckduckgo_endpoints(self):
        with self.assertRaises(ValueError):
            DuckDuckGoHtmlProvider(endpoint="https://linkedin.com/posts/x")

    def test_lite_provider_targets_duckduckgo_only(self):
        session = RecordingSession(text="<html></html>")
        provider = DuckDuckGoLiteProvider(session)
        provider.search("site:linkedin.com/posts stage", now=NOW)
        self.assertEqual(len(session.requested_urls), 1)
        self.assertIn("duckduckgo.com", session.requested_urls[0])
        self.assertNotIn("linkedin", session.requested_urls[0])


class TestFallbackProvider(unittest.TestCase):
    def test_primary_success_never_calls_fallback(self):
        primary = SuccessProvider()
        fallback = FailingProvider("secondary", lambda: XRayProviderError("x"))
        provider = FallbackXRayProvider(primary, fallback)
        results = provider.search("q", now=NOW)
        self.assertEqual(len(results), 1)
        self.assertEqual(fallback.calls, 0)
        self.assertEqual(provider.last_used_provider, "primary_ok")
        self.assertEqual(
            provider.last_attempts,
            [{"provider": "primary_ok", "outcome": "success"}],
        )

    def test_single_fallback_when_primary_is_blocked(self):
        primary = FailingProvider(
            "primary", lambda: XRayBlockedError("challenge", status_code=202)
        )
        fallback = SuccessProvider()
        fallback.name = "secondary_ok"
        provider = FallbackXRayProvider(primary, fallback)
        results = provider.search("q", now=NOW)
        self.assertEqual(len(results), 1)
        self.assertEqual(primary.calls, 1)
        self.assertEqual(provider.last_used_provider, "secondary_ok")
        self.assertEqual(
            [attempt["outcome"] for attempt in provider.last_attempts],
            ["blocked", "success"],
        )

    def test_both_failing_raise_without_any_loop(self):
        primary = FailingProvider("p", lambda: XRayBlockedError("b"))
        fallback = FailingProvider("s", lambda: XRayTimeoutError("t"))
        provider = FallbackXRayProvider(primary, fallback)
        with self.assertRaises(XRayTimeoutError):
            provider.search("q")
        self.assertEqual(primary.calls, 1)
        self.assertEqual(fallback.calls, 1)

    def test_circuit_breaker_skips_broken_provider_then_recovers(self):
        primary = FailingProvider("p", lambda: XRayBlockedError("b"))
        fallback = SuccessProvider()
        fallback.name = "s"
        provider = FallbackXRayProvider(primary, fallback, breaker_threshold=2)
        provider.search("q1")
        provider.search("q2")
        self.assertEqual(primary.calls, 2)
        provider.search("q3")
        # Après deux échecs consécutifs, le principal n'est plus appelé.
        self.assertEqual(primary.calls, 2)
        self.assertEqual(
            provider.last_attempts[0],
            {"provider": "p", "outcome": "skipped_circuit_open"},
        )
        self.assertEqual(provider.last_used_provider, "s")

    def test_all_circuits_open_is_a_typed_error(self):
        primary = FailingProvider("p", lambda: XRayBlockedError("b"))
        fallback = FailingProvider("s", lambda: XRayBlockedError("b"))
        provider = FallbackXRayProvider(primary, fallback, breaker_threshold=1)
        with self.assertRaises(XRayBlockedError):
            provider.search("q1")
        with self.assertRaises(XRayProviderError):
            provider.search("q2")
        # Aucun appel supplémentaire : les deux circuits sont ouverts.
        self.assertEqual(primary.calls, 1)
        self.assertEqual(fallback.calls, 1)


if __name__ == "__main__":
    unittest.main()
