"""Recherche X-ray de posts LinkedIn publics via un moteur de recherche public.

Ce module ne contacte jamais LinkedIn. Deux familles de providers sont
disponibles :

- ``ExaProvider`` : API officielle Exa (https://exa.ai), qui autorise
  explicitement l'usage programmatique — aucune notion de contournement.
  Provider recommandé et prioritaire si ``EXA_API_KEY`` est fourni.
- ``DuckDuckGoHtmlProvider``/``DuckDuckGoLiteProvider`` : scraping best-effort
  de l'interface HTML DuckDuckGo, conservés en repli historique. Mesuré en
  conditions réelles (environnement de développement et runners GitHub
  Actions), ce moteur soumet systématiquement ces requêtes à son défi
  anti-bot (« Unfortunately, bots use DuckDuckGo too », CAPTCHA visuel) : la
  garde ``XRayBlockedError`` détecte ce blocage et ne tente jamais de le
  contourner (aucun CAPTCHA n'est résolu par ce module).

Dans tous les cas, seuls le titre, le texte et l'URL déjà exposés par le
moteur sont exploités. Le transport est injectable pour rendre tous les
comportements testables sans réseau.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any, Mapping, Protocol, Sequence
from urllib.parse import parse_qs, quote, unquote, urljoin, urlsplit, urlunsplit

import requests


DUCKDUCKGO_HTML_ENDPOINT = "https://html.duckduckgo.com/html/"
DUCKDUCKGO_PROVIDER_NAME = "duckduckgo_html"
DUCKDUCKGO_LITE_ENDPOINT = "https://lite.duckduckgo.com/lite/"
DUCKDUCKGO_LITE_PROVIDER_NAME = "duckduckgo_lite"
EXA_SEARCH_ENDPOINT = "https://api.exa.ai/search"
EXA_PROVIDER_NAME = "exa"
LINKEDIN_POST_HOSTS = ("linkedin.com", "fr.linkedin.com")

_SPACE_RE = re.compile(r"\s+")
_MULTI_SLASH_RE = re.compile(r"/{2,}")
_CONTRACT_WORD_RE = re.compile(
    r"\b(stage|stagiaire|intern|internship|pfa|pfe)\b", re.IGNORECASE
)
_RECRUITING_INTENT_RE = re.compile(
    r"\b(recherche|recherchons|recrute|recrutons|recrutement|à\s+pourvoir|"
    r"a\s+pourvoir|candidature|candidatez|postulez|envoyez.{0,20}\bcv\b|"
    r"hiring|looking\s+for|apply)\b",
    re.IGNORECASE,
)
# Posts côté candidat (« open to work », « je suis à la recherche… ») : le
# vocabulaire de recherche y est présent mais l'auteur cherche un poste, il
# n'en propose pas. Constaté en production : un post « Open to Work |
# Ingénieure Méthodes… je suis à la recherche d'une nouvelle opportunité »
# passait le filtre d'intention et polluait le dashboard.
_CANDIDATE_POST_RE = re.compile(
    r"open\s*to\s*work|#?opentowork|"
    r"\bje\s+(?:suis\s+(?:actuellement\s+)?(?:à|a)\s+la\s+recherche|"
    r"recherche\s+(?:un|une)\b|cherche\s+(?:un|une)\b)|"
    r"(?:à|a)\s+la\s+recherche\s+d['e]\s*(?:un\s+stage|une\s+(?:nouvelle\s+)?"
    r"opportunit)|"
    r"\bi\s+am\s+(?:currently\s+)?looking\s+for\b|\bseeking\s+an?\s+internship\b|"
    r"\bmon\s+cv\b|\bmy\s+resume\b",
    re.IGNORECASE,
)
_AUTHOR_RE = re.compile(
    r"^\s*(?P<author>.+?)\s+(?:on|sur)\s+LinkedIn\s*[:|\-]",
    re.IGNORECASE,
)
_ACTIVITY_ID_RE = re.compile(
    r"(?:activity|ugcpost)[\-_:/%]+(?P<id>\d{8,})", re.IGNORECASE
)
_BLOCK_MARKERS = (
    "anomaly-modal",
    "anomaly.js",
    "bots use duckduckgo",
    "captcha",
    "challenge-form",
    "select all squares",
)


class XRayProviderError(RuntimeError):
    """Erreur distante non récupérable d'un provider X-ray."""


