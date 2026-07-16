#!/usr/bin/env python3
"""
Radar Stage France — agrégateur multi-sources, multi-domaines et multi-régions.

Sources :
  1. LinkedIn (offres publiques, sans compte)   — via JobSpy
  2. Indeed (régions configurées)                — via JobSpy
  3. Google Jobs (agrège WTTJ, HelloWork…)       — via JobSpy
  4. APEC France (recherche publique de stages)  — sans compte
  5. Adzuna France (agrégateur officiel)         — si secrets fournis
  6. France Travail (API officielle française)   — si secrets fournis
  7. LinkedIn Posts (X-ray moteur public)         — sans accès à LinkedIn

Chaque source est optionnelle et tolérante aux pannes : si un site bloque
(403/429) ou si une clé manque, on continue avec les autres. Un circuit
breaker évite de marteler une source bloquée pendant le run courant ; elle
est retentée automatiquement au cron suivant.

Diagnostics : chaque source produit un SourceStatus (statut, nombre brut,
acceptés, rejetés avec raisons, durée, erreur HTTP) affiché dans les logs,
sauvegardé dans data/source_status.json et rendu sur le dashboard.

Sorties :
  - Email HTML si nouvelles offres (SMTP)
  - docs/index.html : LE dashboard (GitHub Pages) avec toutes les offres
  - data/offers.json : store complet persistant des offres actives/récentes
  - data/seen_offers.json : mémoire des offres déjà vues (delta detection)
  - data/source_status.json : diagnostic détaillé du dernier scan
"""

import argparse
import hashlib
import html
import json
import logging
import math
import os
import re
import smtplib
import sys
import time
import unicodedata
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from functools import lru_cache
from pathlib import Path
from threading import Lock
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

import radar_store
from radar_io import atomic_write_json, atomic_write_text
from radar_profiles import (
    DEFAULT_PROFILE_ID,
    PROFILES_FILE,
    ScanContext,
    ScanProfile,
    load_profiles,
    resolve_profile,
)
from radar_xray import (
    DuckDuckGoHtmlProvider,
    SearchResult,
    XRayBlockedError,
    XRayProviderError,
    XRayRotationState,
    XRayTimeoutError,
    build_xray_query_plan,
    canonicalize_linkedin_post_url,
    is_relevant_internship_post,
    linkedin_post_identity,
)

# ============================================================
# CONFIG
# ============================================================

CONFIG_FILE = Path("data/config.json")
DOMAINS_FILE = Path("data/domains.json")
REGIONS_FILE = Path("data/regions.json")
SEEN_FILE = Path("data/seen_offers.json")
STATUS_FILE = Path("data/source_status.json")
PROFILE_STATUS_FILES = {
    "fast_data_ia": Path("data/source_status_fast_data_ia.json"),
    "full_general": Path("data/source_status_full_general.json"),
}
METRICS_FILE = Path("data/scan_metrics.json")
XRAY_STATE_FILE = Path("data/xray_state.json")
OFFERS_FILE = Path("data/offers.json")
DOCS_INDEX = Path("docs/index.html")


def load_json(path: Path) -> dict:
    """Charge un fichier de configuration JSON et valide son type racine."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} doit contenir un objet JSON")
    return data


def load_domains(path: Path = DOMAINS_FILE) -> dict:
    """Catalogue métier externalisé, sans valeur métier de secours en code."""
    domains = load_json(path)
    required = {"label", "group", "queries", "positive", "negative", "enabled"}
    for domain_id, domain in domains.items():
        missing = required - set(domain)
        if missing:
            raise ValueError(f"Domaine {domain_id}: champs manquants {sorted(missing)}")
        for field_name in ("queries", "positive", "negative"):
            values = domain[field_name]
            if not isinstance(values, list) or not values or not all(
                    isinstance(value, str) and value.strip() for value in values):
                raise ValueError(f"Domaine {domain_id}: {field_name} invalide")
            if len({value.casefold() for value in values}) != len(values):
                raise ValueError(f"Domaine {domain_id}: doublon dans {field_name}")
        priority = domain.get("priority", 1)
        if isinstance(priority, bool) or not isinstance(priority, int) or priority < 1:
            raise ValueError(f"Domaine {domain_id}: priority doit être un entier positif")
    return domains


def load_config(path: Path = CONFIG_FILE) -> dict:
    config = load_json(path)
    required = {"app_name", "tagline", "highlight_companies",
                "highlight_label", "days_window"}
    missing = required - set(config)
    if missing:
        raise ValueError(f"Configuration: champs manquants {sorted(missing)}")
    if int(config["days_window"]) <= 0:
        raise ValueError("days_window doit être strictement positif")
    expiration = config.get("expiration", {})
    if not isinstance(expiration, dict):
        raise ValueError("expiration doit contenir un objet JSON")
    for field_name, default in (("missing_hours", 72), ("successful_scans", 3)):
        value = expiration.get(field_name, default)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"expiration.{field_name} doit être un entier positif")
    seen_retention = config.get("seen_retention_days", 45)
    if (isinstance(seen_retention, bool) or not isinstance(seen_retention, int)
            or seen_retention <= 0):
        raise ValueError("seen_retention_days doit être un entier positif")
    return config


def load_regions(path: Path = REGIONS_FILE) -> dict:
    """Catalogue géographique et paramètres natifs de chaque connecteur."""
    regions = load_json(path)
    required = {
        "label", "linkedin_location", "google_location", "country_indeed",
        "apec_available", "france_travail_available", "enabled",
    }
    for region_id, region in regions.items():
        missing = required - set(region)
        if missing:
            raise ValueError(f"Région {region_id}: champs manquants {sorted(missing)}")
        for field_name in ("label", "linkedin_location", "google_location",
                           "country_indeed"):
            if not isinstance(region[field_name], str) or not region[field_name].strip():
                raise ValueError(f"Région {region_id}: {field_name} invalide")
        for field_name in ("apec_available", "france_travail_available", "enabled"):
            if not isinstance(region[field_name], bool):
                raise ValueError(f"Région {region_id}: {field_name} doit être booléen")
    if "france" not in regions:
        raise ValueError("Le catalogue des régions doit contenir 'france'")
    return regions


APP_CONFIG = load_config()
DOMAINS = load_domains()
REGIONS = load_regions()
DEFAULT_REGION_ID = "france"

SOURCE_NAMES = [
    "LinkedIn", "Indeed", "Google Jobs", "APEC", "Adzuna",
    "France Travail", "LinkedIn Posts",
]

PROFILES = load_profiles(PROFILES_FILE, DOMAINS, REGIONS, SOURCE_NAMES)

# Mots qui doivent apparaître dans le titre pour garder l'offre
# (filtre anti-bruit : les scrapers remontent parfois des CDI)
TITLE_MUST_MATCH = re.compile(r"\b(stage|stagiaire|intern|internship|pfa|pfe)\b", re.I)

# Fenêtre métier de collecte : exactement 24 heures glissantes en UTC. La
# configuration en jours reste utilisée par l'interface et la rétention ; elle
# n'intervient plus dans la sélection des résultats du scan courant.
MAX_OFFER_AGE_DAYS = int(APP_CONFIG["days_window"])
# Alias rétrocompatible pour les intégrations historiques. Le moteur utilise
# toujours ``ScanContext.profile.window_hours`` pendant un run.
OFFER_WINDOW_HOURS = PROFILES[DEFAULT_PROFILE_ID].window_hours

# Calibration multi-label. Le seuil 2 conserve la dominance du titre (+3)
# face à un seul signal négatif de description (-1) et permet aux intitulés
# composés explicitement catalogués de survivre à un voisin métier (-4).
TITLE_POSITIVE_WEIGHT = 3
DESCRIPTION_POSITIVE_WEIGHT = 1
TITLE_NEGATIVE_WEIGHT = -4
DESCRIPTION_NEGATIVE_WEIGHT = -1
DOMAIN_SCORE_THRESHOLD = 2

# Réseau / robustesse
REQUEST_TIMEOUT = PROFILES[DEFAULT_PROFILE_ID].request_timeout_s
MAX_CONSECUTIVE_BLOCKS = 2    # 403/429 consécutifs avant d'arrêter la source (run courant)
MAX_PARALLEL_SOURCES = PROFILES[DEFAULT_PROFILE_ID].max_parallel_sources
APEC_DETAIL_MAX_CALLS = PROFILES[DEFAULT_PROFILE_ID].detail_budgets.get("APEC", 25)
MAX_LINKEDIN_QUERIES = PROFILES[DEFAULT_PROFILE_ID].query_budgets.get("LinkedIn", 1000)
MAX_LINKEDIN_DESCRIPTION_QUERIES = PROFILES[DEFAULT_PROFILE_ID].detail_budgets.get(
    "LinkedIn", 3
)

# Plafonds purement techniques de JobSpy. Ils déclenchent sa pagination interne
# jusqu'à la fin réelle des résultats ; Google plafonne lui-même à 900 et
# LinkedIn à 1000. Ces valeurs ne sont jamais réduites par le nombre de
# domaines ou de régions.
JOBSPY_RESULTS_WANTED = {
    "linkedin": PROFILES[DEFAULT_PROFILE_ID].results_wanted.get("linkedin", 1000),
    "indeed": PROFILES[DEFAULT_PROFILE_ID].results_wanted.get("indeed", 1000),
    "google": PROFILES[DEFAULT_PROFILE_ID].results_wanted.get("google", 900),
}
JOBSPY_PAGE_SIZES = {"linkedin": 25, "indeed": 100, "google": 10}
# Les loggers JobSpy sont globaux au processus. Deux régions d'un même site ne
# doivent jamais partager leurs handlers temporaires, sinon un 403 d'une région
# peut contaminer le diagnostic d'une autre.
JOBSPY_LOGGER_LOCKS = {
    logger_name: Lock() for logger_name in ("LinkedIn", "Indeed", "Google")
}

# Le garde-fou combinatoire reste uniquement informatif : il ne réduit plus le
# volume. Les limites effectives sont celles des connecteurs et les plafonds de
# pagination élevés ci-dessus.
SCAN_COMBINATION_BUDGET = 6
SCAN_FIXED_ESTIMATE_S = 45
SCAN_SECONDS_PER_EFFECTIVE_COMBINATION = 35

# Pagination explicite des API non-JobSpy. Arrêt à la fin réelle, ou après deux
# pages successives sans nouveau résultat ; MAX_SOURCE_PAGES est la seule
# protection contre une boucle distante défectueuse.
MAX_SOURCE_PAGES = max(PROFILES[DEFAULT_PROFILE_ID].max_pages.values())
MAX_STALE_PAGES = PROFILES[DEFAULT_PROFILE_ID].max_stale_pages
APEC_PAGE_SIZE = 100
ADZUNA_PAGE_SIZE = 50
FRANCE_TRAVAIL_PAGE_SIZE = 150

# Une offre confirmée absente reste archivée brièvement avant purge. Cette
# marge protège l'historique pendant un blocage prolongé tout en gardant le
# dashboard limité à ``days_window``.
OFFER_RETENTION_GRACE_DAYS = 7
OFFER_RETENTION_DAYS = MAX_OFFER_AGE_DAYS + OFFER_RETENTION_GRACE_DAYS
EXPIRATION_MISSING_HOURS = int(APP_CONFIG.get("expiration", {}).get("missing_hours", 72))
EXPIRATION_SUCCESSFUL_SCANS = int(
    APP_CONFIG.get("expiration", {}).get("successful_scans", 3)
)
SEEN_RETENTION_DAYS = int(APP_CONFIG.get("seen_retention_days", 45))
RETIRED_SOURCES = {"Glassdoor"}


@dataclass(frozen=True)
class ScanBudget:
    domain_count: int
    region_count: int
    combination_count: int
    scale: float
    results_wanted: dict[str, int]
    estimated_duration_s: int

    @property
    def is_high(self) -> bool:
        return self.combination_count > SCAN_COMBINATION_BUDGET


def calculate_scan_budget(domain_count: int, region_count: int,
                          profile: ScanProfile | None = None) -> ScanBudget:
    """Calcule un budget déterministe, testable sans aucun appel réseau."""
    if domain_count <= 0 or region_count <= 0:
        raise ValueError("Le budget exige au moins un domaine et une région")
    profile = profile or PROFILES[DEFAULT_PROFILE_ID]
    combinations = domain_count * region_count
    scale = 1.0
    wanted = dict(profile.results_wanted)
    effective_combinations = combinations
    estimated = int(round(
        SCAN_FIXED_ESTIMATE_S
        + SCAN_SECONDS_PER_EFFECTIVE_COMBINATION * effective_combinations
    ))
    return ScanBudget(domain_count, region_count, combinations, scale, wanted, estimated)

# Raisons de rejet (diagnostic)
REASON_NO_TITLE = "titre manquant"
REASON_NOT_INTERNSHIP = "pas un stage"
REASON_TOO_OLD = f"plus de {OFFER_WINDOW_HOURS} heures"
REASON_NO_DOMAIN = "aucun domaine"
REASON_DUPLICATE = "doublon"


def too_old_reason(context: ScanContext | None = None,
                   window_hours: int | None = None) -> str:
    hours = (context.profile.window_hours if context else
             (window_hours if window_hours is not None else OFFER_WINDOW_HOURS))
    return f"plus de {hours} heures"


def default_context(run_at: datetime | None = None) -> ScanContext:
    run_at = run_at or datetime.now(timezone.utc)
    if run_at.tzinfo is None:
        run_at = run_at.replace(tzinfo=timezone.utc)
    return ScanContext(PROFILES[DEFAULT_PROFILE_ID], run_at.astimezone(timezone.utc))

# ============================================================
# STATUT PAR SOURCE
# ============================================================

@dataclass
class SourceStatus:
    """Diagnostic d'une source pour un run.

    status : success | empty | blocked | error | disabled_missing_credentials |
             not_applicable_region
    """
    source: str
    profile_id: str = DEFAULT_PROFILE_ID
    window_hours: int = OFFER_WINDOW_HOURS
    region_id: str = DEFAULT_REGION_ID
    region_label: str = "France"
    status: str = "empty"
    raw_count: int = 0
    accepted_count: int = 0
    rejected_count: int = 0
    reject_reasons: dict = field(default_factory=dict)
    unknown_date_count: int = 0
    too_old_count: int = 0
    duplicate_count: int = 0
    pages_fetched: int = 0
    query_count: int = 0
    network_request_count: int = 0
    detail_request_count: int = 0
    timeout_count: int = 0
    blocked_count: int = 0
    coverage_complete: bool = True
    attempted_queries: int = 0
    completed_queries: int = 0
    failed_queries: int = 0
    interrupted: bool = False
    query_reports: list[dict] = field(default_factory=list)
    duration_s: float = 0.0
    http_error: str = ""
    note: str = ""

    def add_reject(self, reason: str | None) -> None:
        reason = reason or "raison inconnue"
        self.rejected_count += 1
        self.reject_reasons[reason] = self.reject_reasons.get(reason, 0) + 1
        if reason.startswith("plus de "):
            self.too_old_count += 1
        elif reason == REASON_DUPLICATE:
            self.duplicate_count += 1

    def finalize(self) -> None:
        if self.status in ("blocked", "error", "disabled_missing_credentials",
                           "not_applicable_region", "not_queried_budget",
                           "retired"):
            return
        self.status = "success" if self.accepted_count else "empty"


@dataclass
class RunMetrics:
    """Compteurs de phases, thread-safe et séparés du contexte immuable."""
    normalization_s: float = 0.0
    scoring_s: float = 0.0
    deduplication_s: float = 0.0
    store_s: float = 0.0
    dashboard_s: float = 0.0
    collection_s: float = 0.0
    total_s: float = 0.0
    _lock: Lock = field(default_factory=Lock, repr=False)

    def add(self, field_name: str, seconds: float) -> None:
        with self._lock:
            setattr(self, field_name, getattr(self, field_name) + seconds)

    def payload(self) -> dict:
        return {
            name: round(getattr(self, name), 6)
            for name in (
                "collection_s", "normalization_s", "scoring_s",
                "deduplication_s", "store_s", "dashboard_s", "total_s",
            )
        }


BLOCK_CODE_RE = re.compile(r"\b(403|429)\b")


def block_code(exc: Exception) -> str | None:
    """Extrait un code HTTP de blocage (403/429) du message d'une exception."""
    match = BLOCK_CODE_RE.search(str(exc))
    return match.group(1) if match else None


