"""Recherche X-ray de posts LinkedIn publics via DuckDuckGo HTML.

Ce module ne contacte jamais LinkedIn. Il interroge uniquement l'interface
HTML de DuckDuckGo, puis exploite le titre, le snippet et l'URL déjà présents
dans les résultats du moteur. Le transport est injectable pour rendre tous les
comportements testables sans réseau.

La construction du plan de requêtes est volontairement pure : elle retourne
le prochain état de rotation, mais ne l'écrit jamais sur disque. L'appelant
reste responsable de sa persistance atomique hors mode benchmark.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Mapping, Protocol, Sequence
from urllib.parse import parse_qs, quote, unquote, urljoin, urlsplit, urlunsplit

import requests


DUCKDUCKGO_HTML_ENDPOINT = "https://html.duckduckgo.com/html/"
DUCKDUCKGO_PROVIDER_NAME = "duckduckgo_html"
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


@dataclass(frozen=True)
class XRayQuery:
    """Une requête du budget global, associée au domaine qu'elle couvre."""

    domain_id: str
    query: str
    host: str


@dataclass(frozen=True)
class XRayRotationState:
    """État sérialisable de la rotation, sans aucune opération d'E/S."""

    ring_cursor: int = 0
    host_cursor: int = 0
    query_cursors: tuple[tuple[str, int], ...] = ()

    @classmethod
    def from_mapping(cls, value: Mapping | None) -> "XRayRotationState":
        if not value:
            return cls()
        raw_cursors = value.get("query_cursors", {})
        if isinstance(raw_cursors, Mapping):
            cursors = tuple(
                sorted((str(key), max(0, int(cursor)))
                       for key, cursor in raw_cursors.items())
            )
        else:
            cursors = tuple(
                (str(key), max(0, int(cursor))) for key, cursor in raw_cursors
            )
        return cls(
            ring_cursor=max(0, int(value.get("ring_cursor", 0))),
            host_cursor=max(0, int(value.get("host_cursor", 0))),
            query_cursors=cursors,
        )

    def as_dict(self) -> dict:
        return {
            "ring_cursor": self.ring_cursor,
            "host_cursor": self.host_cursor,
            "query_cursors": dict(self.query_cursors),
        }


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


def _strip_contract_words(query: str) -> str:
    value = _CONTRACT_WORD_RE.sub(" ", _clean_text(query))
    return _SPACE_RE.sub(" ", value).strip(" -\"'") or _clean_text(query)


def _smooth_weighted_ring(
    domain_ids: Sequence[str], domains: Mapping[str, Mapping]
) -> list[str]:
    """Anneau pondéré déterministe, répartissant les priorités dans le temps."""
    weights = {domain_id: max(1, int(domains[domain_id].get("priority", 1)))
               for domain_id in domain_ids}
    current = {domain_id: 0 for domain_id in domain_ids}
    total = sum(weights.values())
    order = {domain_id: index for index, domain_id in enumerate(domain_ids)}
    ring: list[str] = []
    for _ in range(total):
        for domain_id in domain_ids:
            current[domain_id] += weights[domain_id]
        chosen = max(domain_ids, key=lambda item: (current[item], -order[item]))
        ring.append(chosen)
        current[chosen] -= total
    return ring


def _format_xray_query(host: str, term: str, variant: int) -> str:
    escaped = term.replace('"', " ")
    escaped = _clean_text(escaped)
    templates = (
        f'site:{host}/posts "stage" "{escaped}"',
        f'site:{host}/posts "recherche stagiaire" "{escaped}"',
        f'site:{host}/posts "stage {escaped}"',
    )
    return templates[variant % len(templates)]


def build_xray_query_plan(
    *,
    profile_id: str,
    domains: Mapping[str, Mapping],
    domain_ids: Sequence[str],
    query_budget: int,
    state: XRayRotationState | Mapping | None = None,
) -> tuple[list[XRayQuery], XRayRotationState]:
    """Construit un plan global et le prochain état, sans aucune écriture.

    `fast_data_ia` est plafonné contractuellement à trois requêtes au total.
    Le FULL utilise un anneau pondéré lissé : tous les domaines apparaissent,
    tandis qu'un domaine de priorité 3 reçoit trois passages par cycle contre
    un passage pour une priorité 1.
    """
    if query_budget < 0:
        raise ValueError("query_budget ne peut pas être négatif")
    selected = list(dict.fromkeys(str(domain_id) for domain_id in domain_ids))
    if not selected:
        raise ValueError("Au moins un domaine X-ray doit être sélectionné")
    unknown = [domain_id for domain_id in selected if domain_id not in domains]
    if unknown:
        raise ValueError(f"Domaines X-ray inconnus: {unknown}")
    for domain_id in selected:
        queries = domains[domain_id].get("queries")
        if not isinstance(queries, list) or not queries:
            raise ValueError(f"Domaine X-ray sans requête: {domain_id}")

    current_state = (
        state if isinstance(state, XRayRotationState)
        else XRayRotationState.from_mapping(state)
    )
    budget = min(query_budget, 3) if profile_id == "fast_data_ia" else query_budget
    if budget == 0:
        return [], current_state

    ring = _smooth_weighted_ring(selected, domains)
    cursors = dict(current_state.query_cursors)
    plan: list[XRayQuery] = []
    seen_queries: set[str] = set()
    ring_cursor = current_state.ring_cursor
    host_cursor = current_state.host_cursor
    # Les doublons de catalogue ne doivent pas réduire silencieusement le
    # budget. La borne évite toute boucle sur un catalogue pathologique.
    attempts = 0
    max_attempts = max(20, budget * max(6, len(ring) * 3))
    while len(plan) < budget and attempts < max_attempts:
        domain_id = ring[ring_cursor % len(ring)]
        ring_cursor += 1
        raw_queries = domains[domain_id]["queries"]
        query_cursor = cursors.get(domain_id, 0)
        term = _strip_contract_words(raw_queries[query_cursor % len(raw_queries)])
        cursors[domain_id] = query_cursor + 1
        host = LINKEDIN_POST_HOSTS[host_cursor % len(LINKEDIN_POST_HOSTS)]
        variant = host_cursor
        host_cursor += 1
        query = _format_xray_query(host, term, variant)
        attempts += 1
        if query in seen_queries:
            continue
        seen_queries.add(query)
        plan.append(XRayQuery(domain_id=domain_id, query=query, host=host))

    next_state = XRayRotationState(
        ring_cursor=ring_cursor,
        host_cursor=host_cursor,
        # Ordre lexical stable pour que JSON -> objet -> JSON conserve une
        # égalité structurelle indépendamment de l'ordre du catalogue appelant.
        query_cursors=tuple(sorted(
            (domain_id, cursors.get(domain_id, 0)) for domain_id in selected
        )),
    )
    return plan, next_state


__all__ = [
    "DUCKDUCKGO_HTML_ENDPOINT",
    "DuckDuckGoHtmlProvider",
    "SearchResult",
    "XRayBlockedError",
    "XRayProviderError",
    "XRayQuery",
    "XRayRotationState",
    "XRaySearchProvider",
    "XRayTimeoutError",
    "build_xray_query_plan",
    "canonicalize_linkedin_post_url",
    "deduplicate_search_results",
    "extract_linkedin_author",
    "is_relevant_internship_post",
    "linkedin_post_identity",
    "parse_duckduckgo_html",
    "parse_engine_date",
]