class LinkedInDestinationError(RuntimeError):
    """Tentative interdite de requête réseau directe vers LinkedIn.

    Cette erreur signale un défaut de programmation, jamais un incident
    distant : le module X-ray n'a le droit d'ouvrir aucune URL LinkedIn,
    seulement de les afficher comme données.
    """


def assert_allowed_search_destination(url: str) -> None:
    """Refuse toute destination réseau LinkedIn, avant chaque requête."""
    hostname = (urlsplit(url).hostname or "").rstrip(".").lower()
    if hostname == "linkedin.com" or hostname.endswith(".linkedin.com"):
        raise LinkedInDestinationError(
            "Requête réseau directe vers LinkedIn interdite (mode X-ray strict)"
        )


class XRayBlockedError(XRayProviderError):
    """Le moteur a explicitement bloqué ou challengé la requête."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class XRayTimeoutError(XRayProviderError):
    """Le moteur n'a pas répondu dans le délai configuré."""


@dataclass(frozen=True)
class SearchResult:
    """Résultat minimal exposé par un moteur de recherche public."""

    title: str
    snippet: str
    url: str
    author: str | None = None
    published_at: datetime | None = None
    provider: str = DUCKDUCKGO_PROVIDER_NAME

    @property
    def external_id(self) -> str:
        """Identité stable d'un post, fondée sur l'activité ou son URL."""
        return linkedin_post_identity(self.url)


class XRaySearchProvider(Protocol):
    """Contrat remplaçable d'un provider de résultats X-ray."""

    name: str

    def search(
        self,
        query: str,
        freshness: str | None = None,
        *,
        page: int = 1,
        max_results: int = 10,
        now: datetime | None = None,
    ) -> list[SearchResult]:
        ...


def _clean_text(value: str | None) -> str:
    return _SPACE_RE.sub(" ", value or "").strip()


def _unwrap_duckduckgo_url(url: str) -> str:
    """Décode une redirection `/l/?uddg=...` sans effectuer de requête."""
    raw = _clean_text(url)
    if not raw:
        return ""
    absolute = urljoin("https://duckduckgo.com/", raw)
    parsed = urlsplit(absolute)
    if parsed.hostname and parsed.hostname.lower().endswith("duckduckgo.com"):
        target = parse_qs(parsed.query).get("uddg")
        if target:
            return unquote(target[0])
    return raw


def canonicalize_linkedin_post_url(url: str) -> str | None:
    """Retourne l'URL canonique d'un post LinkedIn, sinon ``None``.

    L'identité d'un post LinkedIn est portée par son chemin. Tous les paramètres
    et fragments sont donc retirés, y compris les variantes `utm_*`, `trk`,
    `trackingId`, `lipi` et `midToken`.
    """
    raw = _unwrap_duckduckgo_url(url)
    if not raw:
        return None
    if raw.startswith("//"):
        raw = "https:" + raw
    elif "://" not in raw:
        raw = "https://" + raw.lstrip("/")

    try:
        parsed = urlsplit(raw)
    except ValueError:
        return None
    hostname = (parsed.hostname or "").rstrip(".").lower()
    if hostname != "linkedin.com" and not hostname.endswith(".linkedin.com"):
        return None

    decoded_path = unquote(parsed.path or "/")
    path = _MULTI_SLASH_RE.sub("/", decoded_path)
    if not path.lower().startswith("/posts/") or path.rstrip("/") == "/posts":
        return None
    path = path.rstrip("/")
    # Ré-encoder uniquement les caractères non sûrs après normalisation.
    path = quote(path, safe="/:@-._~!$&'()*+,;=")
    return urlunsplit(("https", "www.linkedin.com", path, "", ""))


def linkedin_post_identity(url: str) -> str:
    canonical = canonicalize_linkedin_post_url(url)
    if canonical is None:
        raise ValueError("URL de post LinkedIn invalide")
    match = _ACTIVITY_ID_RE.search(unquote(urlsplit(canonical).path))
    if match:
        return f"linkedin_post:{match.group('id')}"
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
    return f"linkedin_post:{digest}"