def status_label(st: SourceStatus, final_count: int | None = None) -> str:
    """Libellé lisible pour les logs et le dashboard. Ne montre jamais un
    simple « 0 » pour une source qui n'a pas été appelée."""
    count = st.accepted_count if final_count is None else final_count
    if st.status == "disabled_missing_credentials":
        return "Non configuré — identifiants manquants"
    if st.status == "not_applicable_region":
        return f"Non applicable à {st.region_label}"
    if st.status == "not_queried_budget":
        return "Non interrogé sur ce run — rotation du budget"
    if st.status == "retired":
        return "Source retirée"
    if st.status == "blocked":
        return f"Bloqué temporairement — {st.http_error or 'HTTP 403/429'}"
    if st.status == "error":
        return f"Erreur — {(st.note or st.http_error or 'voir logs')[:90]}"
    if count == 0:
        if st.raw_count:
            return f"0 accepté / {st.raw_count} reçus"
        return "Aucune offre reçue"
    return f"{count} offre{'s' if count > 1 else ''}"


def chip_hint(st: SourceStatus, final_count: int) -> str:
    """Version courte du statut pour les puces de filtre du dashboard."""
    if final_count:
        return f"({final_count})"
    if st.status == "disabled_missing_credentials":
        return "non configuré"
    if st.status == "not_applicable_region":
        return "non applicable"
    if st.status == "not_queried_budget":
        return "rotation"
    if st.status == "retired":
        return "retirée"
    if st.status == "blocked":
        code = st.http_error.replace("HTTP ", "") if st.http_error else "403/429"
        return f"bloqué {code}"
    if st.status == "error":
        return "erreur"
    if st.raw_count:
        return f"0 accepté / {st.raw_count} reçus"
    return "(0)"


def register_offer(status: SourceStatus, offers: dict,
                   offer: dict | None, reason: str | None,
                   metrics: RunMetrics | None = None) -> None:
    """Comptabilise une offre évaluée : acceptée, rejetée ou doublon."""
    started = time.perf_counter()
    if offer is None:
        status.add_reject(reason)
    else:
        duplicate_id = find_duplicate_offer_id(offers, offer)
        if duplicate_id:
            merge_offer_metadata(offers[duplicate_id], offer)
            status.add_reject(REASON_DUPLICATE)
        else:
            offers[offer["id"]] = offer
            status.accepted_count += 1
            if not offer.get("date_known", bool(offer.get("date"))):
                status.unknown_date_count += 1
    if metrics:
        metrics.add("deduplication_s", time.perf_counter() - started)


def log_collection_summary(status: SourceStatus) -> None:
    """Bilan homogène demandé pour chaque source et chaque région."""
    print(
        f"[COLLECTE] {status.source} · {status.region_label} : "
        f"récupérées={status.raw_count} "
        f"conservées_{status.window_hours}h={status.accepted_count} "
        f"trop_anciennes={status.too_old_count} "
        f"doublons={status.duplicate_count} "
        f"dates_inconnues={status.unknown_date_count} "
        f"pages={status.pages_fetched}"
    )


# ============================================================
# HELPERS
# ============================================================

def enabled_domain_ids(domains: dict | None = None) -> list[str]:
    """Domaines actifs, avec surcharge explicite pour les benchmarks.

    ``RADAR_DOMAINS=a,b`` n'altère jamais le catalogue versionné et permet de
    comparer plusieurs configurations sur un run isolé.
    """
    domains = domains or DOMAINS
    override = os.environ.get("RADAR_DOMAINS", "").strip()
    if override:
        requested = [value.strip() for value in override.split(",") if value.strip()]
        unknown = [domain_id for domain_id in requested if domain_id not in domains]
        if unknown:
            raise ValueError(f"Domaines inconnus dans RADAR_DOMAINS: {unknown}")
        active = list(dict.fromkeys(requested))
    else:
        active = [domain_id for domain_id, domain in domains.items() if domain["enabled"]]
    if not active:
        raise ValueError("Au moins un domaine doit être actif")
    return active


def enabled_region_ids(regions: dict | None = None) -> list[str]:
    """Régions actives, surchargeables par ``RADAR_REGIONS`` en benchmark."""
    regions = regions or REGIONS
    override = os.environ.get("RADAR_REGIONS", "").strip()
    if override:
        requested = [value.strip() for value in override.split(",") if value.strip()]
        unknown = [region_id for region_id in requested if region_id not in regions]
        if unknown:
            raise ValueError(f"Régions inconnues dans RADAR_REGIONS: {unknown}")
        active = list(dict.fromkeys(requested))
    else:
        active = [region_id for region_id, region in regions.items() if region["enabled"]]
    if not active:
        raise ValueError("Au moins une région doit être active")
    return active


def active_regions(regions: dict | None = None,
                   active_ids: list[str] | None = None) -> dict:
    regions = regions or REGIONS
    active_ids = active_ids or enabled_region_ids(regions)
    return {region_id: regions[region_id] for region_id in active_ids}


def source_status(source: str, region_id: str = DEFAULT_REGION_ID,
                  regions: dict | None = None,
                  context: ScanContext | None = None, **kwargs) -> SourceStatus:
    regions = regions or REGIONS
    region = regions[region_id]
    return SourceStatus(source=source,
                        profile_id=(context.profile.id if context else DEFAULT_PROFILE_ID),
                        window_hours=(context.profile.window_hours
                                      if context else OFFER_WINDOW_HOURS),
                        region_id=region_id,
                        region_label=region["label"], **kwargs)


def active_domains(domains: dict | None = None,
                   active_ids: list[str] | None = None) -> dict:
    domains = domains or DOMAINS
    active_ids = active_ids or enabled_domain_ids(domains)
    return {domain_id: domains[domain_id] for domain_id in active_ids}


def active_queries(domains: dict | None = None,
                   active_ids: list[str] | None = None) -> list[str]:
    """Requêtes actives dédupliquées exactement, ordre du catalogue conservé."""
    selected = active_domains(domains, active_ids)
    return list(dict.fromkeys(
        query.strip()
        for domain in selected.values()
        for query in domain["queries"]
        if query.strip()
    ))


def linkedin_queries(domains: dict | None = None,
                     active_ids: list[str] | None = None,
                     limit: int | None = None) -> list[str]:
    """Allocation pondérée et bornée des requêtes LinkedIn.

    Si le budget couvre tous les domaines, chacun reçoit d'abord une requête.
    Les créneaux restants sont distribués par tours : un domaine reçoit autant
    de requêtes consécutives que sa priorité. Quand le budget est inférieur au
    nombre de domaines, les priorités les plus hautes passent d'abord ; l'ordre
    du catalogue départage les priorités égales.
    """
    selected = active_domains(domains, active_ids)
    limit = MAX_LINKEDIN_QUERIES if limit is None else limit
    buckets = {
        domain_id: list(dict.fromkeys(domain["queries"]))
        for domain_id, domain in selected.items()
    }
    priorities = {
        domain_id: domain.get("priority", 1)
        for domain_id, domain in selected.items()
    }
    ordered_ids = sorted(
        selected,
        key=lambda domain_id: (-priorities[domain_id], list(selected).index(domain_id)),
    )
    plan: list[str] = []
    seen: set[str] = set()
    next_index = {domain_id: 0 for domain_id in selected}

    def take(domain_id: str) -> bool:
        bucket = buckets[domain_id]
        while next_index[domain_id] < len(bucket):
            query = bucket[next_index[domain_id]].strip()
            next_index[domain_id] += 1
            if query and query not in seen:
                plan.append(query)
                seen.add(query)
                return True
        return False

    # Minimum garanti lorsque le nombre de créneaux le permet.
    if limit >= len(selected):
        for domain_id in selected:
            take(domain_id)
            if len(plan) >= limit:
                return plan

    while len(plan) < limit:
        added = False
        for domain_id in ordered_ids:
            for _ in range(priorities[domain_id]):
                if len(plan) >= limit:
                    break
                added = take(domain_id) or added
        if not added:
            break
    return plan


def source_queries(site: str, domains: dict | None = None,
                   active_ids: list[str] | None = None,
                   context: ScanContext | None = None,
                   source_label: str | None = None) -> list[str]:
    label = source_label or {
        "linkedin": "LinkedIn", "indeed": "Indeed", "google": "Google Jobs",
    }.get(site, site)
    limit = (context.profile.query_budgets.get(label, 1000)
             if context else (MAX_LINKEDIN_QUERIES if site == "linkedin" else 1000))
    queries = (linkedin_queries(domains, active_ids, limit) if site == "linkedin"
               else active_queries(domains, active_ids))
    return queries[:limit]


def source_keywords(query: str) -> str:
    """Retire seulement le vocabulaire de contrat pour les API déjà en stage."""
    value = re.sub(r"\b(stage|stagiaire|internship|intern|pfa|pfe)\b", " ", query,
                   flags=re.I)
    return re.sub(r"\s+", " ", value).strip(" -\"'") or query


def normalize_for_match(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", clean_text(value).casefold())
    return "".join(char for char in decomposed if not unicodedata.combining(char))


@lru_cache(maxsize=1024)
def keyword_pattern(keyword: str) -> re.Pattern:
    normalized = normalize_for_match(keyword)
    escaped = re.escape(normalized).replace(r"\ ", r"\s+")
    return re.compile(rf"(?<!\w){escaped}(?!\w)")


def keyword_matches(text: str, keyword: str) -> bool:
    return bool(keyword_pattern(keyword).search(normalize_for_match(text)))


def score_domain(title: str, description: str, domain: dict) -> int:
    """Score déterministe ; chaque mot-clé compte au plus une fois par zone."""
    positive = domain["positive"]
    negative = domain["negative"]
    return (
        TITLE_POSITIVE_WEIGHT * sum(keyword_matches(title, word) for word in positive)
        + DESCRIPTION_POSITIVE_WEIGHT
        * sum(keyword_matches(description, word) for word in positive)
        + TITLE_NEGATIVE_WEIGHT * sum(keyword_matches(title, word) for word in negative)
        + DESCRIPTION_NEGATIVE_WEIGHT
        * sum(keyword_matches(description, word) for word in negative)
    )


def matched_positive_keywords(title: str, description: str,
                              domain: dict) -> list[str]:
    """Mots positifs réellement trouvés, titre prioritaire puis description."""
    ordered: list[str] = []
    seen: set[str] = set()
    for zone in (title, description):
        for keyword in domain["positive"]:
            key = normalize_for_match(keyword)
            if key not in seen and keyword_matches(zone, keyword):
                ordered.append(keyword)
                seen.add(key)
    return ordered


def score_offer_domains(title: str, description: str,
                        domains: dict | None = None,
                        active_ids: list[str] | None = None) -> tuple[list[str], dict[str, int]]:
    selected = active_domains(domains, active_ids)
    scores = {
        domain_id: score_domain(title, description, domain)
        for domain_id, domain in selected.items()
    }
    matched = [domain_id for domain_id, score in scores.items()
               if score >= DOMAIN_SCORE_THRESHOLD]
    return matched, scores


def score_offer_domains_explained(
        title: str, description: str, domains: dict | None = None,
        active_ids: list[str] | None = None) -> tuple[list[str], dict[str, int], dict[str, list[str]]]:
    selected = active_domains(domains, active_ids)
    matched, scores = score_offer_domains(title, description, domains, active_ids)
    keywords = {
        domain_id: matched_positive_keywords(title, description, selected[domain_id])
        for domain_id in matched
    }
    return matched, scores, keywords


TRACKING_QUERY_KEYS = {
    "trk", "trackingid", "lipi", "midtoken", "ref", "refid", "campaign",
    "from", "source", "sourceid",
}


def canonicalize_url(url: str) -> str:
    """Normalise une URL sans retirer les paramètres fonctionnels (ex. ``jk``)."""
    value = clean_text(url)
    if not value.startswith(("http://", "https://")):
        return ""
    try:
        parts = urlsplit(value)
    except ValueError:
        return ""
    host = (parts.hostname or "").casefold()
    if not host:
        return ""
    if host in {"linkedin.com", "fr.linkedin.com"}:
        host = "www.linkedin.com"
    try:
        parsed_port = parts.port
    except ValueError:
        return ""
    port = f":{parsed_port}" if parsed_port and parsed_port not in {80, 443} else ""
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if path != "/":
        path = path.rstrip("/")
    query = []
    for key, value_part in parse_qsl(parts.query, keep_blank_values=True):
        lowered = key.casefold()
        if lowered.startswith("utm_") or lowered in TRACKING_QUERY_KEYS:
            continue
        query.append((key, value_part))
    return urlunsplit(("https", host + port, path, urlencode(query), ""))


def infer_external_id(source: str, url: str, explicit_id: str = "") -> str:
    explicit = clean_text(explicit_id)
    if explicit:
        return re.sub(r"^(?:li|in|go)-", "", explicit, flags=re.I)
    canonical = canonicalize_url(url)
    if source == "LinkedIn":
        match = re.search(r"/(?:jobs/view|view)/(\d+)", canonical)
        return match.group(1) if match else ""
    if source == "Indeed":
        return dict(parse_qsl(urlsplit(canonical).query)).get("jk", "")
    if source == "APEC":
        match = re.search(r"/detail-offre/([^/?#]+)", canonical)
        return match.group(1) if match else ""
    if source == "France Travail":
        match = re.search(r"/detail/([^/?#]+)", canonical)
        return match.group(1) if match else ""
    if source == "LinkedIn Posts":
        try:
            return linkedin_post_identity(canonical)
        except ValueError:
            return ""
    return ""


def exact_identity_keys(source: str, external_id: str, canonical_url: str,
                        title: str, company: str) -> list[str]:
    keys: list[str] = []
    if external_id:
        keys.append(f"native:{source.casefold()}:{external_id.casefold()}")
    if canonical_url:
        keys.append(f"url:{canonical_url}")
    # Le hash de secours n'est utilisé que lorsqu'aucune identité plus forte
    # n'existe. Deux annonces natives différentes peuvent légitimement porter
    # le même titre dans la même entreprise.
    if not external_id and not canonical_url:
        exact = "|".join((normalize_for_match(source), normalize_for_match(title),
                          normalize_for_match(company)))
        keys.append("exact:" + hashlib.sha256(exact.encode()).hexdigest()[:24])
    return keys


GENERIC_COMPANIES = {"", "entreprise non precisee", "post linkedin", "confidentiel"}
GENERIC_LOCATIONS = {"", "france", "belgique", "luxembourg", "maroc", "remote"}
GENERIC_TITLE_TOKENS = {
    "stage", "stagiaire", "intern", "internship", "pfa", "pfe",
    "assistant", "assistante", "junior", "h", "f", "hf", "fh",
}


def probable_offer_fingerprint(title: str, company: str, location: str,
                               contract_type: str = "internship") -> str:
    normalized_title = normalize_for_match(title)
    normalized_company = normalize_for_match(company)
    normalized_location = normalize_for_match(location)
    meaningful_title_tokens = {
        token for token in re.findall(r"[a-z0-9]+", normalized_title)
        if len(token) >= 2 and token not in GENERIC_TITLE_TOKENS
    }
    if (len(normalized_title) < 12 or normalized_company in GENERIC_COMPANIES
            or normalized_location in GENERIC_LOCATIONS
            or len(meaningful_title_tokens) < 2):
        return ""
    base = "|".join((normalized_title, normalized_company,
                     normalized_location, contract_type))
    return hashlib.sha256(base.encode()).hexdigest()[:24]


def merge_offer_metadata(existing: dict, incoming: dict) -> None:
    """Fusion multi-label et multi-région d'un doublon intra/inter-source."""
    existing["domains"] = list(dict.fromkeys(
        existing.get("domains", []) + incoming.get("domains", [])
    ))
    existing["regions"] = list(dict.fromkeys(
        existing.get("regions", []) + incoming.get("regions", [])
    ))
    merged_scores = dict(existing.get("domain_scores", {}))
    for domain_id, score in incoming.get("domain_scores", {}).items():
        merged_scores[domain_id] = max(score, merged_scores.get(domain_id, score))
    existing["domain_scores"] = merged_scores
    existing["highlighted"] = bool(existing.get("highlighted") or incoming.get("highlighted"))
    existing["sources"] = list(dict.fromkeys(
        existing.get("sources", [existing.get("source")])
        + incoming.get("sources", [incoming.get("source")])
    ))
    existing.setdefault("source_urls", {}).update(incoming.get("source_urls", {}))
    existing.setdefault("external_ids", {}).update(incoming.get("external_ids", {}))
    existing["identity_keys"] = list(dict.fromkeys(
        existing.get("identity_keys", []) + incoming.get("identity_keys", [])
    ))
    existing["legacy_ids"] = list(dict.fromkeys(
        existing.get("legacy_ids", []) + incoming.get("legacy_ids", [])
        + ([incoming["id"]] if incoming.get("id") != existing.get("id") else [])
    ))
    existing_keywords = dict(existing.get("matched_keywords", {}))
    for domain_id, words in incoming.get("matched_keywords", {}).items():
        existing_keywords[domain_id] = list(dict.fromkeys(
            existing_keywords.get(domain_id, []) + words
        ))
    existing["matched_keywords"] = existing_keywords
    existing["contact_direct"] = bool(
        existing.get("contact_direct") or incoming.get("contact_direct")
    )


def find_duplicate_offer_id(offers: dict[str, dict], incoming: dict) -> str | None:
    if incoming["id"] in offers:
        return incoming["id"]
    incoming_keys = set(incoming.get("identity_keys", []))
    fingerprint = incoming.get("probable_fingerprint", "")
    for offer_id_value, existing in offers.items():
        if incoming_keys.intersection(existing.get("identity_keys", [])):
            return offer_id_value
        if (fingerprint and fingerprint == existing.get("probable_fingerprint")
                and incoming.get("source") != existing.get("source")):
            return offer_id_value
    return None


def align_offers_with_store(found: dict[str, dict],
                            store: dict[str, dict]) -> dict[str, dict]:
    """Réutilise les IDs historiques lorsque l'identité est déjà persistée."""
    aligned: dict[str, dict] = {}
    fingerprints: dict[str, list[tuple[str, dict]]] = defaultdict(list)
    for existing_id, existing in store.items():
        fingerprint = clean_text(existing.get("probable_fingerprint"))
        if fingerprint:
            fingerprints[fingerprint].append((existing_id, existing))

    for incoming in found.values():
        existing_id = radar_store.find_existing_offer_id(store, incoming)
        if existing_id is None:
            fingerprint = clean_text(incoming.get("probable_fingerprint"))
            incoming_sources = set(incoming.get("sources") or [incoming.get("source")])
            probable = [
                candidate_id for candidate_id, candidate in fingerprints.get(fingerprint, [])
                if incoming_sources.isdisjoint(
                    set(candidate.get("sources") or [candidate.get("source")])
                )
            ]
            if len(probable) == 1:
                existing_id = probable[0]
        target_id = existing_id or incoming["id"]
        candidate = dict(incoming)
        if target_id != incoming["id"]:
            candidate["legacy_ids"] = list(dict.fromkeys(
                candidate.get("legacy_ids", []) + [incoming["id"]]
            ))
            candidate["id"] = target_id
        duplicate_id = find_duplicate_offer_id(aligned, candidate)
        if duplicate_id:
            merge_offer_metadata(aligned[duplicate_id], candidate)
        else:
            aligned[target_id] = candidate
    return aligned


def merge_offer_domains(existing: dict, incoming: dict) -> None:
    """Alias rétrocompatible pour les intégrations qui appellent ce helper."""
    merge_offer_metadata(existing, incoming)

def offer_id(url: str, title: str, company: str) -> str:
    base = (url or "") + "|" + (title or "").lower() + "|" + (company or "").lower()
    return hashlib.sha1(base.encode()).hexdigest()[:16]


def is_highlighted_company(company: str, config: dict | None = None) -> bool:
    config = config or APP_CONFIG
    c = (company or "").lower()
    return any(keyword.lower() in c for keyword in config["highlight_companies"])


def clean_text(value, default="") -> str:
    if value is None:
        return default
    text = str(value).strip()
    if not text or text.lower() in {"nan", "nat", "none"}:
        return default
    return text


def safe_url(url) -> str:
    """URL sûre pour un attribut href : http(s) uniquement, échappée."""
    url = clean_text(url)
    if url.startswith(("http://", "https://")):
        return html.escape(url, quote=True)
    return ""


def parse_offer_date(value, now: datetime | None = None) -> datetime | None:
    """Parse une date absolue ou un libellé relatif courant.

    Les libellés très frais sont volontairement reconnus avant le parseur ISO
    (LinkedIn/Indeed peuvent exposer « à l'instant », « aujourd'hui » ou
    « 2 hours ago »). Une valeur réellement inconnue retourne ``None`` ; elle
    sera conservée par :func:`make_offer`, jamais assimilée à une vieille date.
    """
    raw = "" if value is None else str(value).strip()
    if not raw or raw.lower() in {"nan", "nat", "none"}:
        return None
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)

    relative = raw.lower().replace("’", "'")
    relative = re.sub(r"\s+", " ", relative)
    if relative in {"à l'instant", "a l'instant", "aujourd'hui", "today",
                    "just posted", "just now", "nouveau"}:
        return now
    if relative in {"hier", "yesterday"}:
        return now - timedelta(days=1)
    if "quelques heures" in relative or "a few hours" in relative:
        return now - timedelta(hours=2)

    relative_patterns = [
        (r"(?:il y a\s*)?(\d+)\s*min(?:ute)?s?", "minutes"),
        (r"(?:il y a\s*)?(\d+)\s*h(?:eure)?s?", "hours"),
        (r"(?:il y a\s*)?(\d+)\s*jours?", "days"),
        (r"(\d+)\s*minutes?\s+ago", "minutes"),
        (r"(\d+)\s*hours?\s+ago", "hours"),
        (r"(\d+)\s*days?\s+ago", "days"),
    ]
    for pattern, unit in relative_patterns:
        match = re.search(pattern, relative)
        if match:
            return now - timedelta(**{unit: int(match.group(1))})
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            return None
        try:
            parsed = datetime.strptime(raw, "%Y-%m-%d")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _log_old_date_rejection(source: str, title: str, raw_date, parsed: datetime,
                            reason: str | None = None) -> None:
    """Trace vérifiable demandée pour chaque rejet de date ancienne."""
    raw = re.sub(r"[\r\n\t]+", " ", str(raw_date))[:100]
    safe_title = re.sub(r"[\r\n\t]+", " ", title)[:100]
    print(
        f"[REJET_DATE] source={source} titre={safe_title!r} "
        f"date_brute={raw!r} date_parsee={parsed.isoformat()} "
        f"raison={reason or REASON_TOO_OLD}",
        file=sys.stderr,
    )


def make_offer(title, company, location, url, source, date="",
               confirmed_internship=False, description="", domains: dict | None = None,
               active_ids: list[str] | None = None,
               region_id: str = DEFAULT_REGION_ID,
               regions: dict | None = None,
               now: datetime | None = None,
               freshness_verified: bool = False,
               context: ScanContext | None = None,
               window_hours: int | None = None,
               external_id: str = "",
               contract_type: str = "internship",
               contact_direct: bool = False,
               metrics: RunMetrics | None = None) -> tuple[dict | None, str | None]:
    """Normalise une offre. Retourne (offre, None) ou (None, raison_du_rejet)."""
    normalization_started = time.perf_counter()
    regions = regions or REGIONS
    if region_id not in regions:
        raise ValueError(f"Région inconnue pour l'offre: {region_id}")
    title = clean_text(title)
    if not title:
        return None, REASON_NO_TITLE
    if not confirmed_internship and not TITLE_MUST_MATCH.search(title):
        return None, REASON_NOT_INTERNSHIP
    description = clean_text(description)
    now = now or (context.run_at if context else datetime.now(timezone.utc))
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    effective_window = (context.profile.window_hours if context else
                        (window_hours if window_hours is not None else OFFER_WINDOW_HOURS))
    published_at = parse_offer_date(date, now)
    raw_date = clean_text(date)
    date_only = bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_date))
    cutoff = now - timedelta(hours=effective_window)
    # JobSpy peut réduire une date au jour alors que son filtre natif a validé
    # la fenêtre à l'heure. On accepte seulement le jour qui intersecte encore
    # la fenêtre ; une date de calendrier réellement ancienne reste rejetée.
    native_date_only_in_window = bool(
        freshness_verified and date_only and published_at
        and published_at.date() >= cutoff.date()
    )
    if (published_at and published_at < cutoff
            and not native_date_only_in_window):
        _log_old_date_rejection(
            source, title, date, published_at,
            too_old_reason(context, effective_window),
        )
        if metrics:
            metrics.add("normalization_s", time.perf_counter() - normalization_started)
        return None, too_old_reason(context, effective_window)
    if published_at is None:
        safe_title = re.sub(r"[\r\n\t]+", " ", title)[:100]
        raw = re.sub(r"[\r\n\t]+", " ", clean_text(date))[:100]
        print(
            f"[DATE_INCONNUE] source={source} titre={safe_title!r} "
            f"date_brute={raw!r} — fraîcheur {effective_window} h non vérifiable",
            file=sys.stderr,
        )
    if metrics:
        metrics.add("normalization_s", time.perf_counter() - normalization_started)
    scoring_started = time.perf_counter()
    matched_domains, domain_scores, matched_keywords = score_offer_domains_explained(
        title, description, domains=domains, active_ids=active_ids)
    if metrics:
        metrics.add("scoring_s", time.perf_counter() - scoring_started)
    if not matched_domains:
        return None, REASON_NO_DOMAIN
    company = clean_text(company, "Entreprise non précisée")
    location = clean_text(location, regions[region_id]["label"])
    url = clean_text(url)
    canonical_url = (canonicalize_linkedin_post_url(url) if source == "LinkedIn Posts"
                     else canonicalize_url(url)) or ""
    native_id = infer_external_id(source, url, external_id)
    legacy_id = offer_id(canonical_url or url, title, company)
    identity_keys = exact_identity_keys(
        source, native_id, canonical_url, title, company
    )
    return {
        "id": legacy_id,
        "title": title,
        "company": company,
        "location": location,
        "url": canonical_url or url,
        "source": source,
        "sources": [source],
        "source_urls": {source: canonical_url or url} if (canonical_url or url) else {},
        "external_ids": {source: native_id} if native_id else {},
        "identity_keys": identity_keys,
        "probable_fingerprint": probable_offer_fingerprint(
            title, company, location, contract_type
        ),
        "legacy_ids": [],
        "date": published_at.date().isoformat() if published_at else "",
        "date_known": published_at is not None,
        "date_raw": raw_date,
        "date_status": ("unknown" if published_at is None else
                        ("native_window" if freshness_verified and date_only else "exact")),
        # Une date inconnue conserve le comportement historique : classement
        # au moment du scan, sans prétendre qu'il s'agit de sa publication.
        "sort_date": (published_at or now).isoformat(),
        "freshness_verified_at": (now.isoformat() if freshness_verified else ""),
        "native_window_hours": effective_window if freshness_verified else None,
        "discovered_at": now.isoformat(),
        "domains": matched_domains,
        "regions": [region_id],
        "domain_scores": domain_scores,
        "matched_keywords": matched_keywords,
        "relevance_score": max((domain_scores[d] for d in matched_domains), default=0),
        "highlighted": is_highlighted_company(company),
        "contact_direct": contact_direct,
        "contract_type": contract_type,
    }, None


def sort_offers(offers: list[dict]) -> list[dict]:
    """Plus récentes d'abord ; tri secondaire stable sur le titre."""
    return sorted(sorted(offers, key=lambda o: o.get("title", "")),
                  key=lambda o: o.get("sort_date") or o.get("date", ""), reverse=True)


def publication_age(date_value: str) -> str:
    if not date_value:
        return "date inconnue"
    published = datetime.fromisoformat(date_value).date()
    days = max(0, (datetime.now(timezone.utc).date() - published).days)
    if days == 0:
        return "aujourd'hui"
    if days == 1:
        return "hier"
    return f"il y a {days} jours"


def human_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds} s"
    return f"{seconds // 60} min {seconds % 60:02d} s"


# ============================================================
# SOURCES 1-3 : JobSpy (LinkedIn / Indeed / Google)
# ============================================================

def fetch_jobspy_site(site: str, label: str, scrape_fn=None,
                      region_id: str = DEFAULT_REGION_ID,
                      region_config: dict | None = None,
                      regions: dict | None = None,
                      domains: dict | None = None,
                      active_ids: list[str] | None = None,
                      results_wanted: int | None = None,
                      context: ScanContext | None = None,
                      metrics: RunMetrics | None = None) -> tuple[list[dict], SourceStatus]:
    """Interroge un seul site JobSpy pour toutes les requêtes.

    Chaque site est isolé : une panne d'un connecteur n'influence jamais les autres.
    Un circuit breaker arrête la source après MAX_CONSECUTIVE_BLOCKS
    blocages 403/429 consécutifs (elle sera retentée au prochain cron).
    """
    regions = regions or REGIONS
    domains = domains or DOMAINS
    context = context or default_context()
    region_config = region_config or regions[region_id]
    status = source_status(label, region_id, regions, context=context)
    started = time.monotonic()
    if scrape_fn is None:
        try:
            from jobspy import scrape_jobs as scrape_fn
        except ImportError:
            status.status = "error"
            status.note = "python-jobspy non installé"
            status.coverage_complete = False
            status.duration_s = round(time.monotonic() - started, 1)
            log_collection_summary(status)
            return [], status

    offers: dict[str, dict] = {}
    consecutive_blocks = attempted = failed = 0
    site_queries = source_queries(
        site, domains, active_ids, context=context, source_label=label
    )
    status.query_count = len(site_queries)
    full_query_plan = (
        linkedin_queries(domains, active_ids, limit=10_000)
        if site == "linkedin" else active_queries(domains, active_ids)
    )
    if len(site_queries) < len(full_query_plan):
        status.coverage_complete = False
        domain_coverage = (
            "tous les domaines restent représentés"
            if len(site_queries) >= len(active_domains(domains, active_ids))
            else "périmètre partiel"
        )
        status.note = (
            f"budget de requêtes : {len(site_queries)}/{len(full_query_plan)} ; "
            f"{domain_coverage}"
        )
    logger_name = {"linkedin": "LinkedIn", "indeed": "Indeed",
                   "google": "Google"}[site]

    class CaptureHandler(logging.Handler):
        def __init__(self):
            super().__init__()
            self.records: list[tuple[int, str]] = []

        def emit(self, record):
            self.records.append((record.levelno, record.getMessage()))

    def pages_in_records(records: list[tuple[int, str]], raw_count: int) -> int:
        logged = sum("search page:" in message for _, message in records)
        fallback = max(1, math.ceil(raw_count / JOBSPY_PAGE_SIZES[site]))
        # Google journalise seulement les pages suivant sa page initiale.
        return max(fallback, logged + (1 if site == "google" else 0))

    for query in site_queries:
        if consecutive_blocks >= MAX_CONSECUTIVE_BLOCKS:
            status.note = (f"circuit breaker : requêtes restantes ignorées après "
                           f"{consecutive_blocks} blocages consécutifs")
            status.interrupted = True
            status.coverage_complete = False
            break
        attempted += 1
        status.attempted_queries += 1
        # hours_old borne la fraîcheur au plus près de la source. Le filtre
        # titre local reste l'autorité sur le type de contrat, car les sites
        # étiquettent parfois mal leurs offres.
        if site == "linkedin":
            native_location = region_config["linkedin_location"]
        elif site == "google":
            native_location = region_config["google_location"]
        else:
            native_location = region_config["country_indeed"]
        configured_results = (
            results_wanted if results_wanted is not None
            else context.profile.results_wanted.get(site, 15)
        )
        wanted = min(
            configured_results,
            context.profile.max_pages.get(label, configured_results)
            * JOBSPY_PAGE_SIZES[site],
        )
        kwargs = dict(
            site_name=[site],
            search_term=query,
            location=native_location,
            results_wanted=wanted,
            country_indeed=region_config["country_indeed"],
            hours_old=context.profile.window_hours,
            verbose=int(os.environ.get("JOBSPY_VERBOSE", "1")),
        )
        if site == "linkedin":
            # Les trois requêtes les plus prioritaires reçoivent la description
            # complète. Ce compromis réduit le biais « titre seul » sans payer
            # le coût réseau de l'enrichissement sur les six requêtes.
            kwargs["linkedin_fetch_description"] = (
                site_queries.index(query)
                < context.profile.detail_budgets.get("LinkedIn", 0)
            )
        if site == "google":
            # google_search_term remplace toute la requête côté JobSpy : on
            # évite la redondance « stage … stage » et on garde une syntaxe
            # Google propre (la requête contient déjà « stage »).
            kwargs["google_search_term"] = (
                f"{query} {region_config['google_location']} since:yesterday"
            )
        capture = CaptureHandler()
        jobspy_logger = logging.getLogger(f"JobSpy:{logger_name}")
        try:
            with JOBSPY_LOGGER_LOCKS[logger_name]:
                jobspy_logger.addHandler(capture)
                try:
                    df = scrape_fn(**kwargs)
                finally:
                    if capture in jobspy_logger.handlers:
                        jobspy_logger.removeHandler(capture)
        except Exception as exc:
            page_count = pages_in_records(capture.records, 0)
            status.pages_fetched += page_count
            status.network_request_count += page_count
            failed += 1
            status.failed_queries += 1
            status.coverage_complete = False
            code = block_code(exc)
            if code:
                consecutive_blocks += 1
                status.blocked_count += 1
                status.http_error = f"HTTP {code}"
                print(f"[WARN] {label} '{query}' bloqué ({status.http_error})", file=sys.stderr)
            else:
                status.http_error = status.http_error or str(exc)[:200]
                if isinstance(exc, (requests.Timeout, TimeoutError)) or "timeout" in str(exc).lower():
                    status.timeout_count += 1
                print(f"[WARN] {label} '{query}' a échoué : {exc}", file=sys.stderr)
            continue
        raw = len(df)
        page_count = pages_in_records(capture.records, raw)
        status.pages_fetched += page_count
        status.network_request_count += page_count

        # JobSpy journalise parfois un 403/429 puis retourne un DataFrame vide
        # sans lever d'exception. On transforme ce signal en
        # vrai statut blocked pour le dashboard et le circuit breaker.
        captured_code = next(
            (block_code(Exception(message)) for _, message in capture.records
             if block_code(Exception(message))),
            None,
        )
        if captured_code:
            failed += 1
            status.failed_queries += 1
            status.coverage_complete = False
            consecutive_blocks += 1
            status.blocked_count += 1
            status.http_error = f"HTTP {captured_code}"
            print(f"[WARN] {label} '{query}' bloqué ({status.http_error})", file=sys.stderr)
            if len(df) == 0:
                continue
        else:
            consecutive_blocks = 0
        captured_error = next(
            (message for level, message in capture.records if level >= logging.ERROR),
            "",
        )
        if captured_error and len(df) == 0:
            # Certains connecteurs avalent l'exception,
            # loggent une erreur puis renvoient un DataFrame vide. Sans ce
            # contrôle le dashboard affichait à tort « 0 offre ».
            failed += 1
            status.failed_queries += 1
            status.coverage_complete = False
            status.http_error = status.http_error or captured_error[:200]
            print(f"[WARN] {label} '{query}' a échoué : {captured_error}", file=sys.stderr)
            continue
        status.completed_queries += 1
        status.raw_count += raw
        if raw >= wanted:
            # Le connecteur a atteint le plafond technique demandé : on ne
            # peut pas certifier une absence pour la réconciliation du store.
            status.coverage_complete = False
        print(f"[INFO] {label} '{query}' : {raw} résultats bruts")
        for _, row in df.iterrows():
            row_description = clean_text(row.get("description"))
            offer, reason = make_offer(
                row.get("title"), row.get("company"),
                ", ".join(filter(None, [str(row.get("city") or ""), str(row.get("state") or "")]))
                or region_config["label"],
                row.get("job_url"), label, row.get("date_posted"),
                description=row_description,
                domains=domains, active_ids=active_ids,
                region_id=region_id, regions=regions,
                freshness_verified=True,
                context=context,
                external_id=clean_text(row.get("id")),
                metrics=metrics,
            )
            if (site == "linkedin" and offer is None
                    and reason == REASON_NO_DOMAIN and not row_description):
                status.coverage_complete = False
                coverage_note = "descriptions LinkedIn manquantes pour certains résultats"
                if coverage_note not in status.note:
                    status.note = (status.note + " · " if status.note else "") + \
                        coverage_note
            register_offer(status, offers, offer, reason, metrics)

    status.duration_s = round(time.monotonic() - started, 1)
    if consecutive_blocks >= MAX_CONSECUTIVE_BLOCKS or (
            attempted and failed == attempted and status.http_error.startswith("HTTP")):
        status.status = "blocked"
    elif attempted and failed == attempted:
        status.status = "error"
    else:
        status.finalize()
    status.coverage_complete = (
        status.coverage_complete and status.completed_queries == len(site_queries)
    )
    log_collection_summary(status)
    return list(offers.values()), status