def extract_linkedin_author(title: str) -> str | None:
    """Extrait seulement les signatures explicites « Nom on/sur LinkedIn »."""
    match = _AUTHOR_RE.search(_clean_text(title))
    if not match:
        return None
    author = _clean_text(match.group("author")).strip('"“”')
    if not author or len(author) > 120 or _CONTRACT_WORD_RE.search(author):
        return None
    return author


def parse_engine_date(text: str, now: datetime | None = None) -> datetime | None:
    """Interprète quelques dates courantes de snippets, toujours en UTC."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    value = _clean_text(text).lower().replace("’", "'")
    if not value:
        return None

    relative_patterns = (
        (r"(?:il y a\s+)?(\d+)\s*(?:min|minutes?)\b", "minutes"),
        (r"(?:il y a\s+)?(\d+)\s*(?:h|heures?)\b", "hours"),
        (r"(?:il y a\s+)?(\d+)\s*jours?\b", "days"),
        (r"(\d+)\s*minutes?\s+ago\b", "minutes"),
        (r"(\d+)\s*hours?\s+ago\b", "hours"),
        (r"(\d+)\s*days?\s+ago\b", "days"),
    )
    for pattern, unit in relative_patterns:
        match = re.search(pattern, value)
        if match:
            return now - timedelta(**{unit: int(match.group(1))})
    if re.search(r"\b(aujourd'hui|today)\b", value):
        return now
    if re.search(r"\b(hier|yesterday)\b", value):
        return now - timedelta(days=1)

    iso_match = re.search(r"\b(\d{4}-\d{2}-\d{2})(?:[t ](\d{2}:\d{2}(?::\d{2})?)(z|[+\-]\d{2}:?\d{2})?)?\b", value)
    if not iso_match:
        return None
    raw = iso_match.group(1)
    if iso_match.group(2):
        raw += "T" + iso_match.group(2) + (iso_match.group(3) or "+00:00")
    try:
        parsed = datetime.fromisoformat(raw.replace("z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class _DuckDuckGoResultParser(HTMLParser):
    """Parse les variantes HTML (`result__*`) et Lite (`result-*`)."""

    TITLE_CLASSES = {"result__a", "result-link"}
    SNIPPET_CLASSES = {"result__snippet", "result-snippet"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows: list[tuple[str, str, str]] = []
        self.current: dict[str, object] | None = None
        self.stack: list[tuple[str, str | None]] = []

    @staticmethod
    def _attrs(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
        return {key: value or "" for key, value in attrs}

    def _flush(self) -> None:
        if not self.current:
            return
        href = str(self.current.get("href") or "")
        title = _clean_text(" ".join(self.current.get("title", [])))
        snippet = _clean_text(" ".join(self.current.get("snippet", [])))
        if href and title:
            self.rows.append((title, snippet, href))
        self.current = None

    def handle_starttag(self, tag: str, attrs) -> None:
        attributes = self._attrs(attrs)
        classes = set(attributes.get("class", "").split())
        capture: str | None = None
        if tag == "a" and classes & self.TITLE_CLASSES:
            self._flush()
            self.current = {
                "href": attributes.get("href", ""),
                "title": [],
                "snippet": [],
            }
            capture = "title"
        elif self.current is not None and classes & self.SNIPPET_CLASSES:
            capture = "snippet"
        self.stack.append((tag, capture))

    def handle_startendtag(self, tag: str, attrs) -> None:
        if tag in {"br", "hr"}:
            self.handle_data(" ")

    def handle_endtag(self, tag: str) -> None:
        # Les pages du moteur peuvent être imparfaites : retirer jusqu'au tag
        # correspondant plutôt que supposer un HTML strictement équilibré.
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data: str) -> None:
        if self.current is None or not data.strip():
            return
        for _, capture in reversed(self.stack):
            if capture:
                self.current[capture].append(data)
                return

    def close(self) -> None:
        super().close()
        self._flush()


def deduplicate_search_results(results: Sequence[SearchResult]) -> list[SearchResult]:
    """Déduplique par identité de post en conservant le premier résultat."""
    unique: dict[str, SearchResult] = {}
    for result in results:
        canonical = canonicalize_linkedin_post_url(result.url)
        if canonical is None:
            continue
        normalized = SearchResult(
            title=_clean_text(result.title),
            snippet=_clean_text(result.snippet),
            url=canonical,
            author=result.author,
            published_at=result.published_at,
            provider=result.provider,
        )
        unique.setdefault(normalized.external_id, normalized)
    return list(unique.values())


def parse_duckduckgo_html(
    html_text: str, now: datetime | None = None
) -> list[SearchResult]:
    parser = _DuckDuckGoResultParser()
    parser.feed(html_text or "")
    parser.close()
    results = []
    for title, snippet, href in parser.rows:
        canonical = canonicalize_linkedin_post_url(href)
        if canonical is None:
            continue
        results.append(SearchResult(
            title=title,
            snippet=snippet,
            url=canonical,
            author=extract_linkedin_author(title),
            published_at=parse_engine_date(snippet, now),
        ))
    return deduplicate_search_results(results)


def is_relevant_internship_post(result: SearchResult) -> bool:
    """Filtre anti-bruit minimal avant le scoring multi-domaines existant."""
    text = f"{result.title} {result.snippet}"
    if _CANDIDATE_POST_RE.search(text):
        return False
    return bool(_CONTRACT_WORD_RE.search(text) and _RECRUITING_INTENT_RE.search(text))


def _looks_blocked(status_code: int, body: str) -> bool:
    if status_code in {202, 403, 429} or 300 <= status_code < 400:
        return True
    lowered = (body or "").lower()
    return any(marker in lowered for marker in _BLOCK_MARKERS)


class DuckDuckGoHtmlProvider:
    """Provider best-effort limité à l'hôte DuckDuckGo HTML."""

    name = DUCKDUCKGO_PROVIDER_NAME

    def __init__(
        self,
        session=None,
        *,
        timeout: float = 8.0,
        endpoint: str = DUCKDUCKGO_HTML_ENDPOINT,
    ):
        hostname = (urlsplit(endpoint).hostname or "").lower()
        if hostname != "duckduckgo.com" and not hostname.endswith(".duckduckgo.com"):
            raise ValueError("Le provider X-ray doit cibler uniquement DuckDuckGo")
        self.session = session or requests.Session()
        self.timeout = timeout
        self.endpoint = endpoint

    def search(
        self,
        query: str,
        freshness: str | None = None,
        *,
        page: int = 1,
        max_results: int = 10,
        now: datetime | None = None,
    ) -> list[SearchResult]:
        if not _clean_text(query):
            raise ValueError("La requête X-ray ne peut pas être vide")
        if page < 1 or max_results < 1:
            raise ValueError("page et max_results doivent être strictement positifs")
        params: dict[str, str | int] = {
            "q": query,
            "kl": "fr-fr",
            "s": (page - 1) * 30,
        }
        if freshness:
            params["df"] = freshness
        headers = {
            "Accept": "text/html,application/xhtml+xml",
            "User-Agent": "radar-stage-france-xray/1.0",
        }
        # Ceinture et bretelles : même si l'endpoint a été validé à la
        # construction, chaque requête revérifie qu'elle ne vise pas LinkedIn.
        assert_allowed_search_destination(self.endpoint)
        try:
            response = self.session.get(
                self.endpoint,
                params=params,
                headers=headers,
                timeout=self.timeout,
                # Ne jamais suivre une redirection susceptible de mener au
                # résultat LinkedIn découvert.
                allow_redirects=False,
            )
        except requests.Timeout as exc:
            raise XRayTimeoutError("Timeout DuckDuckGo HTML") from exc
        except requests.RequestException as exc:
            raise XRayProviderError(
                f"Erreur réseau DuckDuckGo: {type(exc).__name__}"
            ) from exc

        body = getattr(response, "text", "") or ""
        status_code = int(getattr(response, "status_code", 0) or 0)
        if _looks_blocked(status_code, body):
            raise XRayBlockedError(
                f"DuckDuckGo a bloqué la recherche (HTTP {status_code or '?'})",
                status_code=status_code or None,
            )
        try:
            response.raise_for_status()
        except requests.RequestException as exc:
            raise XRayProviderError(
                f"DuckDuckGo a répondu HTTP {status_code or '?'}"
            ) from exc
        return parse_duckduckgo_html(body, now)[:max_results]


class DuckDuckGoLiteProvider(DuckDuckGoHtmlProvider):
    """Provider de secours : interface Lite du même moteur DuckDuckGo.

    Le parseur partagé comprend déjà les classes ``result-*`` de cette
    variante. Le domaine reste strictement duckduckgo.com : aucune requête
    LinkedIn n'est possible ici non plus.
    """

    name = DUCKDUCKGO_LITE_PROVIDER_NAME

    def __init__(
        self,
        session=None,
        *,
        timeout: float = 8.0,
        endpoint: str = DUCKDUCKGO_LITE_ENDPOINT,
    ):
        super().__init__(session, timeout=timeout, endpoint=endpoint)


_SITE_PREFIX_RE = re.compile(r"^site:\S+\s*", re.IGNORECASE)
_QUOTED_TERM_RE = re.compile(r'"([^"]+)"')
# Une expression relative (« il y a 3 heures », « 2 hours ago »), un jour
# nommé ou une heure explicite HH:MM signale une date vérifiable à l'heure
# près. Une simple date calendaire (YYYY-MM-DD) sans rien de tout cela est
# une estimation, pas une preuve : voir ``_exa_published_at``.
_PRECISE_TIME_HINT_RE = re.compile(
    r"\b\d+\s*(?:min|minutes?|h|heures?|hours?|jours?|days?)\b|"
    r"\b(?:aujourd'hui|hier|today|yesterday)\b|"
    r"\d{2}:\d{2}",
    re.IGNORECASE,
)