# ============================================================
# SOURCE 4 : APEC (recherche publique de stages, sans compte)
# ============================================================

APEC_HEADERS = {"Accept": "application/json", "User-Agent": "stage-radar/1.0"}
APEC_ADVERTISER_TYPES = [143684, 143685, 143686, 143687, 143706]
APEC_BAC5_LEVEL = 20019


def apec_search_payload(query: str, start_index: int = 0,
                        page_size: int = APEC_PAGE_SIZE) -> dict:
    return {
        "motsCles": query,
        "lieux": [], "fonctions": [], "statutPoste": [],
        "typesContrat": [597171],  # identifiant APEC officiel : STAGE
        # Malgré son nom, typesConvention décrit le type d'ANNONCEUR :
        # 143684 entreprise, 143685 cabinet, 143686 agence, 143687 SSII/ESN,
        # 143706 partenaire (constantes vérifiées dans le bundle APEC).
        # Le partenaire reste inclus : le retirer excluait notamment une offre
        # Data Analyst publiée le jour même. Le filtre métier local élimine le
        # bruit de ces flux sans sacrifier la fraîcheur.
        "typesConvention": APEC_ADVERTISER_TYPES,
        # Référentiel public OFFRE_NIVEAU_ETUDE : 20019=Bac+5,
        # 20020=>Bac+5. Pour un PFE/école d'ingénieur, Bac+5 est le bon niveau.
        "niveauEtude": APEC_BAC5_LEVEL,
        "niveauxExperience": [],
        "idsEtablissement": [], "secteursActivite": [],
        "typesTeletravail": [], "idNomZonesDeplacement": [],
        "positionNumbersExcluded": [], "typeClient": "CADRE",
        "sorts": [{"type": "DATE", "direction": "DESCENDING"}],
        "pagination": {"range": page_size, "startIndex": start_index},
        "activeFiltre": True, "pointGeolocDeReference": {},
    }