def _adapt_query_for_exa(query: str, region_hint: str = "") -> str:
    """Reconstruit la requête au format qu'Exa comprend le mieux.

    Sans l'opérateur ``site:`` (Exa restreint déjà le domaine via
    ``includeDomains``), une requête purement sémantique dérive facilement
    vers des posts LinkedIn seulement adjacents au sujet. Mesuré en
    conditions réelles : reformuler en termes entre guillemets suivis du
    nom de la région en clair (ex. ``"stage" "data engineer" France``,
    plutôt que la requête ``site:...`` brute) ramène des posts nettement
    plus pertinents.
    """
    quoted_terms = _QUOTED_TERM_RE.findall(query)
    base = (
        " ".join(f'"{term}"' for term in quoted_terms) if quoted_terms
        else _SITE_PREFIX_RE.sub("", query).strip()
    )
    if region_hint:
        return f"{base} {region_hint}".strip()
    return base


def _exa_published_at(item: Mapping[str, Any], snippet: str,
                      now: datetime) -> datetime | None:
    """Date de publication Exa, seulement si vérifiable à l'heure près.

    Le champ ``publishedDate`` d'Exa est documenté comme une estimation au
    jour (``YYYY-MM-DD``), sans heure — insuffisant pour le mode strict 24 h
    de LinkedIn Posts. On n'accepte une date que si le texte source (extrait
    ou champ de date) porte une heure explicite ou une expression relative
    précise ; une simple date calendaire est traitée comme non vérifiable
    plutôt que silencieusement ramenée à minuit.
    """
    candidates = (snippet, str(item.get("publishedDate") or ""))
    for candidate in candidates:
        cleaned = _clean_text(candidate)
        if not cleaned or not _PRECISE_TIME_HINT_RE.search(cleaned.lower()):
            continue
        parsed = parse_engine_date(cleaned, now)
        if parsed is not None:
            return parsed
    return None


def _parse_exa_result(item: Any, now: datetime) -> SearchResult | None:
    if not isinstance(item, Mapping):
        return None
    canonical = canonicalize_linkedin_post_url(str(item.get("url") or ""))
    if canonical is None:
        return None
    title = _clean_text(str(item.get("title") or ""))
    if not title:
        return None
    highlights = item.get("highlights")
    highlight_text = (
        " ".join(str(part) for part in highlights)
        if isinstance(highlights, list) and highlights else ""
    )
    snippet = _clean_text(highlight_text or str(item.get("text") or ""))
    author = extract_linkedin_author(title)
    if author is None:
        raw_author = _clean_text(str(item.get("author") or ""))
        author = raw_author if raw_author and len(raw_author) <= 120 else None
    return SearchResult(
        title=title,
        snippet=snippet,
        url=canonical,
        author=author,
        published_at=_exa_published_at(item, snippet, now),
        provider=EXA_PROVIDER_NAME,
    )


class ExaProvider:
    """Provider basé sur l'API officielle Exa (https://exa.ai).

    Contrairement au scraping DuckDuckGo, Exa est un produit d'API dont les
    conditions d'usage autorisent explicitement l'appel programmatique :
    aucune notion de contournement d'anti-bot ici. Le domaine est restreint
    côté serveur (``includeDomains``) et une fenêtre de fraîcheur large est
    demandée nativement (``startPublishedDate``/``endPublishedDate``) en
    complément — jamais en remplacement — du contrôle strict local.
    """

    name = EXA_PROVIDER_NAME

    def __init__(
        self,
        api_key: str,
        session=None,
        *,
        timeout: float = 8.0,
        endpoint: str = EXA_SEARCH_ENDPOINT,
        native_window_hours: int = 48,
        region_hint: str = "France",
    ):
        if not api_key or not api_key.strip():
            raise ValueError("ExaProvider requiert une clé API non vide")
        hostname = (urlsplit(endpoint).hostname or "").lower()
        if hostname != "api.exa.ai":
            raise ValueError("Le provider Exa doit cibler api.exa.ai")
        self.api_key = api_key
        self.session = session or requests.Session()
        self.timeout = timeout
        self.endpoint = endpoint
        self.native_window_hours = native_window_hours
        self.region_hint = region_hint

    def search(
        self,
        query: str,
        freshness: str | None = None,
        *,
        page: int = 1,
        max_results: int = 10,
        now: datetime | None = None,
    ) -> list[SearchResult]:
        if not _clean_text(query):
            raise ValueError("La requête X-ray ne peut pas être vide")
        if page < 1 or max_results < 1:
            raise ValueError("page et max_results doivent être strictement positifs")
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        now = now.astimezone(timezone.utc)
        if page > 1:
            # Exa ne documente pas de pagination par décalage : tout le
            # budget est demandé en un seul appel, les pages suivantes
            # n'ont rien de plus à offrir.
            return []

        def iso(value: datetime) -> str:
            return value.strftime("%Y-%m-%dT%H:%M:%S.000Z")

        payload = {
            "query": _adapt_query_for_exa(query, self.region_hint),
            "type": "fast",
            "numResults": min(max_results, 100),
            "includeDomains": list(LINKEDIN_POST_HOSTS),
            "startPublishedDate": iso(now - timedelta(hours=self.native_window_hours)),
            "endPublishedDate": iso(now),
            "contents": {"text": {"maxCharacters": 500}, "highlights": True},
        }
        headers = {"x-api-key": self.api_key, "Content-Type": "application/json"}
        try:
            response = self.session.post(
                self.endpoint, json=payload, headers=headers, timeout=self.timeout,
            )
        except requests.Timeout as exc:
            raise XRayTimeoutError("Timeout Exa") from exc
        except requests.RequestException as exc:
            raise XRayProviderError(
                f"Erreur réseau Exa: {type(exc).__name__}"
            ) from exc

        status_code = int(getattr(response, "status_code", 0) or 0)
        if status_code == 402:
            # NO_MORE_CREDITS : constaté en production après ~19 runs
            # horaires. Message explicite pour que le dashboard dise quoi
            # faire au lieu d'un « bloqué 202 » hérité du repli DuckDuckGo.
            raise XRayBlockedError(
                "crédits Exa épuisés (HTTP 402) — recharger sur dashboard.exa.ai",
                status_code=status_code,
            )
        if status_code in {401, 403, 429}:
            raise XRayBlockedError(
                f"Exa a refusé la requête (HTTP {status_code})",
                status_code=status_code,
            )
        try:
            response.raise_for_status()
        except requests.RequestException as exc:
            raise XRayProviderError(
                f"Exa a répondu HTTP {status_code or '?'}"
            ) from exc
        try:
            body = response.json()
        except ValueError as exc:
            raise XRayProviderError("Réponse Exa invalide (JSON)") from exc

        raw_results = body.get("results") if isinstance(body, Mapping) else None
        if not isinstance(raw_results, list):
            return []
        parsed = [_parse_exa_result(item, now) for item in raw_results]
        return [result for result in parsed if result is not None][:max_results]