def fetch_apec_detail_description(number: str,
                                  timeout: float | None = None,
                                  raise_on_error: bool = False) -> str:
    """Texte complet d'une offre APEC (le snippet de recherche est
    tronqué vers 280 caractères, souvent avant les missions)."""
    try:
        resp = requests.get(
            "https://www.apec.fr/cms/webservices/offre/public",
            params={"numeroOffre": number},
            headers=APEC_HEADERS, timeout=timeout or REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json() or {}
        blocks = [data.get("texteHtml"), data.get("texteHtmlProfil"),
                  data.get("texteHtmlEntreprise")]
        text = " ".join(str(b) for b in blocks if b)
        return re.sub(r"<[^>]+>", " ", text)
    except Exception as exc:
        print(f"[WARN] APEC détail {number} a échoué : {exc}", file=sys.stderr)
        if raise_on_error:
            raise
        return ""


def fetch_apec_all(region_id: str = DEFAULT_REGION_ID,
                   region_config: dict | None = None,
                   regions: dict | None = None,
                   domains: dict | None = None,
                   active_ids: list[str] | None = None,
                   context: ScanContext | None = None,
                   metrics: RunMetrics | None = None,
                   known_identity_index: dict[str, dict] | None = None) -> tuple[list[dict], SourceStatus]:
    """APEC : scoring titre + texte, avec détails complets bornés."""
    regions = regions or REGIONS
    domains = domains or DOMAINS
    context = context or default_context()
    known_identity_index = known_identity_index or {}
    region_config = region_config or regions[region_id]
    status = source_status("APEC", region_id, regions, context=context)
    started = time.monotonic()
    if not region_config["apec_available"]:
        status.status = "not_applicable_region"
        status.coverage_complete = False
        status.note = "APEC est une source nationale française"
        status.duration_s = round(time.monotonic() - started, 1)
        print(f"[INFO] APEC · {status.region_label} : non applicable à cette région")
        log_collection_summary(status)
        return [], status

    all_queries = list(dict.fromkeys(
        source_keywords(query) for query in active_queries(domains, active_ids)
    ))
    queries = all_queries[:context.profile.query_budgets.get("APEC", len(all_queries))]
    status.query_count = len(queries)
    if len(queries) < len(all_queries):
        status.coverage_complete = False
        status.note = f"budget de requêtes : {len(queries)}/{len(all_queries)}"
    raw_by_number: dict[str, dict] = {}
    attempted = failed = consecutive_blocks = 0
    for query in queries:
        if consecutive_blocks >= MAX_CONSECUTIVE_BLOCKS:
            status.note = (status.note + " · " if status.note else "") + \
                "circuit breaker : requêtes restantes ignorées"
            status.coverage_complete = False
            status.interrupted = True
            break
        attempted += 1
        status.attempted_queries += 1
        query_succeeded = False
        query_complete = False
        stale_pages = 0
        max_pages = context.profile.max_pages.get("APEC", MAX_SOURCE_PAGES)
        for page in range(max_pages):
            start_index = page * APEC_PAGE_SIZE
            try:
                status.network_request_count += 1
                resp = requests.post(
                    "https://www.apec.fr/cms/webservices/rechercheOffre",
                    json=apec_search_payload(query, start_index),
                    headers=APEC_HEADERS, timeout=context.profile.request_timeout_s,
                )
                resp.raise_for_status()
                results = resp.json().get("resultats", [])
            except Exception as exc:
                code = block_code(exc)
                status.http_error = (f"HTTP {code}" if code
                                     else (status.http_error or str(exc)[:200]))
                status.failed_queries += 1
                status.coverage_complete = False
                if code:
                    consecutive_blocks += 1
                    status.blocked_count += 1
                else:
                    consecutive_blocks = 0
                if isinstance(exc, (requests.Timeout, TimeoutError)) or "timeout" in str(exc).lower():
                    status.timeout_count += 1
                print(f"[WARN] APEC '{query}' page {page + 1} a échoué : {exc}",
                      file=sys.stderr)
                break
            query_succeeded = True
            consecutive_blocks = 0
            status.pages_fetched += 1
            status.raw_count += len(results)
            print(f"[INFO] APEC '{query}' page {page + 1} : "
                  f"{len(results)} résultats bruts")
            if not results:
                query_complete = True
                break
            before = len(raw_by_number)
            for r in results:
                number = str(r.get("numeroOffre") or "")
                if number:
                    if number in raw_by_number:
                        status.add_reject(REASON_DUPLICATE)
                    else:
                        raw_by_number[number] = r
            stale_pages = stale_pages + 1 if len(raw_by_number) == before else 0
            if (stale_pages >= context.profile.max_stale_pages
                    or len(results) < APEC_PAGE_SIZE):
                query_complete = True
                break
            page_dates = [
                parse_offer_date(r.get("datePublication"), context.run_at)
                for r in results
            ]
            if (page_dates and all(page_dates)
                    and min(page_dates) < context.cutoff):
                # Le payload impose DATE DESCENDING : les pages suivantes sont
                # nécessairement plus anciennes.
                query_complete = True
                break
        else:
            status.coverage_complete = False
            status.interrupted = True
            status.note = (status.note + " · " if status.note else "") + \
                f"plafond de {max_pages} pages atteint"
        if not query_succeeded:
            failed += 1
        elif query_complete:
            status.completed_queries += 1
        else:
            status.coverage_complete = False

    offers: dict[str, dict] = {}
    detail_budget = context.profile.detail_budgets.get("APEC", APEC_DETAIL_MAX_CALLS)
    detail_consecutive_blocks = 0
    detail_budget_exhausted = False
    for number, r in raw_by_number.items():
        url = f"https://www.apec.fr/candidat/recherche-stage.html/stage/detail-offre/{number}"
        args = (r.get("intitule"), r.get("nomCommercial"), r.get("lieuTexte"),
                url, "APEC", r.get("datePublication"))
        offer, reason = make_offer(*args, confirmed_internship=True,
                                   description=clean_text(r.get("texteOffre")),
                                   domains=domains, active_ids=active_ids,
                                   region_id=region_id, regions=regions,
                                   context=context, external_id=number,
                                   metrics=metrics)
        if offer is None and reason == REASON_NO_DOMAIN:
            # Titre insuffisant et snippet tronqué : lire le texte complet
            # avant de rejeter : un titre générique peut cacher des missions
            # spécifiques à l'un des domaines actifs.
            identity_key = f"native:apec:{number.casefold()}"
            known = known_identity_index.get(identity_key)
            if known:
                offer = dict(known)
                offer["regions"] = list(dict.fromkeys(
                    offer.get("regions", []) + [region_id]
                ))
                reason = None
            elif detail_consecutive_blocks >= MAX_CONSECUTIVE_BLOCKS:
                status.coverage_complete = False
                status.interrupted = True
            elif detail_budget <= 0:
                status.coverage_complete = False
                detail_budget_exhausted = True
            else:
                detail_budget -= 1
                status.detail_request_count += 1
                status.network_request_count += 1
                try:
                    full_text = fetch_apec_detail_description(
                        number, timeout=context.profile.request_timeout_s,
                        raise_on_error=True,
                    )
                except Exception as exc:
                    code = block_code(exc)
                    status.coverage_complete = False
                    if code:
                        detail_consecutive_blocks += 1
                        status.blocked_count += 1
                        status.http_error = f"HTTP {code}"
                        if detail_consecutive_blocks >= MAX_CONSECUTIVE_BLOCKS:
                            status.interrupted = True
                            breaker_note = "circuit breaker des détails APEC"
                            if breaker_note not in status.note:
                                status.note = (
                                    status.note + " · " if status.note else ""
                                ) + breaker_note
                    else:
                        detail_consecutive_blocks = 0
                        status.http_error = status.http_error or type(exc).__name__
                    if (isinstance(exc, (requests.Timeout, TimeoutError))
                            or "timeout" in str(exc).lower()):
                        status.timeout_count += 1
                    full_text = ""
                else:
                    detail_consecutive_blocks = 0
                if full_text:
                    offer, reason = make_offer(*args, confirmed_internship=True,
                                               description=full_text,
                                               domains=domains, active_ids=active_ids,
                                               region_id=region_id, regions=regions,
                                               context=context, external_id=number,
                                               metrics=metrics)
        register_offer(status, offers, offer, reason, metrics)
    if detail_budget_exhausted:
        status.note = (status.note + " · " if status.note else "") + \
            "budget de lectures de détail épuisé"

    status.duration_s = round(time.monotonic() - started, 1)
    if detail_consecutive_blocks >= MAX_CONSECUTIVE_BLOCKS and not offers:
        status.status = "blocked"
    elif consecutive_blocks >= MAX_CONSECUTIVE_BLOCKS or (
            attempted and failed == attempted and status.http_error.startswith("HTTP")):
        status.status = "blocked"
    elif attempted and failed == attempted:
        status.status = "error"
    else:
        status.finalize()
    status.coverage_complete = (
        status.coverage_complete and status.completed_queries == len(queries)
    )
    log_collection_summary(status)
    return list(offers.values()), status


# ============================================================
# SOURCE 5 : Adzuna (API officielle gratuite)
# ============================================================

def fetch_adzuna_all(region_id: str = DEFAULT_REGION_ID,
                     region_config: dict | None = None,
                     regions: dict | None = None,
                     domains: dict | None = None,
                     active_ids: list[str] | None = None,
                     context: ScanContext | None = None,
                     metrics: RunMetrics | None = None) -> tuple[list[dict], SourceStatus]:
    regions = regions or REGIONS
    domains = domains or DOMAINS
    context = context or default_context()
    region_config = region_config or regions[region_id]
    status = source_status("Adzuna", region_id, regions, context=context)
    started = time.monotonic()
    # L'adaptateur existant utilise l'index Adzuna /fr. Le catalogue demandé
    # ne fournit pas d'identifiant Adzuna pour les autres pays : ne jamais
    # attribuer par erreur une offre française à une autre région.
    if region_id != DEFAULT_REGION_ID:
        status.status = "not_applicable_region"
        status.coverage_complete = False
        status.note = "connecteur Adzuna actuel limité à l'index France"
        status.duration_s = round(time.monotonic() - started, 1)
        log_collection_summary(status)
        return [], status
    app_id = os.environ.get("ADZUNA_APP_ID")
    app_key = os.environ.get("ADZUNA_APP_KEY")
    if not app_id or not app_key:
        status.status = "disabled_missing_credentials"
        status.coverage_complete = False
        status.note = "secrets ADZUNA_APP_ID / ADZUNA_APP_KEY absents"
        status.duration_s = round(time.monotonic() - started, 1)
        log_collection_summary(status)
        return [], status

    offers: dict[str, dict] = {}
    consecutive_blocks = attempted = failed = 0
    all_queries = active_queries(domains, active_ids)
    query_budget = context.profile.query_budgets.get("Adzuna", len(all_queries))
    queries = all_queries[:query_budget]
    status.query_count = len(queries)
    if len(queries) < len(all_queries):
        status.coverage_complete = False
        status.note = f"budget de requêtes : {len(queries)}/{len(all_queries)}"
    for query in queries:
        if consecutive_blocks >= MAX_CONSECUTIVE_BLOCKS:
            status.note = "circuit breaker : requêtes restantes ignorées"
            status.coverage_complete = False
            status.interrupted = True
            break
        attempted += 1
        status.attempted_queries += 1
        query_succeeded = False
        query_complete = False
        query_seen: set[str] = set()
        stale_pages = 0
        max_pages = context.profile.max_pages.get("Adzuna", MAX_SOURCE_PAGES)
        for page in range(1, max_pages + 1):
            try:
                status.network_request_count += 1
                resp = requests.get(
                    f"https://api.adzuna.com/v1/api/jobs/fr/search/{page}",
                    params={
                        "app_id": app_id, "app_key": app_key,
                        "what": query, "where": region_config["label"],
                        "results_per_page": ADZUNA_PAGE_SIZE,
                        "sort_by": "date", "max_days_old": 1,
                        "content-type": "application/json",
                    },
                    timeout=context.profile.request_timeout_s,
                )
                resp.raise_for_status()
                payload = resp.json()
                results = payload.get("results", [])
            except Exception as exc:
                code = block_code(exc)
                status.failed_queries += 1
                status.coverage_complete = False
                if code:
                    consecutive_blocks += 1
                    status.blocked_count += 1
                    status.http_error = f"HTTP {code}"
                else:
                    status.http_error = status.http_error or type(exc).__name__
                    if (isinstance(exc, (requests.Timeout, TimeoutError))
                            or "timeout" in str(exc).lower()):
                        status.timeout_count += 1
                safe_error = f"HTTP {code}" if code else type(exc).__name__
                print(f"[WARN] Adzuna '{query}' page {page} a échoué : "
                      f"{safe_error}", file=sys.stderr)
                break
            query_succeeded = True
            consecutive_blocks = 0
            status.pages_fetched += 1
            status.raw_count += len(results)
            print(f"[INFO] Adzuna '{query}' page {page} : "
                  f"{len(results)} résultats bruts")
            if not results:
                query_complete = True
                break
            page_keys = {
                clean_text(r.get("id") or r.get("redirect_url")) for r in results
            } - {""}
            new_keys = page_keys - query_seen
            query_seen.update(page_keys)
            stale_pages = stale_pages + 1 if not new_keys else 0
            for r in results:
                offer, reason = make_offer(
                    r.get("title"), (r.get("company") or {}).get("display_name"),
                    (r.get("location") or {}).get("display_name"),
                    r.get("redirect_url"), "Adzuna", r.get("created") or "",
                    description=clean_text(r.get("description")),
                    domains=domains, active_ids=active_ids,
                    region_id=region_id, regions=regions,
                    context=context, external_id=clean_text(r.get("id")),
                    metrics=metrics,
                )
                register_offer(status, offers, offer, reason, metrics)
            total = int(payload.get("count") or 0)
            if (len(results) < ADZUNA_PAGE_SIZE
                    or (total and page * ADZUNA_PAGE_SIZE >= total)
                    or stale_pages >= context.profile.max_stale_pages):
                query_complete = True
                break
            page_dates = [
                parse_offer_date(r.get("created"), context.run_at) for r in results
            ]
            if (page_dates and all(page_dates)
                    and min(page_dates) < context.cutoff):
                # Adzuna est demandé en tri DATE décroissant.
                query_complete = True
                break
        else:
            status.coverage_complete = False
            status.interrupted = True
            status.note = (status.note + " · " if status.note else "") + \
                f"plafond de {max_pages} pages atteint"
        if not query_succeeded:
            failed += 1
        elif query_complete:
            status.completed_queries += 1
        else:
            status.coverage_complete = False

    status.duration_s = round(time.monotonic() - started, 1)
    if consecutive_blocks >= MAX_CONSECUTIVE_BLOCKS or (
            attempted and failed == attempted and status.http_error.startswith("HTTP")):
        status.status = "blocked"
    elif attempted and failed == attempted:
        status.status = "error"
    else:
        status.finalize()
    status.coverage_complete = (
        status.coverage_complete and status.completed_queries == len(queries)
    )
    log_collection_summary(status)
    return list(offers.values()), status


# ============================================================
# SOURCE 6 : France Travail (API officielle Offres d'emploi v2)
# ============================================================

def ft_token(client_id: str, client_secret: str,
             timeout: float | None = None,
             raise_on_error: bool = False) -> str | None:
    try:
        resp = requests.post(
            "https://entreprise.francetravail.fr/connexion/oauth2/access_token?realm=%2Fpartenaire",
            data={"grant_type": "client_credentials", "client_id": client_id,
                  "client_secret": client_secret,
                  "scope": "api_offresdemploiv2 o2dsoffre"},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=timeout or REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()["access_token"]
    except Exception as exc:
        # Ne jamais inclure client_id / client_secret dans les logs.
        print(f"[WARN] Auth France Travail échouée : {type(exc).__name__} {block_code(exc) or ''}",
              file=sys.stderr)
        if raise_on_error:
            raise
        return None


def fetch_france_travail_all(region_id: str = DEFAULT_REGION_ID,
                             region_config: dict | None = None,
                             regions: dict | None = None,
                             domains: dict | None = None,
                             active_ids: list[str] | None = None,
                             context: ScanContext | None = None,
                             metrics: RunMetrics | None = None) -> tuple[list[dict], SourceStatus]:
    regions = regions or REGIONS
    domains = domains or DOMAINS
    context = context or default_context()
    region_config = region_config or regions[region_id]
    status = source_status("France Travail", region_id, regions, context=context)
    started = time.monotonic()
    if not region_config["france_travail_available"]:
        status.status = "not_applicable_region"
        status.coverage_complete = False
        status.note = "France Travail est une API nationale française"
        status.duration_s = round(time.monotonic() - started, 1)
        print(f"[INFO] France Travail · {status.region_label} : non applicable à cette région")
        log_collection_summary(status)
        return [], status
    client_id = os.environ.get("FT_CLIENT_ID")
    client_secret = os.environ.get("FT_CLIENT_SECRET")
    if not client_id or not client_secret:
        status.status = "disabled_missing_credentials"
        status.coverage_complete = False
        status.note = "secrets FT_CLIENT_ID / FT_CLIENT_SECRET absents"
        status.duration_s = round(time.monotonic() - started, 1)
        log_collection_summary(status)
        return [], status

    status.network_request_count += 1
    try:
        token = ft_token(
            client_id,
            client_secret,
            timeout=context.profile.request_timeout_s,
            raise_on_error=True,
        )
    except Exception as exc:
        code = block_code(exc)
        status.status = "blocked" if code else "error"
        status.http_error = f"HTTP {code}" if code else type(exc).__name__
        status.blocked_count = 1 if code else 0
        if (isinstance(exc, (requests.Timeout, TimeoutError))
                or "timeout" in str(exc).lower()):
            status.timeout_count = 1
        status.note = "authentification OAuth échouée (voir logs)"
        status.coverage_complete = False
        status.duration_s = round(time.monotonic() - started, 1)
        log_collection_summary(status)
        return [], status
    if not token:
        status.status = "error"
        status.note = "authentification OAuth échouée (voir logs)"
        status.coverage_complete = False
        status.duration_s = round(time.monotonic() - started, 1)
        log_collection_summary(status)
        return [], status

    offers: dict[str, dict] = {}
    consecutive_blocks = attempted = failed = 0
    all_queries = list(dict.fromkeys(
        source_keywords(query) for query in active_queries(domains, active_ids)
    ))
    query_budget = context.profile.query_budgets.get(
        "France Travail", len(all_queries)
    )
    queries = all_queries[:query_budget]
    status.query_count = len(queries)
    if len(queries) < len(all_queries):
        status.coverage_complete = False
        status.note = f"budget de requêtes : {len(queries)}/{len(all_queries)}"
    for query in queries:
        if consecutive_blocks >= MAX_CONSECUTIVE_BLOCKS:
            status.note = "circuit breaker : requêtes restantes ignorées"
            status.coverage_complete = False
            status.interrupted = True
            break
        attempted += 1
        status.attempted_queries += 1
        query_succeeded = False
        query_complete = False
        query_seen: set[str] = set()
        stale_pages = 0
        max_pages = context.profile.max_pages.get(
            "France Travail", MAX_SOURCE_PAGES
        )
        for page in range(max_pages):
            start = page * FRANCE_TRAVAIL_PAGE_SIZE
            end = start + FRANCE_TRAVAIL_PAGE_SIZE - 1
            try:
                status.network_request_count += 1
                resp = requests.get(
                    "https://api.francetravail.io/partenaire/offresdemploi/v2/offres/search",
                    headers={"Authorization": f"Bearer {token}"},
                    # Stage natif + publications du dernier jour ; le filtre
                    # local UTC reste l'autorité à la seconde près.
                    params={"motsCles": query, "natureContrat": "FS",
                            "publieeDepuis": 1, "sort": "1",
                            "range": f"{start}-{end}"},
                    timeout=context.profile.request_timeout_s,
                )
                status.pages_fetched += 1
                if resp.status_code == 204:
                    results = []
                else:
                    resp.raise_for_status()
                    results = resp.json().get("resultats", [])
            except Exception as exc:
                code = block_code(exc)
                status.failed_queries += 1
                status.coverage_complete = False
                if code:
                    consecutive_blocks += 1
                    status.blocked_count += 1
                    status.http_error = f"HTTP {code}"
                else:
                    status.http_error = status.http_error or str(exc)[:200]
                    if (isinstance(exc, (requests.Timeout, TimeoutError))
                            or "timeout" in str(exc).lower()):
                        status.timeout_count += 1
                print(f"[WARN] France Travail '{query}' page {page + 1} "
                      f"a échoué : {exc}", file=sys.stderr)
                break
            query_succeeded = True
            consecutive_blocks = 0
            status.raw_count += len(results)
            print(f"[INFO] France Travail '{query}' page {page + 1} : "
                  f"{len(results)} résultats bruts")
            if not results:
                query_complete = True
                break
            page_keys = {clean_text(r.get("id")) for r in results} - {""}
            new_keys = page_keys - query_seen
            query_seen.update(page_keys)
            stale_pages = stale_pages + 1 if not new_keys else 0
            for r in results:
                url = (r.get("origineOffre") or {}).get("urlOrigine") or \
                      f"https://candidat.francetravail.fr/offres/recherche/detail/{r.get('id')}"
                offer, reason = make_offer(
                    r.get("intitule"), (r.get("entreprise") or {}).get("nom"),
                    (r.get("lieuTravail") or {}).get("libelle"),
                    url, "France Travail", r.get("dateCreation") or "",
                    confirmed_internship=True,
                    description=clean_text(r.get("description")),
                    domains=domains, active_ids=active_ids,
                    region_id=region_id, regions=regions,
                    context=context, external_id=clean_text(r.get("id")),
                    metrics=metrics,
                )
                register_offer(status, offers, offer, reason, metrics)
            content_range = resp.headers.get("Content-Range", "")
            total_match = re.search(r"/(\d+)$", content_range)
            total = int(total_match.group(1)) if total_match else 0
            if (len(results) < FRANCE_TRAVAIL_PAGE_SIZE
                    or (total and end + 1 >= total)
                    or stale_pages >= context.profile.max_stale_pages):
                query_complete = True
                break
            page_dates = [
                parse_offer_date(r.get("dateCreation"), context.run_at)
                for r in results
            ]
            if (page_dates and all(page_dates)
                    and min(page_dates) < context.cutoff):
                # sort=1 garantit l'ordre décroissant côté France Travail.
                query_complete = True
                break
        else:
            status.coverage_complete = False
            status.interrupted = True
            status.note = (status.note + " · " if status.note else "") + \
                f"plafond de {max_pages} pages atteint"
        if not query_succeeded:
            failed += 1
        elif query_complete:
            status.completed_queries += 1
        else:
            status.coverage_complete = False

    status.duration_s = round(time.monotonic() - started, 1)
    if consecutive_blocks >= MAX_CONSECUTIVE_BLOCKS or (
            attempted and failed == attempted and status.http_error.startswith("HTTP")):
        status.status = "blocked"
    elif attempted and failed == attempted:
        status.status = "error"
    else:
        status.finalize()
    status.coverage_complete = (
        status.coverage_complete and status.completed_queries == len(queries)
    )
    log_collection_summary(status)
    return list(offers.values()), status


# ============================================================
# SOURCE 7 : LinkedIn Posts (X-ray via moteur public uniquement)
# ============================================================

def load_xray_rotation_state(path: Path = XRAY_STATE_FILE) -> XRayRotationState:
    """Charge le curseur X-ray ; un fichier absent ou ancien repart à zéro."""
    if not path.exists():
        return XRayRotationState()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and "profiles" in payload:
            # Le choix du profil est fait par l'appelant ; ce format est lu
            # directement dans :func:`xray_state_for_profile`.
            return XRayRotationState()
        return XRayRotationState.from_mapping(payload if isinstance(payload, dict) else {})
    except (OSError, ValueError, TypeError):
        return XRayRotationState()


def load_xray_state_catalog(path: Path = XRAY_STATE_FILE) -> dict:
    if not path.exists():
        return {"schema_version": 1, "profiles": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema_version": 1, "profiles": {}}
    if not isinstance(payload, dict):
        return {"schema_version": 1, "profiles": {}}
    if "profiles" not in payload:
        payload = {"schema_version": 1, "profiles": {DEFAULT_PROFILE_ID: payload}}
    if not isinstance(payload.get("profiles"), dict):
        payload["profiles"] = {}
    return payload


def xray_state_for_profile(catalog: dict, profile_id: str) -> XRayRotationState:
    profiles = catalog.get("profiles", {}) if isinstance(catalog, dict) else {}
    return XRayRotationState.from_mapping(profiles.get(profile_id, {}))


def update_xray_state_catalog(catalog: dict, profile_id: str,
                              state: XRayRotationState) -> dict:
    updated = dict(catalog or {})
    updated["schema_version"] = 1
    profiles = dict(updated.get("profiles", {}))
    profiles[profile_id] = state.as_dict()
    updated["profiles"] = profiles
    return updated


def fetch_linkedin_posts(
        *,
        context: ScanContext,
        domains: dict | None = None,
        active_ids: list[str] | None = None,
        regions: dict | None = None,
        region_id: str = DEFAULT_REGION_ID,
        provider=None,
        rotation_state: XRayRotationState | dict | None = None,
        metrics: RunMetrics | None = None,
) -> tuple[list[dict], SourceStatus, XRayRotationState]:
    """Collecte best-effort de posts indexés, sans jamais ouvrir LinkedIn."""
    domains = domains or DOMAINS
    regions = regions or REGIONS
    active_ids = active_ids or list(context.profile.domains)
    status = source_status(
        "LinkedIn Posts", region_id, regions, context=context
    )
    started = time.monotonic()
    provider = provider or DuckDuckGoHtmlProvider(
        timeout=context.profile.request_timeout_s
    )
    budget = context.profile.query_budgets.get("LinkedIn Posts", 0)
    plan, next_state = build_xray_query_plan(
        profile_id=context.profile.id,
        domains=domains,
        domain_ids=active_ids,
        query_budget=budget,
        state=rotation_state,
    )
    status.query_count = len(plan)
    covered_domains = {item.domain_id for item in plan}
    if covered_domains != set(active_ids):
        status.coverage_complete = False
        status.note = (
            f"rotation X-ray : {len(covered_domains)}/{len(active_ids)} domaines ce run"
        )
    if not plan:
        status.status = "not_queried_budget"
        status.coverage_complete = False
        status.note = "budget X-ray nul"
        status.duration_s = round(time.monotonic() - started, 1)
        log_collection_summary(status)
        return [], status, next_state

    offers: dict[str, dict] = {}
    failed_queries = 0
    consecutive_blocks = 0
    max_pages = context.profile.max_pages.get("LinkedIn Posts", 1)
    for item in plan:
        if consecutive_blocks >= MAX_CONSECUTIVE_BLOCKS:
            status.interrupted = True
            status.coverage_complete = False
            status.note = (status.note + " · " if status.note else "") + \
                "circuit breaker X-ray : budget restant ignoré"
            break
        query_started = time.monotonic()
        query_raw = query_accepted = query_rejected = query_duplicates = 0
        query_reasons: dict[str, int] = defaultdict(int)
        status.attempted_queries += 1
        query_failed = False
        query_complete = False
        query_seen: set[str] = set()
        for page in range(1, max_pages + 1):
            try:
                status.network_request_count += 1
                status.pages_fetched += 1
                results = provider.search(
                    item.query,
                    freshness="d",
                    page=page,
                    max_results=30,
                    now=context.run_at,
                )
            except XRayBlockedError as exc:
                query_failed = True
                failed_queries += 1
                status.failed_queries += 1
                status.blocked_count += 1
                status.coverage_complete = False
                code = getattr(exc, "status_code", None)
                status.http_error = f"HTTP {code}" if code else "moteur bloqué"
                consecutive_blocks += 1
                break
            except XRayTimeoutError as exc:
                query_failed = True
                failed_queries += 1
                status.failed_queries += 1
                status.timeout_count += 1
                status.coverage_complete = False
                status.http_error = str(exc)[:200]
                consecutive_blocks = 0
                break
            except (XRayProviderError, Exception) as exc:
                # L'exception large isole ce connecteur expérimental sans
                # masquer d'informations sensibles dans le journal.
                query_failed = True
                failed_queries += 1
                status.failed_queries += 1
                status.coverage_complete = False
                status.http_error = f"{type(exc).__name__}: {str(exc)[:160]}"
                consecutive_blocks = 0
                break

            consecutive_blocks = 0
            status.raw_count += len(results)
            query_raw += len(results)
            if not results:
                query_complete = True
                break
            page_ids = {result.external_id for result in results}
            if page_ids and page_ids.issubset(query_seen):
                query_complete = True
                break
            query_seen.update(page_ids)
            for result in results:
                if not is_relevant_internship_post(result):
                    status.add_reject(REASON_NOT_INTERNSHIP)
                    query_rejected += 1
                    query_reasons[REASON_NOT_INTERNSHIP] += 1
                    continue
                before_duplicates = status.duplicate_count
                before_accepted = status.accepted_count
                date_value = (
                    result.published_at.isoformat()
                    if result.published_at is not None else ""
                )
                offer, reason = make_offer(
                    result.title,
                    result.author or "Post LinkedIn",
                    regions[region_id]["label"],
                    result.url,
                    "LinkedIn Posts",
                    date_value,
                    confirmed_internship=True,
                    description=result.snippet,
                    domains=domains,
                    active_ids=active_ids,
                    region_id=region_id,
                    regions=regions,
                    context=context,
                    external_id=result.external_id,
                    contact_direct=True,
                    metrics=metrics,
                )
                register_offer(status, offers, offer, reason, metrics)
                if status.duplicate_count > before_duplicates:
                    query_duplicates += 1
                    query_reasons[REASON_DUPLICATE] += 1
                elif status.accepted_count > before_accepted:
                    query_accepted += 1
                else:
                    query_rejected += 1
                    query_reasons[reason or "raison inconnue"] += 1
            if len(results) < 30:
                query_complete = True
                break
        else:
            status.coverage_complete = False
            status.interrupted = True

        if not query_failed:
            status.completed_queries += 1
        if not query_complete and not query_failed:
            status.coverage_complete = False
        status.query_reports.append({
            "query": item.query,
            "domain_id": item.domain_id,
            "provider": getattr(provider, "name", type(provider).__name__),
            "duration_s": round(time.monotonic() - query_started, 3),
            "raw_count": query_raw,
            "accepted_count": query_accepted,
            "rejected_count": query_rejected,
            "duplicate_count": query_duplicates,
            "reject_reasons": dict(query_reasons),
            "status": "error" if query_failed else "success",
        })

    status.duration_s = round(time.monotonic() - started, 1)
    if status.completed_queries == 0 and (failed_queries or status.interrupted):
        status.status = "blocked" if status.blocked_count else "error"
    else:
        status.finalize()
    status.coverage_complete = (
        status.coverage_complete and status.completed_queries == len(plan)
    )
    log_collection_summary(status)
    return list(offers.values()), status, next_state


# ============================================================
# ORCHESTRATION
# ============================================================

def run_source_across_regions(
        source_name: str,
        fetcher,
        selected_regions: dict[str, dict],
        *,
        context: ScanContext | None = None,
        max_workers: int = 1,
) -> tuple[list[dict], list[SourceStatus]]:
    """Exécute une source par région avec un parallélisme explicitement borné."""
    offers: dict[str, dict] = {}
    statuses: list[SourceStatus] = []

    def execute(region_id: str, region_config: dict):
        try:
            return fetcher(region_id, region_config)
        except Exception as exc:  # ceinture : une région n'arrête pas les autres
            code = block_code(exc)
            safe_error = f"HTTP {code}" if code else type(exc).__name__
            print(f"[ERREUR] {source_name} · {region_config['label']} : "
                  f"{safe_error}", file=sys.stderr)
            status = source_status(
                source_name, region_id, selected_regions, status="error",
                http_error=safe_error, coverage_complete=False,
                context=context,
            )
            return [], status

    items = list(selected_regions.items())
    if max_workers > 1 and len(items) > 1:
        with ThreadPoolExecutor(max_workers=min(max_workers, len(items))) as pool:
            futures = [pool.submit(execute, region_id, config)
                       for region_id, config in items]
            regional_results = [future.result() for future in futures]
    else:
        regional_results = [execute(region_id, config)
                            for region_id, config in items]

    for regional_offers, status in regional_results:
        for offer in regional_offers:
            duplicate_id = find_duplicate_offer_id(offers, offer)
            if duplicate_id:
                merge_offer_metadata(offers[duplicate_id], offer)
            else:
                offers[offer["id"]] = offer
        statuses.append(status)
    return list(offers.values()), statuses


def run_all_sources(domains: dict | None = None,
                    active_domain_ids: list[str] | None = None,
                    regions: dict | None = None,
                    active_region_ids: list[str] | None = None,
                    budget: ScanBudget | None = None,
                    context: ScanContext | None = None,
                    metrics: RunMetrics | None = None,
                    store: dict[str, dict] | None = None,
                    xray_provider=None,
                    xray_rotation_state: XRayRotationState | dict | None = None,
                    xray_state_sink: dict | None = None) -> tuple[dict[str, dict], list[SourceStatus]]:
    """Croise les régions actives avec les requêtes des domaines actifs.

    Les régions restent séquentielles au sein d'une même source pour ne pas
    marteler le site ; les sources indépendantes s'exécutent en parallèle.
    """
    domains = domains or DOMAINS
    regions = regions or REGIONS
    context = context or default_context()
    active_domain_ids = active_domain_ids or list(context.profile.domains)
    active_region_ids = active_region_ids or list(context.profile.regions)
    selected_regions = active_regions(regions, active_region_ids)
    budget = budget or calculate_scan_budget(
        len(active_domain_ids), len(active_region_ids), context.profile
    )

    known_identity_index: dict[str, dict] = {}
    for known_offer in (store or {}).values():
        for identity_key in known_offer.get("identity_keys", []):
            known_identity_index.setdefault(identity_key, known_offer)

    def jobspy_fetcher(site: str, label: str):
        return lambda region_id, region_config: fetch_jobspy_site(
            site, label, region_id=region_id, region_config=region_config,
            regions=regions, domains=domains, active_ids=active_domain_ids,
            results_wanted=budget.results_wanted.get(
                site, JOBSPY_RESULTS_WANTED.get(site, 100)
            ),
            context=context, metrics=metrics,
        )

    fetchers = {
        "LinkedIn": jobspy_fetcher("linkedin", "LinkedIn"),
        "Indeed": jobspy_fetcher("indeed", "Indeed"),
        "Google Jobs": jobspy_fetcher("google", "Google Jobs"),
        "APEC": lambda region_id, region_config: fetch_apec_all(
            region_id, region_config, regions, domains, active_domain_ids,
            context, metrics, known_identity_index),
        "Adzuna": lambda region_id, region_config: fetch_adzuna_all(
            region_id, region_config, regions, domains, active_domain_ids,
            context, metrics),
        "France Travail": lambda region_id, region_config: fetch_france_travail_all(
            region_id, region_config, regions, domains, active_domain_ids,
            context, metrics),
    }
    selected_sources = list(context.profile.sources)
    results: dict[str, tuple[list[dict], list[SourceStatus]]] = {}
    next_xray_state = (
        xray_rotation_state if isinstance(xray_rotation_state, XRayRotationState)
        else XRayRotationState.from_mapping(xray_rotation_state)
    )

    def run_named_source(name: str):
        nonlocal next_xray_state
        if name == "LinkedIn Posts":
            region_id = (DEFAULT_REGION_ID if DEFAULT_REGION_ID in selected_regions
                         else next(iter(selected_regions)))
            offers, status, next_state = fetch_linkedin_posts(
                context=context,
                domains=domains,
                active_ids=active_domain_ids,
                regions=regions,
                region_id=region_id,
                provider=xray_provider,
                rotation_state=xray_rotation_state,
                metrics=metrics,
            )
            next_xray_state = next_state
            return offers, [status]
        return run_source_across_regions(
            name,
            fetchers[name],
            selected_regions,
            context=context,
            max_workers=context.profile.max_parallel_regions,
        )

    with ThreadPoolExecutor(
            max_workers=min(context.profile.max_parallel_sources,
                            max(1, len(selected_sources)))) as pool:
        futures = {name: pool.submit(run_named_source, name)
                   for name in selected_sources}
        for name, future in futures.items():
            results[name] = future.result()

    if xray_state_sink is not None:
        xray_state_sink["state"] = next_xray_state

    all_offers: dict[str, dict] = {}
    statuses: list[SourceStatus] = []
    for name in selected_sources:
        offers, source_statuses = results[name]
        for offer in offers:
            duplicate_id = find_duplicate_offer_id(all_offers, offer)
            if duplicate_id:
                merge_offer_metadata(all_offers[duplicate_id], offer)
            else:
                all_offers[offer["id"]] = offer
        statuses.extend(source_statuses)
    return all_offers, statuses


def save_status_json(statuses: list[SourceStatus], scan_duration_s: float,
                     active_ids: list[str] | None = None,
                     active_region_ids: list[str] | None = None,
                     budget: ScanBudget | None = None,
                     context: ScanContext | None = None,
                     new_offer_count: int = 0,
                     output_path: Path | None = None,
                     update_legacy: bool = True) -> dict:
    context = context or default_context()
    active_ids = active_ids or list(context.profile.domains)
    active_region_ids = active_region_ids or list(context.profile.regions)
    budget = budget or calculate_scan_budget(
        len(active_ids), len(active_region_ids), context.profile
    )
    payload = {
        "schema_version": 2,
        "profile_id": context.profile.id,
        "generated_at": context.run_at.isoformat(timespec="seconds"),
        "scan_duration_s": round(scan_duration_s, 1),
        "window_hours": context.profile.window_hours,
        "new_offer_count": new_offer_count,
        "active_domains": active_ids,
        "active_regions": active_region_ids,
        "active_sources": list(context.profile.sources),
        "query_count": len(active_queries(active_ids=active_ids)),
        "combination_count": budget.combination_count,
        "results_wanted": budget.results_wanted,
        "sources": [dict(asdict(st), label=status_label(st)) for st in statuses],
    }
    target = output_path or PROFILE_STATUS_FILES.get(
        context.profile.id,
        Path(f"data/source_status_{context.profile.id}.json"),
    )
    atomic_write_json(target, payload)
    if update_legacy and target != STATUS_FILE:
        atomic_write_json(STATUS_FILE, payload)
    return payload


def load_profile_status_payloads() -> dict[str, dict]:
    snapshots: dict[str, dict] = {}
    for profile_id, path in PROFILE_STATUS_FILES.items():
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            snapshots[profile_id] = payload
    # Migration rétrocompatible : avant les profils, seul source_status.json
    # existait et représentait le scan général. On l'expose comme snapshot
    # FULL tant que son fichier dédié n'a pas encore été produit, sans faire
    # réapparaître une source désormais retirée.
    if DEFAULT_PROFILE_ID not in snapshots and STATUS_FILE.exists():
        try:
            legacy = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            legacy = None
        if isinstance(legacy, dict):
            migrated = dict(legacy)
            migrated["profile_id"] = DEFAULT_PROFILE_ID
            migrated.setdefault(
                "window_hours", PROFILES[DEFAULT_PROFILE_ID].window_hours
            )
            migrated["sources"] = [
                row for row in legacy.get("sources", [])
                if isinstance(row, dict) and row.get("source") in SOURCE_NAMES
            ]
            snapshots[DEFAULT_PROFILE_ID] = migrated
    return snapshots


def build_metrics_record(
        context: ScanContext,
        statuses: list[SourceStatus],
        run_metrics: RunMetrics,
        *,
        new_offer_count: int,
        offer_count: int,
        store_size_bytes: int = 0,
        seen_size_bytes: int = 0,
        benchmark: bool = False,
) -> dict:
    return {
        "profile_id": context.profile.id,
        "run_at": context.run_at.isoformat(timespec="seconds"),
        "benchmark": benchmark,
        "window_hours": context.profile.window_hours,
        "new_offer_count": new_offer_count,
        "offer_count": offer_count,
        "store_size_bytes": store_size_bytes,
        "seen_size_bytes": seen_size_bytes,
        "phases": run_metrics.payload(),
        "totals": {
            "network_request_count": sum(st.network_request_count for st in statuses),
            "pages_fetched": sum(st.pages_fetched for st in statuses),
            "raw_count": sum(st.raw_count for st in statuses),
            "accepted_count": sum(st.accepted_count for st in statuses),
            "rejected_count": sum(st.rejected_count for st in statuses),
            "duplicate_count": sum(st.duplicate_count for st in statuses),
            "timeout_count": sum(st.timeout_count for st in statuses),
            "blocked_count": sum(st.blocked_count for st in statuses),
            "detail_request_count": sum(st.detail_request_count for st in statuses),
        },
        "sources": [asdict(status) for status in statuses],
    }


def save_metrics_record(record: dict, path: Path = METRICS_FILE,
                        history_limit: int = 50) -> dict:
    try:
        current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, json.JSONDecodeError):
        current = {}
    histories = dict(current.get("profiles", {})) if isinstance(current, dict) else {}
    profile_id = str(record["profile_id"])
    history = list(histories.get(profile_id, []))
    history.append(record)
    histories[profile_id] = history[-history_limit:]
    payload = {"schema_version": 1, "profiles": histories}
    atomic_write_json(path, payload)
    return payload


# ============================================================
# MEMOIRE / EMAIL / DASHBOARD
# ============================================================

def load_seen_document(path: Path = SEEN_FILE,
                       run_at: datetime | None = None) -> dict:
    """Charge et migre en mémoire le schéma seen v2 sans écrire."""
    if not path.exists():
        payload = None
    else:
        payload = json.loads(path.read_text(encoding="utf-8"))
    return radar_store.migrate_seen_memory(payload, run_at=run_at)


def load_seen(path: Path = SEEN_FILE) -> set:
    """Vue ensemble rétrocompatible de la mémoire horodatée."""
    return radar_store.seen_offer_ids(load_seen_document(path))


def save_seen_document(document: dict, path: Path = SEEN_FILE) -> None:
    atomic_write_json(path, document)


def save_seen(ids: set, path: Path = SEEN_FILE,
              context: ScanContext | None = None) -> None:
    """Compatibilité historique : met à jour seen v2 à partir d'un set."""
    context = context or default_context()
    document, _ = radar_store.update_seen_memory(
        load_seen_document(path, context.run_at),
        ids,
        run_at=context.run_at,
        profile_id=context.profile.id,
        retention_days=SEEN_RETENTION_DAYS,
    )
    save_seen_document(document, path)


def load_offer_store(path: Path = OFFERS_FILE,
                     run_at: datetime | None = None) -> dict[str, dict]:
    """Charge le store complet ; son absence est une migration valide."""
    if not path.exists():
        data = {}
    else:
        data = json.loads(path.read_text(encoding="utf-8"))
    migrated = radar_store.migrate_offer_store(data, run_at=run_at)
    for offer_id_value, entry in migrated.items():
        source = clean_text(entry.get("source"))
        url = clean_text(entry.get("url"))
        canonical = (canonicalize_linkedin_post_url(url)
                     if source == "LinkedIn Posts" else canonicalize_url(url)) or url
        native_id = infer_external_id(source, canonical)
        entry["id"] = offer_id_value
        entry["url"] = canonical
        entry["sources"] = list(dict.fromkeys(
            entry.get("sources", []) + ([source] if source else [])
        ))
        entry.setdefault("source_urls", {})
        if source and canonical:
            entry["source_urls"].setdefault(source, canonical)
        entry.setdefault("external_ids", {})
        if source and native_id:
            entry["external_ids"].setdefault(source, native_id)
        generated_keys = exact_identity_keys(
            source, native_id, canonical,
            clean_text(entry.get("title")), clean_text(entry.get("company")),
        )
        entry["identity_keys"] = list(dict.fromkeys(
            entry.get("identity_keys", []) + generated_keys
        ))
        entry.setdefault("legacy_ids", [])
        if not entry.get("probable_fingerprint"):
            entry["probable_fingerprint"] = probable_offer_fingerprint(
                clean_text(entry.get("title")), clean_text(entry.get("company")),
                clean_text(entry.get("location")),
                clean_text(entry.get("contract_type"), "internship"),
            )
    return migrated


def save_offer_store(store: dict[str, dict], path: Path = OFFERS_FILE) -> None:
    ordered = {offer_id_value: store[offer_id_value] for offer_id_value in sorted(store)}
    atomic_write_json(path, ordered)


def source_succeeded_for_offer(offer: dict, statuses: list[SourceStatus],
                               context: ScanContext | None = None) -> bool:
    """Compatibilité : vrai uniquement pour une absence pertinente et saine."""
    return radar_store.offer_is_pertinent_absence(
        offer, statuses, context or default_context()
    )


def _store_datetime(value: str) -> datetime | None:
    return radar_store.parse_store_datetime(value)


def reconcile_offer_store(found_offers: list[dict], statuses: list[SourceStatus],
                          store: dict[str, dict] | None = None,
                          run_at: datetime | None = None,
                          retention_days: int = OFFER_RETENTION_DAYS,
                          context: ScanContext | None = None) -> dict[str, dict]:
    """Réconciliation profile-aware, puis purge des seules entrées purgeables."""
    context = context or default_context(run_at)
    base = store if store is not None else load_offer_store(run_at=context.run_at)
    merged = radar_store.reconcile_offer_store(
        found_offers,
        statuses,
        context,
        store=base,
        expiration_hours=EXPIRATION_MISSING_HOURS,
        successful_absences=EXPIRATION_SUCCESSFUL_SCANS,
    )
    return radar_store.purge_offer_store(
        merged, now=context.run_at, retention_days=retention_days
    )


def dashboard_offers_from_store(store: dict[str, dict],
                                now: datetime | None = None,
                                days_window: int = MAX_OFFER_AGE_DAYS) -> list[dict]:
    """Vue active rétrocompatible ; l'expiration est gérée par preuves."""
    del now, days_window
    return radar_store.active_dashboard_offers(store)


def dashboard_offer_sections_from_store(
        store: dict[str, dict]) -> tuple[list[dict], list[dict]]:
    return radar_store.dashboard_offer_sections(store)


def send_email(new_offers: list[dict], dashboard_url: str,
               domains: dict | None = None, config: dict | None = None,
               regions: dict | None = None) -> None:
    domains = domains or DOMAINS
    config = config or APP_CONFIG
    regions = regions or REGIONS
    smtp_user = os.environ["SMTP_USER"]
    smtp_pass = os.environ["SMTP_PASS"]
    to_addr = os.environ.get("ALERT_TO", smtp_user)

    n = len(new_offers)
    subject = (f"🎯 {config['app_name']} · {n} nouvelle{'s' if n > 1 else ''} "
               f"offre{'s' if n > 1 else ''}")

    by_source = {}
    for o in new_offers:
        by_source.setdefault(o["source"], []).append(o)

    parts = [f"<h2>{n} nouvelle{'s' if n>1 else ''} offre{'s' if n>1 else ''}</h2>"]
    for src, lst in sorted(by_source.items()):
        parts.append(f"<h3>{html.escape(src)} ({len(lst)})</h3>")
        for o in lst:
            badge = (f" ✅ <b>{html.escape(config['highlight_label'])}</b>"
                     if o.get("highlighted") else "")
            labels = [domains[domain_id]["label"] for domain_id in o.get("domains", [])
                      if domain_id in domains]
            region_labels = [regions[region_id]["label"] for region_id in o.get("regions", [])
                             if region_id in regions]
            metadata_labels = labels + region_labels
            domain_line = (f"<br><small>{html.escape(' · '.join(metadata_labels))}</small>"
                           if metadata_labels else "")
            url = safe_url(o["url"])
            title = html.escape(o["title"])
            link = f"<a href='{url}'><b>{title}</b></a>" if url else f"<b>{title}</b>"
            parts.append(
                f"<p>{link}<br>"
                f"{html.escape(o['company'])} · {html.escape(o['location'])}{badge}"
                f"{domain_line}</p>"
            )
    parts.append(f"<hr><p><a href='{html.escape(dashboard_url, quote=True)}'>📊 Ouvrir le dashboard complet</a></p>")
    parts.append(f"<p style='color:#888;font-size:12px'>{html.escape(config['tagline'])}</p>")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = to_addr
    msg.attach(MIMEText("".join(parts), "html"))

    with smtplib.SMTP(os.environ.get("SMTP_HOST", "smtp.gmail.com"),
                      int(os.environ.get("SMTP_PORT", "587"))) as s:
        s.starttls()
        s.login(smtp_user, smtp_pass)
        s.sendmail(smtp_user, [to_addr], msg.as_string())


STATUS_CSS = {
    "success": "ok",
    "empty": "warn",
    "blocked": "bad",
    "error": "bad",
    "disabled_missing_credentials": "muted",
    "not_applicable_region": "muted",
    "not_queried_budget": "muted",
    "retired": "muted",
}


def render_dashboard(offers: list[dict], new_ids: set,
                     statuses: list[SourceStatus] | None = None,
                     scan_duration_s: float = 0.0,
                     domains: dict | None = None,
                     config: dict | None = None,
                     active_ids: list[str] | None = None,
                     regions: dict | None = None,
                     active_region_ids: list[str] | None = None,
                     expired_offers: list[dict] | None = None,
                     profile_status_payloads: dict[str, dict] | None = None,
                     context: ScanContext | None = None,
                     output_path: Path | None = None) -> None:
    domains = domains or DOMAINS
    config = config or APP_CONFIG
    regions = regions or REGIONS
    active_ids = active_ids or enabled_domain_ids(domains)
    active_region_ids = active_region_ids or enabled_region_ids(regions)
    expired_offers = expired_offers or []
    profile_status_payloads = profile_status_payloads or load_profile_status_payloads()
    target = output_path or DOCS_INDEX
    target.parent.mkdir(parents=True, exist_ok=True)
    generated_at = context.run_at if context else datetime.now(timezone.utc)
    now = generated_at.strftime("%d/%m/%Y %H:%M UTC")
    generated_at_iso = generated_at.isoformat()
    statuses = statuses or [source_status(name, active_region_ids[0], regions)
                            for name in SOURCE_NAMES]
    active_region_labels = [regions[region_id]["label"]
                            for region_id in active_region_ids]

    final_counts = {name: 0 for name in SOURCE_NAMES}
    domain_counts = {domain_id: 0 for domain_id in active_ids}
    region_counts = {region_id: 0 for region_id in active_region_ids}
    for o in offers:
        for source_name in o.get("sources") or [o.get("source")]:
            if source_name in final_counts:
                final_counts[source_name] += 1
        for domain_id in o.get("domains", []):
            if domain_id in domain_counts:
                domain_counts[domain_id] += 1
        for region_id in o.get("regions", []):
            if region_id in region_counts:
                region_counts[region_id] += 1

    def render_card(o: dict, *, likely_expired: bool = False) -> str:
        is_new = o["id"] in new_ids
        date_known = o.get("date_known", bool(o.get("date")))
        if date_known and o.get("date"):
            published = datetime.fromisoformat(o["date"]).strftime("%d/%m/%Y")
            published_line = f"Publiée {publication_age(o['date'])} · {published}"
        else:
            published_line = "Date de publication inconnue · conservée par précaution"
        oid = html.escape(str(o["id"]), quote=True)
        title = html.escape(o["title"])
        company = html.escape(o["company"])
        location = html.escape(o["location"])
        offer_sources = [source for source in (o.get("sources") or [o.get("source")])
                         if source in SOURCE_NAMES]
        if not offer_sources:
            offer_sources = [clean_text(o.get("source"), "Source inconnue")]
        source = html.escape(str(o.get("source", offer_sources[0])), quote=True)
        sources_attr = html.escape(",".join(offer_sources), quote=True)
        search_attr = html.escape(
            " ".join(str(o.get(field, "")) for field in ("title", "company", "location")),
            quote=True,
        )
        offer_domains = [domain_id for domain_id in o.get("domains", [])
                         if domain_id in active_ids]
        domain_attr = html.escape(",".join(offer_domains), quote=True)
        domain_tags = "".join(
            f'<span class="tag domain">{html.escape(domains[domain_id]["label"])}</span>'
            for domain_id in offer_domains
        )
        offer_regions = [region_id for region_id in o.get("regions", [])
                         if region_id in active_region_ids]
        region_attr = html.escape(",".join(offer_regions), quote=True)
        region_tags = "".join(
            f'<span class="tag region">{html.escape(regions[region_id]["label"])}</span>'
            for region_id in offer_regions
        )
        highlighted = bool(o.get("highlighted"))
        url = safe_url(o["url"])
        link = (f'<a href="{url}" target="_blank" rel="noopener noreferrer">{title}</a>'
                if url else title)
        scores_attr = html.escape(
            json.dumps(o.get("domain_scores", {}), ensure_ascii=False), quote=True
        )
        sort_date_attr = html.escape(
            str(o.get("sort_date") or o.get("date") or ""), quote=True
        )
        keywords: list[str] = []
        for domain_id in offer_domains:
            for keyword in o.get("matched_keywords", {}).get(domain_id, []):
                if keyword not in keywords:
                    keywords.append(keyword)
                if len(keywords) == 5:
                    break
            if len(keywords) == 5:
                break
        keyword_line = (
            '<div class="matched-keywords"><span class="matched-keywords-label">match :</span> '
            + html.escape(" · ".join(keywords)) + "</div>"
            if keywords else ""
        )
        source_tags = "".join(
            f'<span class="tag src">{html.escape(source_name)}</span>'
            for source_name in offer_sources
        )
        expired = likely_expired or bool(o.get("likely_expired"))
        contact_tag = ('<span class="tag contact-direct">contact direct</span>'
                       if o.get("contact_direct") else "")
        expired_tag = ('<span class="tag likely-expired">Probablement pourvue</span>'
                       if expired else "")
        return f"""
      <article class="job {'is-new' if is_new else ''} {'likely-expired' if expired else ''}" data-id="{oid}"
               data-src="{source}" data-sources="{sources_attr}" data-domains="{domain_attr}"
               data-regions="{region_attr}" data-highlight="{'1' if highlighted else '0'}"
               data-search="{search_attr}" data-sort-date="{sort_date_attr}"
               data-relevance="{html.escape(str(o.get('relevance_score', 0)), quote=True)}"
               data-domain-scores="{scores_attr}" data-likely-expired="{'1' if expired else '0'}">
        <div class="job-top"><div class="job-content">
          <h3>{link}</h3>
          <div class="meta">{company} · {location}</div>
          <div class="published">{published_line}</div>
          {keyword_line}
        </div>
        <button class="apply-btn" type="button" data-offer-id="{oid}">✓ Marquer comme postulé</button></div>
        <div class="tags" translate="no">{'<span class="tag new">NOUVEAU</span>' if is_new else ''}
          {f'<span class="tag highlight">✓ {html.escape(config["highlight_label"])}</span>' if highlighted else ''}
          {'<span class="tag date-unknown">date inconnue</span>' if not date_known else ''}
          {contact_tag}{expired_tag}{domain_tags}{region_tags}{source_tags}</div>
      </article>"""

    cards = [render_card(offer) for offer in sort_offers(offers)]
    expired_cards = [render_card(offer, likely_expired=True)
                     for offer in sort_offers(expired_offers)]

    multiple_regions = len(active_region_ids) > 1
    status_rows = "".join(
        f'<span class="src-stat st-{STATUS_CSS.get(st.status, "warn")}">'
        f'<b>{html.escape(st.source)}'
        f'{" · " + html.escape(st.region_label) if multiple_regions else ""}</b> · '
        f'{html.escape(status_label(st, st.accepted_count))}</span>'
        for st in statuses
    )

    source_chips = []
    status_priority = {"blocked": 0, "error": 1, "disabled_missing_credentials": 2,
                       "empty": 3, "not_applicable_region": 4,
                       "not_queried_budget": 5, "success": 6}
    for source_name in SOURCE_NAMES:
        related = [st for st in statuses if st.source == source_name]
        if not related:
            related = [source_status(source_name, active_region_ids[0], regions)]
        representative = min(related, key=lambda st: status_priority.get(st.status, 9))
        name = html.escape(source_name, quote=True)
        count = final_counts.get(source_name, 0)
        hint = html.escape(chip_hint(representative, count))
        note_attr = ("" if count else
                     f' data-note="{html.escape(status_label(representative, count), quote=True)}"')
        source_chips.append(
            f'<button class="fchip source-chip" data-src="{name}"{note_attr} '
            f'aria-pressed="false">{name} <b>{hint}</b></button>'
        )
    filter_btns = "".join(source_chips)

    domain_chips = "".join(
        f'<button class="fchip domain-chip" data-domain="{html.escape(domain_id, quote=True)}" '
        f'aria-pressed="false">'
        f'{html.escape(domains[domain_id]["label"])} <b>({domain_counts[domain_id]})</b></button>'
        for domain_id in active_ids
    )
    single_domain_class = " single-domain" if len(active_ids) == 1 else ""
    default_region_id = (DEFAULT_REGION_ID if DEFAULT_REGION_ID in active_region_ids
                         else active_region_ids[0])
    region_chips = "".join(
        f'<button class="fchip region-chip{(" on" if region_id == default_region_id else "")}" '
        f'data-region="{html.escape(region_id, quote=True)}" '
        f'aria-pressed="{("true" if region_id == default_region_id else "false")}">'
        f'{html.escape(regions[region_id]["label"])} <b>({region_counts[region_id]})</b></button>'
        for region_id in active_region_ids
    )
    single_region_class = " single-region" if len(active_region_ids) == 1 else ""
    app_name = html.escape(str(config["app_name"]))
    tagline = html.escape(str(config["tagline"]))
    highlight_label = html.escape(str(config["highlight_label"]))
    new_offer_count = sum(1 for offer in offers if offer["id"] in new_ids)
    successful_statuses = sum(1 for status in statuses if status.status == "success")
    degraded_statuses = sum(
        1 for status in statuses if status.status in {"blocked", "error"}
    )

    def profile_card(profile_id: str, label: str) -> str:
        payload = profile_status_payloads.get(profile_id, {})
        source_rows = payload.get("sources", []) if isinstance(payload, dict) else []
        rows_by_source: dict[str, list[dict]] = defaultdict(list)
        for source_payload in source_rows:
            rows_by_source[str(source_payload.get("source", ""))].append(source_payload)
        healthy = 0
        for source_name in PROFILES[profile_id].sources:
            applicable_rows = [
                row for row in rows_by_source.get(source_name, [])
                if row.get("status") != "not_applicable_region"
            ]
            if (applicable_rows and all(
                    row.get("status") in {"success", "empty"}
                    and row.get("coverage_complete", True)
                    and not row.get("interrupted", False)
                    and int(row.get("failed_queries", 0) or 0) == 0
                    and int(row.get("timeout_count", 0) or 0) == 0
                    and int(row.get("blocked_count", 0) or 0) == 0
                    for row in applicable_rows)):
                healthy += 1
        generated = clean_text(payload.get("generated_at"))
        try:
            generated_label = datetime.fromisoformat(
                generated.replace("Z", "+00:00")
            ).astimezone(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
        except (TypeError, ValueError):
            generated_label = "Jamais exécuté"
        duration = human_duration(float(payload.get("scan_duration_s", 0) or 0))
        new_count = int(payload.get("new_offer_count", 0) or 0)
        window = int(payload.get("window_hours", PROFILES[profile_id].window_hours))
        return f"""
  <article class="profile-card" data-profile="{html.escape(profile_id, quote=True)}">
    <div class="profile-card-head"><div><p class="eyebrow">{html.escape(label)}</p><h2>{window} h glissantes</h2></div></div>
    <p class="profile-card-meta">Dernier run : {html.escape(generated_label)}</p>
    <div class="profile-card-stats">
      <div class="profile-stat"><span>Durée</span><strong>{html.escape(duration)}</strong></div>
      <div class="profile-stat"><span>Nouvelles</span><strong>{new_count}</strong></div>
      <div class="profile-stat"><span>Sources opérationnelles</span><strong>{healthy}/{len(PROFILES[profile_id].sources)}</strong></div>
    </div>
  </article>"""

    profile_cards = (
        profile_card("fast_data_ia", "Dernier FAST Data/IA")
        + profile_card("full_general", "Dernier FULL général")
    )
    dashboard_config_json = json.dumps({
        "highlightLabel": str(config["highlight_label"]),
        "domainLabels": {
            domain_id: domains[domain_id]["label"] for domain_id in active_ids
        },
        "regionLabels": {
            region_id: regions[region_id]["label"] for region_id in active_region_ids
        },
        "defaultRegion": default_region_id,
    }, ensure_ascii=False).replace("</", "<\\/")

    html_page = f"""<!DOCTYPE html>
<html lang="fr" translate="no"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="google" content="notranslate">
<meta name="radar-generated-at" content="{html.escape(generated_at_iso, quote=True)}">
<meta name="theme-color" content="#07111f">
<meta http-equiv="Content-Security-Policy" content="default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; base-uri 'self'; form-action 'none'; object-src 'none'">
<title>{app_name} — Dashboard</title>
<link rel="stylesheet" href="assets/dashboard.css">
</head>
<body>
<div class="shell">
<header class="topbar">
  <a class="brand" href="#top" aria-label="Accueil {app_name}">
    <span class="brand-mark" aria-hidden="true">◎</span>
    <span>{app_name}</span>
  </a>
  <div class="automation-status"><span class="live-dot" aria-hidden="true"></span><span>Automatisation GitHub Actions active</span></div>
</header>
<main id="top">
<section class="hero" aria-labelledby="pageTitle">
  <div>
    <p class="eyebrow">Radar de stages multi-sources</p>
    <h1 id="pageTitle">Trouvez les offres qui méritent votre candidature.</h1>
    <p class="hero-copy">{tagline}. Les résultats récents sont consolidés, dédupliqués et classés automatiquement.</p>
    <div class="scan-meta">
      <span>Dernier scan <strong>{now}</strong></span>
      <span>Fenêtres <strong>FAST 2 h · FULL 24 h</strong></span>
      <span>Tri <strong>plus récent d’abord</strong></span>
    </div>
  </div>
  <div class="hero-actions">
    <button id="refreshBtn" class="button button-secondary" type="button">
      <span class="button-icon" aria-hidden="true">↺</span><span>Actualiser les offres</span>
    </button>
  </div>
</section>

<section class="metrics" aria-label="Indicateurs du dernier scan">
  <article class="metric"><span class="metric-label">Offres actives</span><strong class="metric-value">{len(offers)}</strong><span class="metric-note">hors offres probablement pourvues</span></article>
  <article class="metric"><span class="metric-label">Nouvelles</span><strong class="metric-value">{new_offer_count}</strong><span class="metric-note">depuis le scan précédent</span></article>
  <article class="metric"><span class="metric-label">Marchés</span><strong class="metric-value">{len(active_region_ids)}</strong><span class="metric-note">régions analysées</span></article>
  <article class="metric"><span class="metric-label">Durée du scan</span><strong class="metric-value">{human_duration(scan_duration_s)}</strong><span class="metric-note">collecte et classement</span></article>
</section>

<section class="profile-summary" aria-label="État des profils de scan">{profile_cards}</section>

<div class="catalog-summary">
  <span class="catalog-icon" aria-hidden="true">⌁</span>
  <span><strong>{len(active_ids)} domaines</strong> et <strong>{len(active_region_ids)} régions</strong> analysés : {html.escape(' · '.join(active_region_labels))}</span>
</div>

<details class="source-health">
  <summary>
    <span class="source-health-title"><span class="live-dot" aria-hidden="true"></span>État des connecteurs</span>
    <span class="source-health-copy">{successful_statuses} opérationnels · {degraded_statuses} bloqués ou en erreur · afficher le détail</span>
  </summary>
  <div class="sources-panel" translate="no">{status_rows}</div>
</details>

<section class="workspace" aria-labelledby="offersTitle">
<div class="workspace-head">
  <div><p class="eyebrow">Candidatures</p><h2 id="offersTitle" class="section-title">Explorer les offres</h2></div>
  <div class="workspace-tools"><label class="sort-control" for="sortSelect"><span>Tri</span>
    <select id="sortSelect"><option value="date" selected>Plus récent d’abord</option><option value="relevance">Par pertinence</option></select>
  </label><label class="search-box" role="search"><span aria-hidden="true">⌕</span>
    <input id="searchInput" type="search" placeholder="Poste, entreprise ou ville…" autocomplete="off" aria-label="Rechercher une offre">
  </label></div>
</div>
<div class="filter-panel" aria-label="Filtres des offres">
  <div class="filter-group"><span class="filter-label">Statut</span>
    <button class="fchip status-chip on" data-status="todo" aria-pressed="true">À postuler <b id="todoCount"></b></button>
    <button class="fchip status-chip" data-status="applied" aria-pressed="false">Déjà postulé <b id="appliedCount"></b></button>
    <button class="fchip status-chip" data-status="all" aria-pressed="false">Tous</button>
  </div>
  <div class="filter-group" translate="no"><span class="filter-label">Source</span>
    <button class="fchip source-chip on" data-src="*" aria-pressed="true">Toutes</button>{filter_btns}
  </div>
  <div class="filter-group{single_domain_class}" translate="no"><span class="filter-label">Domaine</span>
    <button class="fchip domain-chip on" data-domain="*" aria-pressed="true">Tous</button>{domain_chips}
  </div>
  <div class="filter-group{single_region_class}" translate="no"><span class="filter-label">Région</span>
    <button class="fchip region-chip" data-region="*" aria-pressed="false">Toutes</button>{region_chips}
  </div>
  <div class="filter-group" translate="no"><span class="filter-label">Options</span>
    <button id="highlightFilter" class="fchip" aria-pressed="false">✓ {highlight_label}</button>
  </div>
  <div class="filter-group active-row"><span class="filter-label">Actifs</span>
    <span id="activeFilters" class="active-filters" translate="no"></span>
    <button id="resetBtn" class="fchip reset-btn" disabled>✕ Réinitialiser les filtres</button>
  </div>
</div>
<div class="list-head"><h3 class="list-title">Offres recommandées</h3><span id="visibleCount" class="visible-count" aria-live="polite"></span></div>
<p id="emptyState" class="empty" hidden>Aucune offre ne correspond à ces filtres.</p>
<div id="jobs">{"".join(cards)}</div>
<details class="expired-section" {'hidden' if not expired_cards else ''}>
  <summary>Offres probablement pourvues <span id="expiredVisibleCount">({len(expired_cards)})</span></summary>
  <p id="expiredEmptyState" class="empty" hidden>Aucune offre probablement pourvue ne correspond à ces filtres.</p>
  <div id="expiredJobs">{"".join(expired_cards)}</div>
</details>
</section>
<p class="footer-note">Le statut de candidature reste uniquement dans ce navigateur et n’est jamais envoyé au dépôt.</p>
</main>
</div>
<script id="radarConfig" type="application/json">{dashboard_config_json}</script>
<script src="assets/dashboard.js" defer></script>
</body></html>"""
    # Les badges conditionnels peuvent laisser des espaces en fin de ligne ;
    # normaliser la sortie garde le fichier généré propre et diffable.
    html_page = "\n".join(line.rstrip() for line in html_page.splitlines()) + "\n"
    atomic_write_text(target, html_page)


# ============================================================
# MAIN
# ============================================================

def build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Collecte les stages selon un profil FAST ou FULL."
    )
    parser.add_argument("--profile", choices=tuple(PROFILES), default=None)
    parser.add_argument("--domains", help="Surcharge CSV de RADAR_DOMAINS")
    parser.add_argument("--regions", help="Surcharge CSV de RADAR_REGIONS")
    parser.add_argument("--sources", help="Surcharge CSV de RADAR_SOURCES")
    parser.add_argument("--window-hours", type=int,
                        help="Surcharge de RADAR_WINDOW_HOURS")
    parser.add_argument("--benchmark", action="store_true",
                        help="Collecte et mesure sans aucune mutation de production")
    parser.add_argument("--benchmark-output", type=Path,
                        help="Rapport distinct du benchmark (JSON)")
    return parser


def benchmark_output_is_safe(path: Path) -> bool:
    production_paths = {
        STATUS_FILE, METRICS_FILE, XRAY_STATE_FILE, OFFERS_FILE, SEEN_FILE,
        DOCS_INDEX, *PROFILE_STATUS_FILES.values(),
    }
    resolved = path.resolve()
    return all(resolved != production.resolve() for production in production_paths)


def main(argv: list[str] | None = None) -> int:
    args = build_cli_parser().parse_args(argv)
    if args.benchmark_output and not args.benchmark:
        raise SystemExit("--benchmark-output exige --benchmark")
    if args.benchmark_output and not benchmark_output_is_safe(args.benchmark_output):
        raise SystemExit("Le rapport benchmark doit être distinct des fichiers de production")

    profile = resolve_profile(
        PROFILES,
        profile_id=args.profile,
        cli_domains=args.domains,
        cli_regions=args.regions,
        cli_sources=args.sources,
        cli_window_hours=args.window_hours,
        domains=DOMAINS,
        regions=REGIONS,
        sources=SOURCE_NAMES,
    )
    context = ScanContext(profile, datetime.now(timezone.utc))
    active_ids = list(profile.domains)
    active_region_ids = list(profile.regions)
    budget = calculate_scan_budget(
        len(active_ids), len(active_region_ids), profile
    )
    run_metrics = RunMetrics()
    total_started = time.perf_counter()
    production_store = load_offer_store(run_at=context.run_at)
    xray_catalog = load_xray_state_catalog()
    xray_state = xray_state_for_profile(xray_catalog, profile.id)
    xray_state_sink: dict = {}
    print(f"Profil : {profile.id} · fenêtre {profile.window_hours} h")
    print("Domaines actifs : " + ", ".join(
        f"{domain_id} ({DOMAINS[domain_id]['label']})" for domain_id in active_ids
    ))
    print("Régions actives : " + ", ".join(
        f"{region_id} ({REGIONS[region_id]['label']})" for region_id in active_region_ids
    ))
    print("Sources actives : " + ", ".join(profile.sources))
    print(f"Requêtes actives dédupliquées : {len(active_queries(active_ids=active_ids))}")
    if budget.is_high:
        print(
            f"[WARN] budget de scan élevé : {budget.domain_count} domaines × "
            f"{budget.region_count} régions = {budget.combination_count} combinaisons — "
            f"durée estimée {human_duration(budget.estimated_duration_s)} ; "
            f"limites du profil {budget.results_wanted}",
            file=sys.stderr,
        )
    started = time.perf_counter()
    all_offers, statuses = run_all_sources(
        active_domain_ids=active_ids,
        active_region_ids=active_region_ids,
        budget=budget,
        context=context,
        metrics=run_metrics,
        store=production_store,
        xray_rotation_state=xray_state,
        xray_state_sink=xray_state_sink,
    )
    dedup_started = time.perf_counter()
    all_offers = align_offers_with_store(all_offers, production_store)
    run_metrics.deduplication_s += time.perf_counter() - dedup_started
    scan_duration = time.perf_counter() - started
    run_metrics.collection_s = scan_duration

    seen_document = load_seen_document(run_at=context.run_at)
    next_seen_document, new_ids = radar_store.update_seen_memory(
        seen_document,
        all_offers.keys(),
        run_at=context.run_at,
        profile_id=profile.id,
        retention_days=SEEN_RETENTION_DAYS,
    )
    new_offers = [offer for offer_id_value, offer in all_offers.items()
                  if offer_id_value in new_ids]

    if args.benchmark:
        run_metrics.total_s = time.perf_counter() - total_started
        report = build_metrics_record(
            context,
            statuses,
            run_metrics,
            new_offer_count=0,
            offer_count=len(all_offers),
            store_size_bytes=(OFFERS_FILE.stat().st_size if OFFERS_FILE.exists() else 0),
            seen_size_bytes=(SEEN_FILE.stat().st_size if SEEN_FILE.exists() else 0),
            benchmark=True,
        )
        report["production_mutated"] = False
        report["new_offers_suppressed"] = len(new_ids)
        if args.benchmark_output:
            atomic_write_json(args.benchmark_output, report)
            print(f"Rapport benchmark : {args.benchmark_output}")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    store_started = time.perf_counter()
    offer_store = reconcile_offer_store(
        list(all_offers.values()), statuses,
        store=production_store,
        run_at=context.run_at,
        context=context,
    )
    dashboard_offers, expired_offers = dashboard_offer_sections_from_store(
        offer_store
    )
    run_metrics.store_s += time.perf_counter() - store_started

    final_counts = {name: 0 for name in SOURCE_NAMES}
    for o in all_offers.values():
        final_counts[o["source"]] = final_counts.get(o["source"], 0) + 1

    domain_counts = {
        domain_id: sum(domain_id in o.get("domains", []) for o in all_offers.values())
        for domain_id in active_ids
    }
    region_counts = {
        region_id: sum(region_id in o.get("regions", []) for o in all_offers.values())
        for region_id in active_region_ids
    }

    print(f"\nTotal : {len(all_offers)} offres · Nouvelles : {len(new_offers)} · "
          f"Durée : {human_duration(scan_duration)}")
    print(f"Dashboard persistant : {len(dashboard_offers)} offres actives · "
          f"store={len(offer_store)} entrées")
    for st in statuses:
        reasons = ", ".join(
            f"{k} ×{v}" for k, v in sorted(st.reject_reasons.items(), key=lambda kv: -kv[1])
        ) or "—"
        extra = f" · {st.note}" if st.note else ""
        http = f" · {st.http_error}" if st.http_error else ""
        print(f"  - {st.source} · {st.region_label}: {status_label(st, st.accepted_count)} "
              f"[{st.status}] bruts={st.raw_count} acceptés={st.accepted_count} "
              f"rejetés={st.rejected_count} trop_anciens={st.too_old_count} "
              f"doublons={st.duplicate_count} pages={st.pages_fetched} "
              f"dates_inconnues={st.unknown_date_count} "
              f"durée={st.duration_s}s · rejets : {reasons}{extra}{http}")
    for domain_id, count in domain_counts.items():
        print(f"  - domaine {domain_id}: {count} offres")
    for region_id, count in region_counts.items():
        print(f"  - région {region_id}: {count} offres")

    save_status_json(
        statuses,
        scan_duration,
        active_ids,
        active_region_ids,
        budget,
        context=context,
        new_offer_count=len(new_offers),
    )

    dashboard_url = os.environ.get("DASHBOARD_URL", "")
    if new_offers:
        if os.environ.get("SMTP_USER") and os.environ.get("SMTP_PASS"):
            try:
                send_email(new_offers, dashboard_url, DOMAINS, APP_CONFIG, REGIONS)
                print("Email envoyé.")
            except Exception as e:
                print(f"[ERREUR] email : {e}", file=sys.stderr)
        else:
            print("[INFO] SMTP non configuré (SMTP_USER/SMTP_PASS absents) — pas d'email envoyé.")

    store_write_started = time.perf_counter()
    save_offer_store(offer_store)
    save_seen_document(next_seen_document)
    next_xray_state = xray_state_sink.get("state", xray_state)
    atomic_write_json(
        XRAY_STATE_FILE,
        update_xray_state_catalog(xray_catalog, profile.id, next_xray_state),
    )
    run_metrics.store_s += time.perf_counter() - store_write_started

    dashboard_started = time.perf_counter()
    # Le dashboard reste général même lorsque le dernier run est FAST.
    render_dashboard(
        dashboard_offers,
        new_ids,
        statuses,
        scan_duration,
        DOMAINS,
        APP_CONFIG,
        list(PROFILES[DEFAULT_PROFILE_ID].domains),
        REGIONS,
        list(PROFILES[DEFAULT_PROFILE_ID].regions),
        expired_offers=expired_offers,
        profile_status_payloads=load_profile_status_payloads(),
        context=context,
    )
    run_metrics.dashboard_s = time.perf_counter() - dashboard_started
    run_metrics.total_s = time.perf_counter() - total_started
    metrics_record = build_metrics_record(
        context,
        statuses,
        run_metrics,
        new_offer_count=len(new_offers),
        offer_count=len(all_offers),
        store_size_bytes=OFFERS_FILE.stat().st_size,
        seen_size_bytes=SEEN_FILE.stat().st_size,
    )
    save_metrics_record(metrics_record)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