class FallbackXRayProvider:
    """Chaîne un provider principal et un secours, sans jamais boucler.

    Contrat :
    - au plus une bascule par requête (deux providers maximum) ;
    - circuit breaker par provider : après ``breaker_threshold`` échecs
      consécutifs, le provider est ignoré jusqu'à son prochain succès ;
    - chaque tentative est consignée dans ``last_attempts`` pour que
      l'appelant comptabilise le volume réseau réel du fallback.
    """

    def __init__(self, primary, fallback, *, breaker_threshold: int = 2):
        if breaker_threshold < 1:
            raise ValueError("breaker_threshold doit être strictement positif")
        self.providers = (primary, fallback)
        self.breaker_threshold = breaker_threshold
        self._consecutive_failures = {primary.name: 0, fallback.name: 0}
        self.name = f"{primary.name}+{fallback.name}"
        self.last_used_provider: str | None = None
        self.last_attempts: list[dict] = []

    def circuit_open(self, provider_name: str) -> bool:
        return (
            self._consecutive_failures.get(provider_name, 0)
            >= self.breaker_threshold
        )

    def search(
        self,
        query: str,
        freshness: str | None = None,
        *,
        page: int = 1,
        max_results: int = 10,
        now: datetime | None = None,
    ) -> list[SearchResult]:
        self.last_attempts = []
        self.last_used_provider = None
        last_error: Exception | None = None
        for provider in self.providers:
            if self.circuit_open(provider.name):
                self.last_attempts.append(
                    {"provider": provider.name, "outcome": "skipped_circuit_open"}
                )
                continue
            try:
                results = provider.search(
                    query, freshness, page=page,
                    max_results=max_results, now=now,
                )
            except XRayBlockedError as exc:
                self._consecutive_failures[provider.name] += 1
                self.last_attempts.append(
                    {"provider": provider.name, "outcome": "blocked",
                     "error": str(exc)[:160]}
                )
                last_error = exc
                continue
            except XRayTimeoutError as exc:
                self._consecutive_failures[provider.name] += 1
                self.last_attempts.append(
                    {"provider": provider.name, "outcome": "timeout",
                     "error": str(exc)[:160]}
                )
                last_error = exc
                continue
            except XRayProviderError as exc:
                self._consecutive_failures[provider.name] += 1
                self.last_attempts.append(
                    {"provider": provider.name, "outcome": "error",
                     "error": str(exc)[:160]}
                )
                last_error = exc
                continue
            self._consecutive_failures[provider.name] = 0
            self.last_attempts.append(
                {"provider": provider.name, "outcome": "success"}
            )
            self.last_used_provider = provider.name
            return results
        if last_error is not None:
            raise last_error
        raise XRayProviderError(
            "Tous les providers X-ray sont indisponibles (circuit breaker ouvert)"
        )


__all__ = [
    "DUCKDUCKGO_HTML_ENDPOINT",
    "DUCKDUCKGO_LITE_ENDPOINT",
    "EXA_SEARCH_ENDPOINT",
    "LINKEDIN_POST_HOSTS",
    "DuckDuckGoHtmlProvider",
    "DuckDuckGoLiteProvider",
    "ExaProvider",
    "FallbackXRayProvider",
    "LinkedInDestinationError",
    "SearchResult",
    "XRayBlockedError",
    "XRayProviderError",
    "XRaySearchProvider",
    "XRayTimeoutError",
    "assert_allowed_search_destination",
    "canonicalize_linkedin_post_url",
    "deduplicate_search_results",
    "extract_linkedin_author",
    "is_relevant_internship_post",
    "linkedin_post_identity",
    "parse_duckduckgo_html",
    "parse_engine_date",
]
