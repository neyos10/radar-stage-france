#!/usr/bin/env python3
"""
Radar Stage France — agrégateur multi-sources, multi-domaines et multi-régions.

Usage actuel (branche perso-data-france-maroc) : domaine data_ia seul, régions
France et Maroc. Les autres domaines/régions restent dans les catalogues JSON
avec ``enabled: false`` ; la version multi-domaine est le tag v-multi-domaine.

Sources :
  1. LinkedIn (offres publiques, sans compte)   — via JobSpy
  2. Indeed (régions configurées)                — via JobSpy
  3. APEC France (recherche publique de stages)  — sans compte
  4. Adzuna France (agrégateur officiel)         — si secret fourni
  5. The Muse (API officielle, filtre stage)     — clé optionnelle
  6. France Travail (API officielle française)   — si secrets fournis
  7. LinkedIn Posts (X-ray via moteur public)    — jamais LinkedIn direct

Glassdoor, Google Jobs et Jooble ont été retirés du code (pas seulement
désactivés) : sur plusieurs runs de production consécutifs, ils ont produit
0 offre acceptée dans 100 % des cas (Glassdoor bloqué en 403 par son
anti-bot, Google Jobs répondant vide sans erreur, Jooble sans aucun filtre
de type de contrat — 137/139 de ses résultats n'étaient réellement pas des
stages) tout en consommant du temps de scan. Voir l'historique Git si l'un
d'eux redevient exploitable un jour.

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
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import parsedate_to_datetime
from functools import lru_cache
from pathlib import Path

import requests

from radar_xray import (
    DuckDuckGoHtmlProvider,
    DuckDuckGoLiteProvider,
    ExaProvider,
    FallbackXRayProvider,
    LINKEDIN_POST_HOSTS,
    TavilyProvider,
    XRayBlockedError,
    XRayProviderError,
    XRayTimeoutError,
    is_relevant_internship_post,
)

# ============================================================
# CONFIG
# ============================================================

CONFIG_FILE = Path("data/config.json")
DOMAINS_FILE = Path("data/domains.json")
REGIONS_FILE = Path("data/regions.json")
SEEN_FILE = Path("data/seen_offers.json")
STATUS_FILE = Path("data/source_status.json")
OFFERS_FILE = Path("data/offers.json")
DOCS_INDEX = Path("docs/index.html")
LINKEDIN_POSTS_ROTATION_FILE = Path("data/linkedin_posts_rotation.json")


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
        # Optionnels : négatifs pénalisés dans le titre seulement (« recrutement »
        # apparaît dans presque toutes les descriptions) et expressions qui
        # neutralisent un négatif (« marketing analytics » ≠ « marketing »).
        for field_name in ("negative_title_only", "negative_exceptions"):
            values = domain.get(field_name, [])
            if not isinstance(values, list) or not all(
                    isinstance(value, str) and value.strip() for value in values):
                raise ValueError(f"Domaine {domain_id}: {field_name} invalide")
        min_hits = domain.get("description_only_min_positives", 1)
        if isinstance(min_hits, bool) or not isinstance(min_hits, int) or min_hits < 1:
            raise ValueError(
                f"Domaine {domain_id}: description_only_min_positives doit être un entier positif")
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
    highlight_regions = config.get("highlight_regions")
    if highlight_regions is not None and (
            not isinstance(highlight_regions, list)
            or not all(isinstance(value, str) for value in highlight_regions)):
        raise ValueError("highlight_regions doit être une liste d'identifiants de région")
    disabled = config.get("disabled_sources", {})
    if not isinstance(disabled, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in disabled.items()):
        raise ValueError("disabled_sources doit associer un nom de source à une raison")
    return config


def load_regions(path: Path = REGIONS_FILE) -> dict:
    """Catalogue géographique et paramètres natifs de chaque connecteur."""
    regions = load_json(path)
    required = {
        "label", "linkedin_location", "country_indeed",
        "apec_available", "france_travail_available", "enabled",
    }
    for region_id, region in regions.items():
        missing = required - set(region)
        if missing:
            raise ValueError(f"Région {region_id}: champs manquants {sorted(missing)}")
        for field_name in ("label", "linkedin_location", "country_indeed"):
            if not isinstance(region[field_name], str) or not region[field_name].strip():
                raise ValueError(f"Région {region_id}: {field_name} invalide")
        for field_name in ("apec_available", "france_travail_available", "enabled"):
            if not isinstance(region[field_name], bool):
                raise ValueError(f"Région {region_id}: {field_name} doit être booléen")
        extra = region.get("linkedin_extra_queries", [])
        if not isinstance(extra, list) or not all(
                isinstance(value, str) and value.strip() for value in extra):
            raise ValueError(f"Région {region_id}: linkedin_extra_queries invalide")
        local = region.get("local_sources", [])
        if not isinstance(local, list) or not all(isinstance(value, str) for value in local):
            raise ValueError(f"Région {region_id}: local_sources invalide")
    if "france" not in regions:
        raise ValueError("Le catalogue des régions doit contenir 'france'")
    return regions


APP_CONFIG = load_config()
DOMAINS = load_domains()
REGIONS = load_regions()
DEFAULT_REGION_ID = "france"

SOURCE_NAMES = [
    "LinkedIn", "Indeed", "APEC", "Adzuna", "The Muse",
    "France Travail", "LinkedIn Posts", "Dreamjob", "Sites Maroc",
]


def active_source_names(config: dict | None = None) -> list[str]:
    """Sources interrogées et affichées. ``disabled_sources`` (config) retire
    une source sans supprimer son code : ni appel réseau, ni puce au dashboard.
    """
    config = config or APP_CONFIG
    disabled = config.get("disabled_sources", {})
    return [name for name in SOURCE_NAMES if name not in disabled]

# Mots qui doivent apparaître dans le titre pour garder l'offre
# (filtre anti-bruit : les scrapers remontent parfois des CDI)
TITLE_MUST_MATCH = re.compile(
    r"\b(stages?|stagiaires?|interns?|internships?|pfa|pfe"
    r"|projet\s+de\s+fin\s+d['’]?\s*[ée]tudes?)\b",
    re.I,
)

# Fenêtre métier de collecte : exactement 24 heures glissantes en UTC. La
# configuration en jours reste utilisée par l'interface et la rétention ; elle
# n'intervient plus dans la sélection des résultats du scan courant.
MAX_OFFER_AGE_DAYS = int(APP_CONFIG["days_window"])
OFFER_WINDOW_HOURS = 24

# Calibration multi-label. Le seuil 2 conserve la dominance du titre (+3)
# face à un seul signal négatif de description (-1) et permet aux intitulés
# composés explicitement catalogués de survivre à un voisin métier (-4).
TITLE_POSITIVE_WEIGHT = 3
DESCRIPTION_POSITIVE_WEIGHT = 1
TITLE_NEGATIVE_WEIGHT = -4
DESCRIPTION_NEGATIVE_WEIGHT = -1
DOMAIN_SCORE_THRESHOLD = 2
# Sans aucun mot positif dans le titre, combien de mots positifs distincts la
# description seule doit citer. Défaut historique : 2 (= le seuil). Un domaine
# peut le relever via ``description_only_min_positives`` : data_ia exige 3,
# car « SQL + Power BI » dans une offre d'assistant ne doit pas suffire depuis
# que ces outils génériques sont des signaux positifs.
DESCRIPTION_ONLY_MIN_POSITIVES = 2

# Réseau / robustesse
REQUEST_TIMEOUT = 20          # secondes, pour tous les appels requests
MAX_CONSECUTIVE_BLOCKS = 2    # 403/429 consécutifs avant d'arrêter la source (run courant)
MAX_PARALLEL_SOURCES = 4      # parallélisme borné entre sources indépendantes
APEC_DETAIL_MAX_CALLS = 1000  # plafond technique, jamais un quota métier
MAX_LINKEDIN_QUERIES = 1000   # plafond anti-boucle ; couvre le catalogue entier
# Descriptions LinkedIn de la seule requête prioritaire. Mesuré le 25/11/2026 :
# une requête coûte ~50 s sans description et ~145 s avec (une page par
# offre) ; 3 requêtes enrichies portaient le run France+Maroc à 14 min 35.
MAX_LINKEDIN_DESCRIPTION_QUERIES = 1

# Plafonds purement techniques de JobSpy. Ils déclenchent sa pagination interne
# jusqu'à la fin réelle des résultats ; LinkedIn plafonne lui-même à 1000. Ces
# valeurs ne sont jamais réduites par le nombre de domaines ou de régions.
JOBSPY_RESULTS_WANTED = {"linkedin": 1000, "indeed": 1000}
JOBSPY_PAGE_SIZES = {"linkedin": 25, "indeed": 100}

# Le garde-fou combinatoire reste uniquement informatif : il ne réduit plus le
# volume. Les limites effectives sont celles des connecteurs et les plafonds de
# pagination élevés ci-dessus.
SCAN_COMBINATION_BUDGET = 6
SCAN_FIXED_ESTIMATE_S = 45
SCAN_SECONDS_PER_EFFECTIVE_COMBINATION = 35

# Pagination explicite des API non-JobSpy. Arrêt à la fin réelle, ou après deux
# pages successives sans nouveau résultat ; MAX_SOURCE_PAGES est la seule
# protection contre une boucle distante défectueuse.
MAX_SOURCE_PAGES = 100
MAX_STALE_PAGES = 2
APEC_PAGE_SIZE = 100
ADZUNA_PAGE_SIZE = 50
FRANCE_TRAVAIL_PAGE_SIZE = 150
# LinkedIn Posts (X-ray) : un moteur de recherche public, jamais LinkedIn
# directement. Budget calibré sur le quota gratuit Tavily (1000 crédits/mois,
# 1 crédit par recherche) : 2 créneaux/jour × 4 requêtes × régions actives
# (2 : France + Maroc) = 16 crédits/jour ≈ 480/mois ; le plafond de 4 régions
# resterait sous 1000/mois. Hors créneau, la source est simplement
# différée — jamais un crédit dépensé. Pagination limitée : ni Tavily ni
# Exa ne paginent au-delà d'une page.
LINKEDIN_POSTS_QUERY_BUDGET = 4
LINKEDIN_POSTS_MAX_PAGES = 1
LINKEDIN_POSTS_RUN_HOURS_UTC = (6, 18)

# Une source à couverture rotative n'interroge qu'un sous-ensemble du
# catalogue par run : l'absence d'une offre au run suivant ne prouve jamais
# sa disparition (la requête qui l'avait trouvée n'a pas été rejouée). Ces
# sources ne confirment donc jamais une absence ; leurs offres expirent par
# la fenêtre de fraîcheur, pas par non-réobservation.
ROTATING_COVERAGE_SOURCES = {"LinkedIn Posts", "Sites Maroc"}

# Une offre confirmée absente reste archivée brièvement avant purge. Cette
# marge protège l'historique pendant un blocage prolongé tout en gardant le
# dashboard limité à ``days_window``.
OFFER_RETENTION_GRACE_DAYS = 7
OFFER_RETENTION_DAYS = MAX_OFFER_AGE_DAYS + OFFER_RETENTION_GRACE_DAYS


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


def calculate_scan_budget(domain_count: int, region_count: int) -> ScanBudget:
    """Calcule un budget déterministe, testable sans aucun appel réseau."""
    if domain_count <= 0 or region_count <= 0:
        raise ValueError("Le budget exige au moins un domaine et une région")
    combinations = domain_count * region_count
    scale = 1.0
    wanted = dict(JOBSPY_RESULTS_WANTED)
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
REASON_UNVERIFIABLE_RECENT_DATE = "date non vérifiable sur les dernières 24 heures"

# ============================================================
# STATUT PAR SOURCE
# ============================================================

@dataclass
class SourceStatus:
    """Diagnostic d'une source pour un run.

    status : success | empty | blocked | error | disabled_missing_credentials |
             not_applicable_region | deferred
    """
    source: str
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
    duration_s: float = 0.0
    http_error: str = ""
    note: str = ""
    # Rendement par requête (JobSpy) : {requête: {"raw": n, "accepted": m}}.
    # Sert à mesurer puis élaguer les requêtes qui n'apportent rien.
    query_yield: dict = field(default_factory=dict)

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
                           "not_applicable_region", "deferred"):
            return
        self.status = "success" if self.accepted_count else "empty"


BLOCK_CODE_RE = re.compile(r"\b(403|429)\b")


# Les exceptions requests incluent l'URL complète, donc les identifiants
# passés en paramètres (Adzuna app_id/app_key, The Muse api_key). Sans ce
# masquage, une erreur 503 Adzuna a écrit la clé en clair dans
# data/source_status.json, fichier commité à chaque run.
SECRET_PARAM_RE = re.compile(
    r"((?:app_id|app_key|api_key|apikey|key|token|access_token|client_id|client_secret)=)[^&\s'\"]+",
    re.I,
)


def safe_error(exc: Exception | str) -> str:
    """Texte d'erreur sans aucun identifiant passé en paramètre d'URL."""
    return SECRET_PARAM_RE.sub(r"\1***", str(exc))


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
    if st.status == "deferred":
        return f"Différé — {st.note or 'prochain créneau planifié'}"
    if st.status == "blocked":
        detail = st.note or st.http_error or "HTTP 403/429"
        return f"Bloqué temporairement — {detail[:160]}"
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
    if st.status == "blocked":
        code = st.http_error.replace("HTTP ", "") if st.http_error else "403/429"
        return f"bloqué {code}"
    if st.status == "deferred":
        return "différé"
    if st.status == "error":
        return "erreur"
    if st.raw_count:
        return f"0 accepté / {st.raw_count} reçus"
    return "(0)"


def register_offer(status: SourceStatus, offers: dict,
                   offer: dict | None, reason: str | None) -> None:
    """Comptabilise une offre évaluée : acceptée, rejetée ou doublon."""
    if offer is None:
        status.add_reject(reason)
    elif offer["id"] in offers:
        merge_offer_metadata(offers[offer["id"]], offer)
        status.add_reject(REASON_DUPLICATE)
    else:
        offers[offer["id"]] = offer
        status.accepted_count += 1
        if not offer.get("date_known", bool(offer.get("date"))):
            status.unknown_date_count += 1


def log_collection_summary(status: SourceStatus) -> None:
    """Bilan homogène demandé pour chaque source et chaque région."""
    print(
        f"[COLLECTE] {status.source} · {status.region_label} : "
        f"récupérées={status.raw_count} "
        f"conservées_24h={status.accepted_count} "
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
                  regions: dict | None = None, **kwargs) -> SourceStatus:
    regions = regions or REGIONS
    region = regions[region_id]
    return SourceStatus(source=source, region_id=region_id,
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
                     active_ids: list[str] | None = None) -> list[str]:
    """Allocation pondérée et bornée des requêtes LinkedIn.

    Si le budget couvre tous les domaines, chacun reçoit d'abord une requête.
    Les créneaux restants sont distribués par tours : un domaine reçoit autant
    de requêtes consécutives que sa priorité. Quand le budget est inférieur au
    nombre de domaines, les priorités les plus hautes passent d'abord ; l'ordre
    du catalogue départage les priorités égales.
    """
    selected = active_domains(domains, active_ids)
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
    if MAX_LINKEDIN_QUERIES >= len(selected):
        for domain_id in selected:
            take(domain_id)
            if len(plan) >= MAX_LINKEDIN_QUERIES:
                return plan

    while len(plan) < MAX_LINKEDIN_QUERIES:
        added = False
        for domain_id in ordered_ids:
            for _ in range(priorities[domain_id]):
                if len(plan) >= MAX_LINKEDIN_QUERIES:
                    break
                added = take(domain_id) or added
        if not added:
            break
    return plan


def source_queries(site: str, domains: dict | None = None,
                   active_ids: list[str] | None = None) -> list[str]:
    return (linkedin_queries(domains, active_ids) if site == "linkedin"
            else active_queries(domains, active_ids))


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


def _strip_exceptions(text: str, exceptions: list[str]) -> str:
    """Retire les expressions qui neutralisent un négatif avant son test."""
    for phrase in exceptions:
        text = keyword_pattern(phrase).sub(" ", normalize_for_match(text))
    return text


def _domain_signals(title: str, description: str, domain: dict) -> tuple[int, int, int]:
    """(score, positifs dans le titre, positifs dans la description)."""
    positive = domain["positive"]
    negative = domain["negative"]
    title_only = domain.get("negative_title_only", [])
    exceptions = domain.get("negative_exceptions", [])
    neg_title = _strip_exceptions(title, exceptions)
    neg_description = _strip_exceptions(description, exceptions)
    title_hits = sum(keyword_matches(title, word) for word in positive)
    description_hits = sum(keyword_matches(description, word) for word in positive)
    score = (
        TITLE_POSITIVE_WEIGHT * title_hits
        + DESCRIPTION_POSITIVE_WEIGHT * description_hits
        + TITLE_NEGATIVE_WEIGHT
        * sum(keyword_matches(neg_title, word) for word in [*negative, *title_only])
        + DESCRIPTION_NEGATIVE_WEIGHT
        * sum(keyword_matches(neg_description, word) for word in negative)
    )
    return score, title_hits, description_hits


def score_domain(title: str, description: str, domain: dict) -> int:
    """Score déterministe ; chaque mot-clé compte au plus une fois par zone."""
    return _domain_signals(title, description, domain)[0]


def apply_scoring_overrides(domain: dict, overrides: dict | None) -> dict:
    """Ajuste un domaine pour une source au format particulier.

    ``extra_positive`` ajoute des positifs, ``ignore_negative`` retire des
    négatifs (titre et titre seul), ``description_only_min_positives``
    remplace le minimum de la description seule. Le catalogue n'est jamais
    modifié.
    """
    if not overrides:
        return domain
    ignored = {value.casefold() for value in overrides.get("ignore_negative", [])}
    adjusted = dict(domain)
    adjusted["positive"] = list(dict.fromkeys(
        domain["positive"] + list(overrides.get("extra_positive", []))))
    for field_name in ("negative", "negative_title_only"):
        adjusted[field_name] = [value for value in domain.get(field_name, [])
                                if value.casefold() not in ignored]
    if "description_only_min_positives" in overrides:
        adjusted["description_only_min_positives"] = overrides["description_only_min_positives"]
    return adjusted


def score_offer_domains(title: str, description: str,
                        domains: dict | None = None,
                        active_ids: list[str] | None = None,
                        overrides: dict | None = None) -> tuple[list[str], dict[str, int]]:
    selected = active_domains(domains, active_ids)
    scores: dict[str, int] = {}
    matched: list[str] = []
    for domain_id, domain in selected.items():
        domain = apply_scoring_overrides(domain, overrides)
        score, title_hits, description_hits = _domain_signals(title, description, domain)
        scores[domain_id] = score
        min_description_hits = domain.get(
            "description_only_min_positives", DESCRIPTION_ONLY_MIN_POSITIVES)
        if score >= DOMAIN_SCORE_THRESHOLD and (
                title_hits or description_hits >= min_description_hits):
            matched.append(domain_id)
    return matched, scores


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


def merge_offer_domains(existing: dict, incoming: dict) -> None:
    """Alias rétrocompatible pour les intégrations qui appellent ce helper."""
    merge_offer_metadata(existing, incoming)

def offer_id(url: str, title: str, company: str) -> str:
    base = (url or "") + "|" + (title or "").lower() + "|" + (company or "").lower()
    return hashlib.sha1(base.encode()).hexdigest()[:16]


def is_highlighted_company(company: str, config: dict | None = None) -> bool:
    """Mot entier, insensible aux accents : « octo » ne matche plus « Octopus »."""
    config = config or APP_CONFIG
    return any(keyword_matches(company or "", keyword)
               for keyword in config["highlight_companies"])


def shows_highlight(offer: dict, config: dict | None = None) -> bool:
    """Badge entreprise limité aux régions où il a un sens.

    « Visa-friendly probable » ne concerne que les offres en France : une
    filiale marocaine du même groupe (Capgemini Maroc…) ne le porte pas.
    Sans ``highlight_regions`` dans la config, toutes les régions l'affichent.
    """
    config = config or APP_CONFIG
    if not offer.get("highlighted"):
        return False
    allowed = config.get("highlight_regions")
    if allowed is None:
        return True
    return any(region_id in allowed for region_id in offer.get("regions", []))


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
        try:
            parsed = datetime.strptime(raw[:10], "%Y-%m-%d")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _log_old_date_rejection(source: str, title: str, raw_date, parsed: datetime) -> None:
    """Trace vérifiable demandée pour chaque rejet de date ancienne."""
    raw = re.sub(r"[\r\n\t]+", " ", str(raw_date))[:100]
    safe_title = re.sub(r"[\r\n\t]+", " ", title)[:100]
    print(
        f"[REJET_DATE] source={source} titre={safe_title!r} "
        f"date_brute={raw!r} date_parsee={parsed.isoformat()} raison={REASON_TOO_OLD}",
        file=sys.stderr,
    )


def make_offer(title, company, location, url, source, date="",
               confirmed_internship=False, description="", domains: dict | None = None,
               active_ids: list[str] | None = None,
               region_id: str = DEFAULT_REGION_ID,
               regions: dict | None = None,
               now: datetime | None = None,
               freshness_verified: bool = False,
               reject_missing_date: bool = False,
               max_age_hours: int = OFFER_WINDOW_HOURS,
               scoring: dict | None = None) -> tuple[dict | None, str | None]:
    """Normalise une offre. Retourne (offre, None) ou (None, raison_du_rejet)."""
    regions = regions or REGIONS
    if region_id not in regions:
        raise ValueError(f"Région inconnue pour l'offre: {region_id}")
    title = clean_text(title)
    if not title:
        return None, REASON_NO_TITLE
    if not confirmed_internship and not TITLE_MUST_MATCH.search(title):
        return None, REASON_NOT_INTERNSHIP
    description = clean_text(description)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    published_at = parse_offer_date(date, now)
    if (published_at and not freshness_verified
            and published_at < now - timedelta(hours=max_age_hours)):
        _log_old_date_rejection(source, title, date, published_at)
        if max_age_hours != OFFER_WINDOW_HOURS:
            return None, f"plus de {max_age_hours // 24} jours"
        return None, REASON_TOO_OLD
    if published_at is None and reject_missing_date:
        # Une source X-ray n'a que des indices approximatifs de date : sans
        # preuve vérifiable de fraîcheur, l'offre est rejetée plutôt que
        # conservée par précaution (contrairement au comportement par défaut).
        return None, REASON_UNVERIFIABLE_RECENT_DATE
    if published_at is None:
        safe_title = re.sub(r"[\r\n\t]+", " ", title)[:100]
        raw = re.sub(r"[\r\n\t]+", " ", clean_text(date))[:100]
        print(
            f"[DATE_INCONNUE] source={source} titre={safe_title!r} "
            f"date_brute={raw!r} — fraîcheur 24 h non vérifiable",
            file=sys.stderr,
        )
    matched_domains, domain_scores = score_offer_domains(
        title, description, domains=domains, active_ids=active_ids,
        overrides=scoring,
    )
    if not matched_domains:
        return None, REASON_NO_DOMAIN
    company = clean_text(company, "Entreprise non précisée")
    location = clean_text(location, regions[region_id]["label"])
    url = clean_text(url)
    return {
        "id": offer_id(url, title, company),
        "title": title,
        "company": company,
        "location": location,
        "url": url,
        "source": source,
        "date": published_at.date().isoformat() if published_at else "",
        "date_known": published_at is not None,
        "date_raw": clean_text(date),
        # Une date inconnue conserve le comportement historique : classement
        # au moment du scan, sans prétendre qu'il s'agit de sa publication.
        "sort_date": (published_at or now).isoformat(),
        "freshness_verified_at": (now.isoformat() if freshness_verified else ""),
        "domains": matched_domains,
        "regions": [region_id],
        "domain_scores": domain_scores,
        "highlighted": is_highlighted_company(company),
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


def plural(count: int, word: str) -> str:
    return f"{count} {word}{'s' if count > 1 else ''}"


def human_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds} s"
    return f"{seconds // 60} min {seconds % 60:02d} s"


# ============================================================
# SOURCES 1-2 : JobSpy (LinkedIn / Indeed)
# ============================================================

def fetch_jobspy_site(site: str, label: str, scrape_fn=None,
                      region_id: str = DEFAULT_REGION_ID,
                      region_config: dict | None = None,
                      regions: dict | None = None,
                      domains: dict | None = None,
                      active_ids: list[str] | None = None,
                      results_wanted: int | None = None) -> tuple[list[dict], SourceStatus]:
    """Interroge un seul site JobSpy pour toutes les requêtes.

    Chaque site est isolé : une panne Indeed n'influence jamais LinkedIn.
    Un circuit breaker arrête la source après MAX_CONSECUTIVE_BLOCKS
    blocages 403/429 consécutifs (elle sera retentée au prochain cron).
    """
    regions = regions or REGIONS
    domains = domains or DOMAINS
    region_config = region_config or regions[region_id]
    status = source_status(label, region_id, regions)
    started = time.monotonic()
    if scrape_fn is None:
        try:
            from jobspy import scrape_jobs as scrape_fn
        except ImportError:
            status.status = "error"
            status.note = "python-jobspy non installé"
            status.duration_s = round(time.monotonic() - started, 1)
            log_collection_summary(status)
            return [], status

    offers: dict[str, dict] = {}
    consecutive_blocks = attempted = failed = 0
    site_queries = source_queries(site, domains, active_ids)
    if site == "linkedin":
        # Variantes propres à un marché (ex. intitulés anglais au Maroc),
        # ajoutées après le catalogue métier et dédoublonnées.
        site_queries = list(dict.fromkeys(
            site_queries + [query.strip() for query in
                            region_config.get("linkedin_extra_queries", [])]
        ))
    logger_name = {"linkedin": "LinkedIn", "indeed": "Indeed"}[site]

    class CaptureHandler(logging.Handler):
        def __init__(self):
            super().__init__()
            self.records: list[tuple[int, str]] = []

        def emit(self, record):
            self.records.append((record.levelno, record.getMessage()))

    def pages_in_records(records: list[tuple[int, str]], raw_count: int) -> int:
        logged = sum("search page:" in message for _, message in records)
        fallback = max(1, math.ceil(raw_count / JOBSPY_PAGE_SIZES[site]))
        return max(fallback, logged)

    for query in site_queries:
        if consecutive_blocks >= MAX_CONSECUTIVE_BLOCKS:
            status.note = (f"circuit breaker : requêtes restantes ignorées après "
                           f"{consecutive_blocks} blocages consécutifs")
            break
        attempted += 1
        # hours_old borne la fraîcheur au plus près de la source. Le filtre
        # titre local reste l'autorité sur le type de contrat, car les sites
        # étiquettent parfois mal leurs offres.
        native_location = (region_config["linkedin_location"] if site == "linkedin"
                           else region_config["country_indeed"])
        kwargs = dict(
            site_name=[site],
            search_term=query,
            location=native_location,
            results_wanted=(results_wanted if results_wanted is not None
                            else JOBSPY_RESULTS_WANTED.get(site, 15)),
            country_indeed=region_config["country_indeed"],
            hours_old=OFFER_WINDOW_HOURS,
            verbose=int(os.environ.get("JOBSPY_VERBOSE", "1")),
        )
        if site == "linkedin":
            # Les MAX_LINKEDIN_DESCRIPTION_QUERIES premières requêtes reçoivent
            # la description complète : compromis entre biais « titre seul » et
            # coût réseau (une page LinkedIn par offre).
            kwargs["linkedin_fetch_description"] = (
                site_queries.index(query) < MAX_LINKEDIN_DESCRIPTION_QUERIES
            )
        capture = CaptureHandler()
        jobspy_logger = logging.getLogger(f"JobSpy:{logger_name}")
        jobspy_logger.addHandler(capture)
        try:
            df = scrape_fn(**kwargs)
        except Exception as exc:
            jobspy_logger.removeHandler(capture)
            status.pages_fetched += pages_in_records(capture.records, 0)
            failed += 1
            code = block_code(exc)
            if code:
                consecutive_blocks += 1
                status.http_error = f"HTTP {code}"
                print(f"[WARN] {label} '{query}' bloqué ({status.http_error})", file=sys.stderr)
            else:
                status.http_error = status.http_error or safe_error(exc)[:200]
                print(f"[WARN] {label} '{query}' a échoué : {safe_error(exc)}", file=sys.stderr)
            continue
        finally:
            if capture in jobspy_logger.handlers:
                jobspy_logger.removeHandler(capture)

        raw = len(df)
        status.pages_fetched += pages_in_records(capture.records, raw)

        # JobSpy journalise parfois un 403/429 puis retourne un DataFrame vide
        # sans lever d'exception. On transforme ce signal en vrai statut
        # blocked pour le dashboard et le circuit breaker.
        captured_code = next(
            (block_code(Exception(message)) for _, message in capture.records
             if block_code(Exception(message))),
            None,
        )
        if captured_code:
            failed += 1
            consecutive_blocks += 1
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
            # Certains connecteurs avalent l'exception, loggent une erreur
            # puis renvoient un DataFrame vide. Sans ce contrôle le dashboard
            # affichait à tort « 0 offre ».
            failed += 1
            status.http_error = status.http_error or captured_error[:200]
            print(f"[WARN] {label} '{query}' a échoué : {captured_error}", file=sys.stderr)
            continue
        status.raw_count += raw
        print(f"[INFO] {label} '{query}' : {raw} résultats bruts")
        accepted_before = status.accepted_count
        for _, row in df.iterrows():
            offer, reason = make_offer(
                row.get("title"), row.get("company"),
                ", ".join(filter(None, [clean_text(row.get("city")),
                                        clean_text(row.get("state"))]))
                or region_config["label"],
                row.get("job_url"), label, row.get("date_posted"),
                description=clean_text(row.get("description")),
                domains=domains, active_ids=active_ids,
                region_id=region_id, regions=regions,
                freshness_verified=True,
            )
            register_offer(status, offers, offer, reason)
        status.query_yield[query] = {
            "raw": raw, "accepted": status.accepted_count - accepted_before,
        }

    status.duration_s = round(time.monotonic() - started, 1)
    if consecutive_blocks >= MAX_CONSECUTIVE_BLOCKS or (
            attempted and failed == attempted and status.http_error.startswith("HTTP")):
        status.status = "blocked"
    elif attempted and failed == attempted:
        status.status = "error"
    else:
        status.finalize()
    log_collection_summary(status)
    return list(offers.values()), status


# ============================================================
# SOURCE 5 : APEC (recherche publique de stages, sans compte)
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


def fetch_apec_detail_description(number: str) -> tuple[str, bool]:
    """Texte complet d'une offre APEC (le snippet de recherche est
    tronqué vers 280 caractères, souvent avant les missions).

    Retourne ``(texte, blocked)`` : ``blocked`` distingue un blocage 403/429
    d'une autre erreur, pour permettre un circuit breaker côté appelant sans
    répéter des appels voués à échouer identiquement.
    """
    try:
        resp = requests.get(
            "https://www.apec.fr/cms/webservices/offre/public",
            params={"numeroOffre": number},
            headers=APEC_HEADERS, timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json() or {}
        blocks = [data.get("texteHtml"), data.get("texteHtmlProfil"),
                  data.get("texteHtmlEntreprise")]
        text = " ".join(str(b) for b in blocks if b)
        return re.sub(r"<[^>]+>", " ", text), False
    except Exception as exc:
        print(f"[WARN] APEC détail {number} a échoué : {safe_error(exc)}", file=sys.stderr)
        return "", bool(block_code(exc))


def fetch_apec_all(region_id: str = DEFAULT_REGION_ID,
                   region_config: dict | None = None,
                   regions: dict | None = None,
                   domains: dict | None = None,
                   active_ids: list[str] | None = None) -> tuple[list[dict], SourceStatus]:
    """APEC : scoring titre + texte, avec détails complets bornés."""
    regions = regions or REGIONS
    domains = domains or DOMAINS
    region_config = region_config or regions[region_id]
    status = source_status("APEC", region_id, regions)
    started = time.monotonic()
    if not region_config["apec_available"]:
        status.status = "not_applicable_region"
        status.note = "APEC est une source nationale française"
        status.duration_s = round(time.monotonic() - started, 1)
        print(f"[INFO] APEC · {status.region_label} : non applicable à cette région")
        log_collection_summary(status)
        return [], status

    queries = list(dict.fromkeys(
        source_keywords(query) for query in active_queries(domains, active_ids)
    ))
    raw_by_number: dict[str, dict] = {}
    failed = consecutive_blocks = 0
    for query in queries:
        if consecutive_blocks >= MAX_CONSECUTIVE_BLOCKS:
            # Même garde que les autres sources : la recherche elle-même est
            # bloquée, inutile de marteler APEC avec les requêtes restantes.
            status.note = "circuit breaker : requêtes restantes ignorées"
            failed += 1
            continue
        query_succeeded = False
        stale_pages = 0
        for page in range(MAX_SOURCE_PAGES):
            start_index = page * APEC_PAGE_SIZE
            try:
                resp = requests.post(
                    "https://www.apec.fr/cms/webservices/rechercheOffre",
                    json=apec_search_payload(query, start_index),
                    headers=APEC_HEADERS, timeout=REQUEST_TIMEOUT,
                )
                resp.raise_for_status()
                results = resp.json().get("resultats", [])
            except Exception as exc:
                code = block_code(exc)
                status.http_error = (f"HTTP {code}" if code
                                     else (status.http_error or safe_error(exc)[:200]))
                if code:
                    consecutive_blocks += 1
                print(f"[WARN] APEC '{query}' page {page + 1} a échoué : {safe_error(exc)}",
                      file=sys.stderr)
                break
            consecutive_blocks = 0
            query_succeeded = True
            status.pages_fetched += 1
            status.raw_count += len(results)
            print(f"[INFO] APEC '{query}' page {page + 1} : "
                  f"{len(results)} résultats bruts")
            if not results:
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
            if stale_pages >= MAX_STALE_PAGES or len(results) < APEC_PAGE_SIZE:
                break
            page_dates = [parse_offer_date(r.get("datePublication")) for r in results]
            if (page_dates and all(page_dates)
                    and min(page_dates) < datetime.now(timezone.utc)
                    - timedelta(hours=OFFER_WINDOW_HOURS)):
                # Le payload impose DATE DESCENDING : les pages suivantes sont
                # nécessairement plus anciennes.
                break
        if not query_succeeded:
            failed += 1

    offers: dict[str, dict] = {}
    detail_budget = APEC_DETAIL_MAX_CALLS
    detail_blocked = False
    for number, r in raw_by_number.items():
        url = f"https://www.apec.fr/candidat/recherche-stage.html/stage/detail-offre/{number}"
        args = (r.get("intitule"), r.get("nomCommercial"), r.get("lieuTexte"),
                url, "APEC", r.get("datePublication"))
        # La recherche APEC ne renvoie que des offres actuellement ouvertes
        # (filtre stage serveur) : une datePublication ancienne ne signifie
        # pas une offre expirée. Sans cet indicateur, toute offre publiée il
        # y a plus de 24h était rejetée à chaque run puis considérée absente
        # par le store — d'où un compteur APEC qui fondait à vue d'œil.
        offer, reason = make_offer(*args, confirmed_internship=True,
                                   description=clean_text(r.get("texteOffre")),
                                   domains=domains, active_ids=active_ids,
                                   region_id=region_id, regions=regions,
                                   freshness_verified=True)
        if (offer is None and reason == REASON_NO_DOMAIN
                and detail_budget > 0 and not detail_blocked):
            # Titre insuffisant et snippet tronqué : lire le texte complet
            # avant de rejeter : un titre générique peut cacher des missions
            # spécifiques à l'un des domaines actifs.
            detail_budget -= 1
            full_text, blocked = fetch_apec_detail_description(number)
            if blocked:
                # L'endpoint détail bloque de façon systématique (403/429) :
                # les appels suivants échoueraient identiquement, autant ne
                # pas les tenter et garder le budget informatif exact.
                detail_blocked = True
            if full_text:
                offer, reason = make_offer(*args, confirmed_internship=True,
                                           description=full_text,
                                           domains=domains, active_ids=active_ids,
                                           region_id=region_id, regions=regions,
                                           freshness_verified=True)
        register_offer(status, offers, offer, reason)
    if detail_blocked:
        status.note = "lectures de détail APEC bloquées (403/429)"
    elif detail_budget == 0:
        status.note = f"budget de {APEC_DETAIL_MAX_CALLS} lectures de détail épuisé"

    status.duration_s = round(time.monotonic() - started, 1)
    if failed == len(queries):
        status.status = "blocked" if status.http_error.startswith("HTTP") else "error"
    else:
        status.finalize()
    log_collection_summary(status)
    return list(offers.values()), status


# ============================================================
# SOURCE 6 : Adzuna (API officielle gratuite)
# ============================================================

def fetch_adzuna_all(region_id: str = DEFAULT_REGION_ID,
                     region_config: dict | None = None,
                     regions: dict | None = None,
                     domains: dict | None = None,
                     active_ids: list[str] | None = None) -> tuple[list[dict], SourceStatus]:
    regions = regions or REGIONS
    domains = domains or DOMAINS
    region_config = region_config or regions[region_id]
    status = source_status("Adzuna", region_id, regions)
    started = time.monotonic()
    # L'adaptateur existant utilise l'index Adzuna /fr. Le catalogue demandé
    # ne fournit pas d'identifiant Adzuna pour les autres pays : ne jamais
    # attribuer par erreur une offre française à une autre région.
    if region_id != DEFAULT_REGION_ID:
        status.status = "not_applicable_region"
        status.note = "connecteur Adzuna actuel limité à l'index France"
        log_collection_summary(status)
        return [], status
    app_id = os.environ.get("ADZUNA_APP_ID")
    app_key = os.environ.get("ADZUNA_APP_KEY")
    if not app_id or not app_key:
        status.status = "disabled_missing_credentials"
        status.note = "secrets ADZUNA_APP_ID / ADZUNA_APP_KEY absents"
        log_collection_summary(status)
        return [], status

    offers: dict[str, dict] = {}
    remote_seen: set[str] = set()
    consecutive_blocks = attempted = failed = 0
    for query in active_queries(domains, active_ids):
        if consecutive_blocks >= MAX_CONSECUTIVE_BLOCKS:
            status.note = "circuit breaker : requêtes restantes ignorées"
            break
        attempted += 1
        query_succeeded = False
        stale_pages = 0
        for page in range(1, MAX_SOURCE_PAGES + 1):
            try:
                resp = requests.get(
                    f"https://api.adzuna.com/v1/api/jobs/fr/search/{page}",
                    params={
                        "app_id": app_id, "app_key": app_key,
                        "what": query, "where": region_config["label"],
                        "results_per_page": ADZUNA_PAGE_SIZE,
                        "sort_by": "date", "max_days_old": 1,
                        "content-type": "application/json",
                    },
                    timeout=REQUEST_TIMEOUT,
                )
                resp.raise_for_status()
                payload = resp.json()
                results = payload.get("results", [])
            except Exception as exc:
                code = block_code(exc)
                if code:
                    consecutive_blocks += 1
                    status.http_error = f"HTTP {code}"
                else:
                    status.http_error = status.http_error or safe_error(exc)[:200]
                print(f"[WARN] Adzuna '{query}' page {page} a échoué : {safe_error(exc)}",
                      file=sys.stderr)
                break
            query_succeeded = True
            consecutive_blocks = 0
            status.pages_fetched += 1
            status.raw_count += len(results)
            print(f"[INFO] Adzuna '{query}' page {page} : "
                  f"{len(results)} résultats bruts")
            if not results:
                break
            page_keys = {
                clean_text(r.get("id") or r.get("redirect_url")) for r in results
            } - {""}
            new_keys = page_keys - remote_seen
            remote_seen.update(page_keys)
            stale_pages = stale_pages + 1 if not new_keys else 0
            for r in results:
                # max_days_old=1 filtre déjà la fraîcheur côté serveur (en
                # jours calendaires) : re-rejeter localement à 24 h glissantes
                # écartait des offres limites pourtant garanties récentes par
                # la source (85 rejets constatés sur un run sain) — même
                # logique que le hours_old de JobSpy.
                offer, reason = make_offer(
                    r.get("title"), (r.get("company") or {}).get("display_name"),
                    (r.get("location") or {}).get("display_name"),
                    r.get("redirect_url"), "Adzuna", r.get("created") or "",
                    description=clean_text(r.get("description")),
                    domains=domains, active_ids=active_ids,
                    region_id=region_id, regions=regions,
                    freshness_verified=True,
                )
                register_offer(status, offers, offer, reason)
            total = int(payload.get("count") or 0)
            if (len(results) < ADZUNA_PAGE_SIZE
                    or (total and page * ADZUNA_PAGE_SIZE >= total)
                    or stale_pages >= MAX_STALE_PAGES):
                break
            page_dates = [parse_offer_date(r.get("created")) for r in results]
            if (page_dates and all(page_dates)
                    and min(page_dates) < datetime.now(timezone.utc)
                    - timedelta(hours=OFFER_WINDOW_HOURS)):
                break
        if not query_succeeded:
            failed += 1

    status.duration_s = round(time.monotonic() - started, 1)
    if consecutive_blocks >= MAX_CONSECUTIVE_BLOCKS or (
            attempted and failed == attempted and status.http_error.startswith("HTTP")):
        status.status = "blocked"
    elif attempted and failed == attempted:
        status.status = "error"
    else:
        status.finalize()
    log_collection_summary(status)
    return list(offers.values()), status


def load_rotation_index(path: Path = LINKEDIN_POSTS_ROTATION_FILE) -> int:
    """Curseur de rotation générique ; absent ou corrompu = repart à zéro."""
    if not path.exists():
        return 0
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    if not isinstance(payload, dict):
        return 0
    try:
        return max(0, int(payload.get("next_index", 0)))
    except (TypeError, ValueError):
        return 0


def save_rotation_index(index: int, path: Path = LINKEDIN_POSTS_ROTATION_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"next_index": max(0, int(index))}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )


# ============================================================
# SOURCE 6 : The Muse (API officielle, filtre stage natif)
# ============================================================

THE_MUSE_ENDPOINT = "https://www.themuse.com/api/public/jobs"


def fetch_the_muse_all(region_id: str = DEFAULT_REGION_ID,
                       region_config: dict | None = None,
                       regions: dict | None = None,
                       domains: dict | None = None,
                       active_ids: list[str] | None = None) -> tuple[list[dict], SourceStatus]:
    """The Muse : API officielle avec filtre serveur natif ``level=Internship``.

    Contrairement aux autres sources, aucune requête par mot-clé n'est
    nécessaire : le filtre ``level=Internship`` cible déjà exactement les
    stages (confirmation équivalente au filtre stage natif d'APEC), et
    chaque page couvre l'ensemble du catalogue pour la région — le scoring
    par domaine s'applique ensuite localement, comme pour APEC. Aucune clé
    n'est requise (500 requêtes/heure) ; ``THEMUSE_API_KEY`` est optionnel
    et relève seulement la limite à 3600/heure.
    """
    regions = regions or REGIONS
    domains = domains or DOMAINS
    region_config = region_config or regions[region_id]
    status = source_status("The Muse", region_id, regions)
    started = time.monotonic()
    if region_id != DEFAULT_REGION_ID:
        status.status = "not_applicable_region"
        status.note = "connecteur The Muse actuel limité à la France"
        log_collection_summary(status)
        return [], status

    api_key = os.environ.get("THEMUSE_API_KEY", "").strip()
    offers: dict[str, dict] = {}
    seen_ids: set[str] = set()
    consecutive_blocks = 0
    attempted_pages = failed_pages = 0
    stale_pages = 0
    page = 0
    while page < MAX_SOURCE_PAGES:
        if consecutive_blocks >= MAX_CONSECUTIVE_BLOCKS:
            status.note = "circuit breaker : pages restantes ignorées"
            break
        attempted_pages += 1
        params: dict[str, str | int] = {
            "page": page, "location": region_config["label"],
            "level": "Internship",
        }
        if api_key:
            params["api_key"] = api_key
        try:
            resp = requests.get(
                THE_MUSE_ENDPOINT, params=params, timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            payload = resp.json()
            results = payload.get("results", [])
        except Exception as exc:
            failed_pages += 1
            code = block_code(exc)
            if code:
                consecutive_blocks += 1
                status.http_error = f"HTTP {code}"
            else:
                consecutive_blocks = 0
                status.http_error = status.http_error or safe_error(exc)[:200]
            print(f"[WARN] The Muse page {page} a échoué : {safe_error(exc)}", file=sys.stderr)
            break

        consecutive_blocks = 0
        status.pages_fetched += 1
        status.raw_count += len(results)
        print(f"[INFO] The Muse page {page} : {len(results)} résultats bruts")
        if not results:
            break
        page_ids = {str(item.get("id") or "") for item in results} - {""}
        new_ids = page_ids - seen_ids
        seen_ids.update(page_ids)
        stale_pages = stale_pages + 1 if not new_ids else 0
        for item in results:
            company = clean_text((item.get("company") or {}).get("name"))
            location_entries = item.get("locations") or []
            location = clean_text(
                location_entries[0].get("name") if location_entries else ""
            ) or region_config["label"]
            url = clean_text((item.get("refs") or {}).get("landing_page"))
            offer, reason = make_offer(
                item.get("name"), company, location, url, "The Muse",
                item.get("publication_date") or "",
                confirmed_internship=True,
                description=clean_text(item.get("contents")),
                domains=domains, active_ids=active_ids,
                region_id=region_id, regions=regions,
                # The Muse n'a pas de paramètre hours_old : chaque run relit
                # tout le catalogue courant des offres "Internship" encore
                # ouvertes, quelle que soit leur date de publication d'origine
                # (souvent bien antérieure à 24h). Sans cet indicateur, 100 %
                # des résultats étaient rejetés "plus de 24 heures" alors que
                # l'API confirme qu'ils sont toujours actifs à l'instant du
                # run — même logique que JobSpy (LinkedIn/Indeed) ci-dessus.
                freshness_verified=True,
            )
            register_offer(status, offers, offer, reason)
        page_count = int(payload.get("page_count") or 0)
        page += 1
        if (page_count and page >= page_count) or stale_pages >= MAX_STALE_PAGES:
            break

    status.duration_s = round(time.monotonic() - started, 1)
    if attempted_pages and failed_pages == attempted_pages and status.http_error.startswith("HTTP"):
        status.status = "blocked"
    elif attempted_pages and failed_pages == attempted_pages:
        status.status = "error"
    else:
        status.finalize()
    log_collection_summary(status)
    return list(offers.values()), status


# ============================================================
# SOURCE 8 : France Travail (API officielle Offres d'emploi v2)
# ============================================================

def ft_token(client_id: str, client_secret: str) -> str | None:
    try:
        resp = requests.post(
            "https://entreprise.francetravail.fr/connexion/oauth2/access_token?realm=%2Fpartenaire",
            data={"grant_type": "client_credentials", "client_id": client_id,
                  "client_secret": client_secret,
                  "scope": "api_offresdemploiv2 o2dsoffre"},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()["access_token"]
    except Exception as exc:
        # Ne jamais inclure client_id / client_secret dans les logs.
        print(f"[WARN] Auth France Travail échouée : {type(exc).__name__} {block_code(exc) or ''}",
              file=sys.stderr)
        return None


def fetch_france_travail_all(region_id: str = DEFAULT_REGION_ID,
                             region_config: dict | None = None,
                             regions: dict | None = None,
                             domains: dict | None = None,
                             active_ids: list[str] | None = None) -> tuple[list[dict], SourceStatus]:
    regions = regions or REGIONS
    domains = domains or DOMAINS
    region_config = region_config or regions[region_id]
    status = source_status("France Travail", region_id, regions)
    started = time.monotonic()
    if not region_config["france_travail_available"]:
        status.status = "not_applicable_region"
        status.note = "France Travail est une API nationale française"
        status.duration_s = round(time.monotonic() - started, 1)
        print(f"[INFO] France Travail · {status.region_label} : non applicable à cette région")
        log_collection_summary(status)
        return [], status
    client_id = os.environ.get("FT_CLIENT_ID")
    client_secret = os.environ.get("FT_CLIENT_SECRET")
    if not client_id or not client_secret:
        status.status = "disabled_missing_credentials"
        status.note = "secrets FT_CLIENT_ID / FT_CLIENT_SECRET absents"
        log_collection_summary(status)
        return [], status

    token = ft_token(client_id, client_secret)
    if not token:
        status.status = "error"
        status.note = "authentification OAuth échouée (voir logs)"
        status.duration_s = round(time.monotonic() - started, 1)
        log_collection_summary(status)
        return [], status

    offers: dict[str, dict] = {}
    remote_seen: set[str] = set()
    consecutive_blocks = attempted = failed = 0
    queries = list(dict.fromkeys(
        source_keywords(query) for query in active_queries(domains, active_ids)
    ))
    for query in queries:
        if consecutive_blocks >= MAX_CONSECUTIVE_BLOCKS:
            status.note = "circuit breaker : requêtes restantes ignorées"
            break
        attempted += 1
        query_succeeded = False
        stale_pages = 0
        for page in range(MAX_SOURCE_PAGES):
            start = page * FRANCE_TRAVAIL_PAGE_SIZE
            end = start + FRANCE_TRAVAIL_PAGE_SIZE - 1
            try:
                resp = requests.get(
                    "https://api.francetravail.io/partenaire/offresdemploi/v2/offres/search",
                    headers={"Authorization": f"Bearer {token}"},
                    # Stage natif + publications du dernier jour ; le filtre
                    # local UTC reste l'autorité à la seconde près.
                    params={"motsCles": query, "natureContrat": "FS",
                            "publieeDepuis": 1, "sort": "1",
                            "range": f"{start}-{end}"},
                    timeout=REQUEST_TIMEOUT,
                )
                status.pages_fetched += 1
                if resp.status_code == 204:
                    results = []
                else:
                    resp.raise_for_status()
                    results = resp.json().get("resultats", [])
            except Exception as exc:
                code = block_code(exc)
                if code:
                    consecutive_blocks += 1
                    status.http_error = f"HTTP {code}"
                else:
                    status.http_error = status.http_error or safe_error(exc)[:200]
                print(f"[WARN] France Travail '{query}' page {page + 1} "
                      f"a échoué : {safe_error(exc)}", file=sys.stderr)
                break
            query_succeeded = True
            consecutive_blocks = 0
            status.raw_count += len(results)
            print(f"[INFO] France Travail '{query}' page {page + 1} : "
                  f"{len(results)} résultats bruts")
            if not results:
                break
            page_keys = {clean_text(r.get("id")) for r in results} - {""}
            new_keys = page_keys - remote_seen
            remote_seen.update(page_keys)
            stale_pages = stale_pages + 1 if not new_keys else 0
            for r in results:
                url = (r.get("origineOffre") or {}).get("urlOrigine") or \
                      f"https://candidat.francetravail.fr/offres/recherche/detail/{r.get('id')}"
                # Comme APEC/The Muse : l'API ne renvoie que des offres
                # publiées et ouvertes ; dateCreation ancienne ne vaut jamais
                # rejet (68/71 offres rejetées "plus de 24 heures" sur un run
                # sain avant ce correctif).
                offer, reason = make_offer(
                    r.get("intitule"), (r.get("entreprise") or {}).get("nom"),
                    (r.get("lieuTravail") or {}).get("libelle"),
                    url, "France Travail", r.get("dateCreation") or "",
                    confirmed_internship=True,
                    description=clean_text(r.get("description")),
                    domains=domains, active_ids=active_ids,
                    region_id=region_id, regions=regions,
                    freshness_verified=True,
                )
                register_offer(status, offers, offer, reason)
            content_range = resp.headers.get("Content-Range", "")
            total_match = re.search(r"/(\d+)$", content_range)
            total = int(total_match.group(1)) if total_match else 0
            if (len(results) < FRANCE_TRAVAIL_PAGE_SIZE
                    or (total and end + 1 >= total)
                    or stale_pages >= MAX_STALE_PAGES):
                break
            page_dates = [parse_offer_date(r.get("dateCreation")) for r in results]
            if (page_dates and all(page_dates)
                    and min(page_dates) < datetime.now(timezone.utc)
                    - timedelta(hours=OFFER_WINDOW_HOURS)):
                break
        if not query_succeeded:
            failed += 1

    status.duration_s = round(time.monotonic() - started, 1)
    if consecutive_blocks >= MAX_CONSECUTIVE_BLOCKS or (
            attempted and failed == attempted and status.http_error.startswith("HTTP")):
        status.status = "blocked"
    elif attempted and failed == attempted:
        status.status = "error"
    else:
        status.finalize()
    log_collection_summary(status)
    return list(offers.values()), status


# ============================================================
# SOURCE 9 : LinkedIn Posts (X-ray public, jamais LinkedIn directement)
# ============================================================

def linkedin_posts_query_plan(
        domains: dict | None = None, active_ids: list[str] | None = None,
        *, start_index: int = 0,
        budget: int = LINKEDIN_POSTS_QUERY_BUDGET) -> tuple[list[str], int]:
    """Sous-ensemble rotatif et déterministe du catalogue, sans écriture."""
    queries = active_queries(domains, active_ids)
    if not queries or budget <= 0:
        return [], start_index
    count = min(budget, len(queries))
    start_index = start_index % len(queries)
    plan = [queries[(start_index + i) % len(queries)] for i in range(count)]
    next_index = (start_index + count) % len(queries)
    return plan, next_index


def _build_linkedin_posts_provider(timeout: float, region_hint: str = "France"):
    """Tavily puis Exa (API officielles) selon les clés fournies.

    Tavily est prioritaire : son plan gratuit se renouvelle chaque mois
    (1000 crédits), là où les crédits d'essai Exa sont à usage unique
    (épuisement constaté en production, HTTP 402). DuckDuckGo reste le
    repli final, mais mesuré en conditions réelles il soumet
    systématiquement ces requêtes à son défi anti-bot (HTTP 202,
    « anomaly-modal ») — jamais contourné, seulement détecté.
    """
    provider = FallbackXRayProvider(
        DuckDuckGoHtmlProvider(timeout=timeout),
        DuckDuckGoLiteProvider(timeout=timeout),
    )
    exa_key = os.environ.get("EXA_API_KEY", "").strip()
    if exa_key:
        provider = FallbackXRayProvider(
            ExaProvider(exa_key, timeout=timeout, region_hint=region_hint),
            provider,
        )
    tavily_key = os.environ.get("TAVILY_API_KEY", "").strip()
    if tavily_key:
        provider = FallbackXRayProvider(
            TavilyProvider(tavily_key, timeout=timeout, region_hint=region_hint),
            provider,
        )
    return provider


def fetch_linkedin_posts_all(region_id: str = DEFAULT_REGION_ID,
                             region_config: dict | None = None,
                             regions: dict | None = None,
                             domains: dict | None = None,
                             active_ids: list[str] | None = None,
                             provider=None,
                             rotation_path: Path = LINKEDIN_POSTS_ROTATION_FILE,
                             now: datetime | None = None
                             ) -> tuple[list[dict], SourceStatus]:
    """Posts LinkedIn publics indexés par un moteur de recherche — jamais LinkedIn.

    Ce connecteur n'ouvre jamais linkedin.com : il lit uniquement ce qu'un
    moteur de recherche public (Tavily, Exa, ou DuckDuckGo en repli) expose
    déjà sur les URLs de posts publics. Le budget par run reste prudent et
    rotatif (rotation persistée). Une offre sans fraîcheur vérifiable — ni
    date précise, ni fenêtre garantie côté moteur — est rejetée plutôt que
    conservée par précaution.
    """
    regions = regions or REGIONS
    domains = domains or DOMAINS
    region_config = region_config or regions[region_id]
    status = source_status("LinkedIn Posts", region_id, regions)
    started = time.monotonic()
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)

    if now.hour not in LINKEDIN_POSTS_RUN_HOURS_UTC:
        # Hors créneau : aucune requête, aucun crédit, rotation intacte.
        # Le statut reste "error"-neutre (ni succès ni échec) pour ne jamais
        # faire disparaître les offres des créneaux précédents.
        hours = " et ".join(f"{h:02d}h" for h in LINKEDIN_POSTS_RUN_HOURS_UTC)
        status.status = "deferred"
        status.note = (f"créneaux {hours} UTC — quota gratuit Tavily "
                       "préservé (1000 recherches/mois)")
        log_collection_summary(status)
        return [], status

    start_index = load_rotation_index(rotation_path)
    plan, next_index = linkedin_posts_query_plan(
        domains, active_ids, start_index=start_index,
    )
    print(f"[INFO] LinkedIn Posts : rotation {len(plan)}/"
          f"{len(active_queries(domains, active_ids))} requêtes "
          f"(curseur {start_index} → {next_index})")
    if not plan:
        status.finalize()
        log_collection_summary(status)
        save_rotation_index(next_index, rotation_path)
        return [], status

    provider = provider or _build_linkedin_posts_provider(
        REQUEST_TIMEOUT, region_hint=region_config["label"],
    )

    offers: dict[str, dict] = {}
    consecutive_blocks = 0
    for index, raw_query in enumerate(plan):
        if consecutive_blocks >= MAX_CONSECUTIVE_BLOCKS:
            # Conserver le détail des échecs providers déjà collecté : c'est
            # lui qui porte la cause primaire (ex. crédits Exa épuisés).
            breaker = "circuit breaker : requêtes restantes ignorées"
            status.note = f"{status.note} · {breaker}"[:220] if status.note else breaker
            break
        term = source_keywords(raw_query)
        host = LINKEDIN_POST_HOSTS[index % len(LINKEDIN_POST_HOSTS)]
        query = f'site:{host}/posts "stage" "{term}"'
        query_seen: set[str] = set()
        for page in range(1, LINKEDIN_POSTS_MAX_PAGES + 1):
            try:
                results = provider.search(query, freshness="d", page=page, max_results=30)
            except XRayBlockedError as exc:
                consecutive_blocks += 1
                code = getattr(exc, "status_code", None)
                status.http_error = f"HTTP {code}" if code else "moteur bloqué"
                # L'exception levée par la chaîne de providers est celle du
                # DERNIER repli (souvent le 202 DuckDuckGo) : détailler chaque
                # tentative pour que le dashboard montre la cause primaire
                # (ex. crédits Exa épuisés), pas seulement l'échec du repli.
                attempts = getattr(provider, "last_attempts", None)
                if attempts:
                    failures = [
                        f"{a['provider']}: {a.get('error', a.get('outcome', '?'))}"
                        for a in attempts if a.get("outcome") != "success"
                    ]
                    if failures:
                        status.note = " · ".join(failures)[:220]
                print(f"[WARN] LinkedIn Posts '{query}' page {page} bloquée : {safe_error(exc)}",
                      file=sys.stderr)
                break
            except XRayTimeoutError as exc:
                status.http_error = safe_error(exc)[:200]
                print(f"[WARN] LinkedIn Posts '{query}' page {page} timeout : {safe_error(exc)}",
                      file=sys.stderr)
                break
            except XRayProviderError as exc:
                status.http_error = status.http_error or safe_error(exc)[:200]
                print(f"[WARN] LinkedIn Posts '{query}' page {page} a échoué : {safe_error(exc)}",
                      file=sys.stderr)
                break

            consecutive_blocks = 0
            status.pages_fetched += 1
            status.raw_count += len(results)
            print(f"[INFO] LinkedIn Posts '{query}' page {page} : "
                  f"{len(results)} résultats bruts")
            if not results:
                break
            page_ids = {result.external_id for result in results}
            if page_ids and page_ids.issubset(query_seen):
                break
            query_seen.update(page_ids)
            for result in results:
                if not is_relevant_internship_post(result):
                    status.add_reject(REASON_NOT_INTERNSHIP)
                    continue
                date_value = (
                    result.published_at.isoformat()
                    if result.published_at is not None else ""
                )
                # Fraîcheur : soit une date précise parsée depuis l'extrait,
                # soit une fenêtre garantie côté moteur (time_range=day de
                # Tavily) — même mécanique que hours_old de JobSpy. Sans
                # l'une ni l'autre, rejet plutôt que conservation.
                source_fresh = bool(getattr(result, "source_fresh", False))
                offer, reason = make_offer(
                    result.title, result.author or "Post LinkedIn",
                    region_config["label"], result.url, "LinkedIn Posts",
                    date_value, confirmed_internship=True,
                    description=result.snippet, domains=domains,
                    active_ids=active_ids, region_id=region_id, regions=regions,
                    freshness_verified=source_fresh,
                    reject_missing_date=not source_fresh,
                )
                register_offer(status, offers, offer, reason)
            if len(results) < 30:
                break

    status.duration_s = round(time.monotonic() - started, 1)
    if consecutive_blocks >= MAX_CONSECUTIVE_BLOCKS:
        status.status = "blocked"
    else:
        status.finalize()
    log_collection_summary(status)
    save_rotation_index(next_index, rotation_path)
    return list(offers.values()), status


# ============================================================
# SOURCES LOCALES MAROC : Dreamjob (RSS) et Sites Maroc (Tavily)
# ============================================================

def local_source_status(source: str, region_id: str, region_config: dict,
                        regions: dict) -> SourceStatus | None:
    """Statut « non applicable » si la région n'active pas cette source."""
    if source in region_config.get("local_sources", []):
        return None
    status = source_status(source, region_id, regions)
    status.status = "not_applicable_region"
    status.note = f"source locale non activée pour {region_config['label']}"
    log_collection_summary(status)
    return status


# Les deux sources marocaines publient des titres et résumés éditoriaux
# (« Saham Bank recrute des stagiaires PFE en IT, data et risques ») : le mot
# « data » seul y est le signal métier, « recrute »/« recrutement » figure dans
# presque chaque titre, et le résumé d'une ou deux phrases est trop court pour
# exiger trois mots positifs. Mesuré sur 60 annonces Dreamjob réelles.
MAROC_LOCAL_SCORING = {
    "extra_positive": ["data"],
    "ignore_negative": ["recrutement"],
    "description_only_min_positives": 1,
}
MOROCCAN_CITIES = (
    "Casablanca", "Rabat", "Tanger", "Marrakech", "Fès", "Agadir", "Kénitra",
    "Meknès", "Oujda", "Tétouan", "Mohammedia", "Benguerir", "El Jadida",
    "Settat", "Nador", "Bouskoura", "Témara", "Salé", "Berrechid", "Laâyoune",
)


def moroccan_city(text: str, default: str = "Maroc") -> str:
    for city in MOROCCAN_CITIES:
        if keyword_matches(text, city):
            return f"{city}, Maroc"
    return default


DREAMJOB_FEED = "https://www.dreamjob.ma/stage/feed/"
DREAMJOB_MAX_PAGES = 2
# Identifie le radar auprès du site ; le flux RSS est autorisé par son
# robots.txt (seules /?s=, /wp-admin et /wp-json sont interdites).
RADAR_USER_AGENT = "radar-stage/1.0 (+https://neyos10.github.io/radar-stage-france/)"
DREAMJOB_FOOTER_RE = re.compile(r"L['’]article .*? est apparu en premier sur .*$", re.S)
DREAMJOB_COMPANY_RE = re.compile(r"^(?P<company>.+?)\s+Emploi et Recrutement$", re.I)


def parse_dreamjob_feed(xml_text: str) -> list[dict]:
    """Articles du flux RSS Dreamjob « Stages » : titre, lien, date, résumé."""
    root = ET.fromstring(xml_text)
    items = []
    for item in root.iter("item"):
        categories = [clean_text(c.text) for c in item.findall("category")]
        company = next((m.group("company") for m in
                        (DREAMJOB_COMPANY_RE.match(c) for c in categories) if m), "")
        description = re.sub(r"<[^>]+>", " ", html.unescape(item.findtext("description") or ""))
        items.append({
            "title": html.unescape(clean_text(item.findtext("title"))),
            "url": clean_text(item.findtext("link")),
            "date": clean_text(item.findtext("pubDate")),
            "description": " ".join(DREAMJOB_FOOTER_RE.sub("", description).split()),
            "company": company,
        })
    return items


def rss_date_iso(value: str) -> str:
    try:
        return parsedate_to_datetime(value).astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError):
        return ""


def fetch_dreamjob_all(region_id: str = "maroc",
                       region_config: dict | None = None,
                       regions: dict | None = None,
                       domains: dict | None = None,
                       active_ids: list[str] | None = None,
                       now: datetime | None = None) -> tuple[list[dict], SourceStatus]:
    """Dreamjob.ma, rubrique « Stages » : actualité des stages au Maroc.

    Une requête RSS par run (deux si la première page reste dans la
    fenêtre). Les annonces gardent leur date de publication et restent
    éligibles ``days_window`` jours, comme sur le dashboard.
    """
    regions = regions or REGIONS
    region_config = region_config or regions[region_id]
    skipped = local_source_status("Dreamjob", region_id, region_config, regions)
    if skipped:
        return [], skipped
    status = source_status("Dreamjob", region_id, regions)
    started = time.monotonic()
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    window_start = now - timedelta(days=MAX_OFFER_AGE_DAYS)
    offers: dict[str, dict] = {}
    for page in range(1, DREAMJOB_MAX_PAGES + 1):
        try:
            resp = requests.get(DREAMJOB_FEED, params={"paged": page} if page > 1 else None,
                                headers={"User-Agent": RADAR_USER_AGENT},
                                timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            items = parse_dreamjob_feed(resp.text)
        except Exception as exc:
            code = block_code(exc)
            status.http_error = f"HTTP {code}" if code else safe_error(exc)[:200]
            if page == 1:
                status.status = "blocked" if code else "error"
            print(f"[WARN] Dreamjob page {page} a échoué : {safe_error(exc)}", file=sys.stderr)
            break
        status.pages_fetched += 1
        status.raw_count += len(items)
        print(f"[INFO] Dreamjob page {page} : {len(items)} résultats bruts")
        for item in items:
            offer, reason = make_offer(
                item["title"], item["company"] or "Entreprise (voir l'annonce)",
                moroccan_city(item["title"] + " " + item["description"]),
                item["url"], "Dreamjob", rss_date_iso(item["date"]),
                confirmed_internship=True, description=item["description"],
                domains=domains, active_ids=active_ids,
                region_id=region_id, regions=regions, now=now,
                max_age_hours=MAX_OFFER_AGE_DAYS * 24, scoring=MAROC_LOCAL_SCORING,
            )
            register_offer(status, offers, offer, reason)
        dates = [_store_datetime(rss_date_iso(item["date"])) for item in items]
        if not items or any(d is None or d < window_start for d in dates):
            break
    status.duration_s = round(time.monotonic() - started, 1)
    status.finalize()
    log_collection_summary(status)
    return list(offers.values()), status


# Sites d'emploi marocains interrogés via l'index de Tavily (API officielle),
# jamais directement : ReKrute interdit le crawl de ses recherches dans son
# robots.txt et Emploi.ma oppose un anti-bot. Seules les pages d'offre
# individuelles sont retenues (pas les listes ni les « demandes de stage »).
MAROC_SITES_DOMAINS = ("stagiaires.ma", "rekrute.com", "emploi.ma")
MAROC_SITES_OFFER_URL_RE = re.compile(
    r"^https://(?:www\.)?(?:stagiaires\.ma/stage-emploi-maroc/\d+"
    r"|rekrute\.com/(?:[a-z]{2}/)?offre-emploi-[^/]+-\d+\.html"
    r"|emploi\.ma/offre-emploi-maroc/[^/?#]+)",
    re.I,
)
MAROC_SITES_TITLE_SUFFIX_RE = re.compile(
    r"\s*[|\-–]\s*(?:Stagiaires\.ma|ReKrute(?:\.com)?|Emploi\.ma)\b.*$", re.I)
MAROC_SITES_COMPANY_RE = re.compile(r"\s+chez\s+(?P<company>[^|–\-]+?)\s*$", re.I)
# 5 requêtes × 2 créneaux = tout le catalogue data_ia (9 requêtes) chaque
# jour, soit 10 crédits/jour ; avec LinkedIn Posts (16/jour) le total reste
# à ~780 crédits/mois sur les 1000 gratuits.
MAROC_SITES_QUERY_BUDGET = 5
MAROC_SITES_ROTATION_FILE = Path("data/maroc_sites_rotation.json")


def parse_maroc_site_result(item: dict) -> dict | None:
    url = clean_text(item.get("url"))
    if not MAROC_SITES_OFFER_URL_RE.match(url):
        return None
    title = MAROC_SITES_TITLE_SUFFIX_RE.sub("", clean_text(item.get("title")))
    company = ""
    match = MAROC_SITES_COMPANY_RE.search(title)
    if match:
        company = match.group("company").strip()
        title = title[:match.start()].strip()
    if not title:
        return None
    return {"title": title, "url": url.split("#")[0], "company": company,
            "description": clean_text(item.get("content"))}


def fetch_maroc_sites_all(region_id: str = "maroc",
                          region_config: dict | None = None,
                          regions: dict | None = None,
                          domains: dict | None = None,
                          active_ids: list[str] | None = None,
                          session=None,
                          rotation_path: Path = MAROC_SITES_ROTATION_FILE,
                          now: datetime | None = None) -> tuple[list[dict], SourceStatus]:
    """Offres de stage publiées sur Stagiaires.ma, ReKrute et Emploi.ma.

    Même créneaux et même quota que LinkedIn Posts (Tavily gratuit) :
    ``MAROC_SITES_QUERY_BUDGET`` requêtes rotatives par créneau.
    """
    regions = regions or REGIONS
    region_config = region_config or regions[region_id]
    skipped = local_source_status("Sites Maroc", region_id, region_config, regions)
    if skipped:
        return [], skipped
    status = source_status("Sites Maroc", region_id, regions)
    started = time.monotonic()
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if now.hour not in LINKEDIN_POSTS_RUN_HOURS_UTC:
        hours = " et ".join(f"{h:02d}h" for h in LINKEDIN_POSTS_RUN_HOURS_UTC)
        status.status = "deferred"
        status.note = f"créneaux {hours} UTC — quota gratuit Tavily préservé"
        log_collection_summary(status)
        return [], status
    api_key = os.environ.get("TAVILY_API_KEY", "").strip()
    if not api_key:
        status.status = "disabled_missing_credentials"
        status.note = "secret TAVILY_API_KEY absent"
        log_collection_summary(status)
        return [], status

    start_index = load_rotation_index(rotation_path)
    plan, next_index = linkedin_posts_query_plan(
        domains, active_ids, start_index=start_index, budget=MAROC_SITES_QUERY_BUDGET)
    session = session or requests.Session()
    offers: dict[str, dict] = {}
    failed = 0
    for raw_query in plan:
        query = f"stage PFE {source_keywords(raw_query)} Maroc"
        try:
            resp = session.post(
                "https://api.tavily.com/search",
                json={"query": query, "search_depth": "basic", "max_results": 20,
                      "include_domains": list(MAROC_SITES_DOMAINS), "time_range": "week"},
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=REQUEST_TIMEOUT,
            )
            if resp.status_code in {401, 402, 403, 429, 432}:
                raise RuntimeError(f"HTTP {resp.status_code if resp.status_code != 432 else 429} "
                                   f"Tavily (quota ou clé)")
            resp.raise_for_status()
            results = resp.json().get("results", [])
        except Exception as exc:
            failed += 1
            code = block_code(exc)
            status.http_error = f"HTTP {code}" if code else safe_error(exc)[:200]
            print(f"[WARN] Sites Maroc '{query}' a échoué : {safe_error(exc)}", file=sys.stderr)
            if code:
                break
            continue
        status.pages_fetched += 1
        status.raw_count += len(results)
        accepted_before = status.accepted_count
        for item in results:
            parsed = parse_maroc_site_result(item)
            if parsed is None:
                status.add_reject("page hors offre (liste, demande de stage…)")
                continue
            offer, reason = make_offer(
                parsed["title"], parsed["company"] or "Entreprise (voir l'annonce)",
                moroccan_city(parsed["title"] + " " + parsed["description"]),
                parsed["url"], "Sites Maroc", "", description=parsed["description"],
                domains=domains, active_ids=active_ids, region_id=region_id,
                regions=regions, now=now, freshness_verified=True,
                scoring=MAROC_LOCAL_SCORING,
            )
            register_offer(status, offers, offer, reason)
        status.query_yield[query] = {"raw": len(results),
                                     "accepted": status.accepted_count - accepted_before}
        print(f"[INFO] Sites Maroc '{query}' : {len(results)} résultats bruts")
    status.duration_s = round(time.monotonic() - started, 1)
    if plan and failed == len(plan) or (failed and status.http_error.startswith("HTTP")):
        status.status = "blocked" if status.http_error.startswith("HTTP") else "error"
    else:
        status.finalize()
    log_collection_summary(status)
    save_rotation_index(next_index, rotation_path)
    return list(offers.values()), status


# ============================================================
# ORCHESTRATION
# ============================================================

def run_source_across_regions(source_name: str, fetcher,
                              selected_regions: dict[str, dict]) -> tuple[list[dict], list[SourceStatus]]:
    """Exécute une source séquentiellement par région et fusionne ses doublons."""
    offers: dict[str, dict] = {}
    statuses: list[SourceStatus] = []
    for region_id, region_config in selected_regions.items():
        try:
            regional_offers, status = fetcher(region_id, region_config)
        except Exception as exc:  # ceinture : une région n'arrête pas les autres
            print(f"[ERREUR] {source_name} · {region_config['label']} : {safe_error(exc)}",
                  file=sys.stderr)
            status = source_status(
                source_name, region_id, selected_regions, status="error",
                http_error=safe_error(exc)[:200],
            )
            regional_offers = []
        for offer in regional_offers:
            if offer["id"] in offers:
                merge_offer_metadata(offers[offer["id"]], offer)
            else:
                offers[offer["id"]] = offer
        statuses.append(status)
    return list(offers.values()), statuses


def run_all_sources(domains: dict | None = None,
                    active_domain_ids: list[str] | None = None,
                    regions: dict | None = None,
                    active_region_ids: list[str] | None = None,
                    budget: ScanBudget | None = None) -> tuple[dict[str, dict], list[SourceStatus]]:
    """Croise les régions actives avec les requêtes des domaines actifs.

    Les régions restent séquentielles au sein d'une même source pour ne pas
    marteler le site ; les sources indépendantes s'exécutent en parallèle.
    """
    domains = domains or DOMAINS
    regions = regions or REGIONS
    active_domain_ids = active_domain_ids or enabled_domain_ids(domains)
    active_region_ids = active_region_ids or enabled_region_ids(regions)
    selected_regions = active_regions(regions, active_region_ids)
    budget = budget or calculate_scan_budget(
        len(active_domain_ids), len(active_region_ids)
    )

    def jobspy_fetcher(site: str, label: str):
        return lambda region_id, region_config: fetch_jobspy_site(
            site, label, region_id=region_id, region_config=region_config,
            regions=regions, domains=domains, active_ids=active_domain_ids,
            results_wanted=budget.results_wanted[site],
        )

    fetchers = {
        "LinkedIn": jobspy_fetcher("linkedin", "LinkedIn"),
        "Indeed": jobspy_fetcher("indeed", "Indeed"),
        "APEC": lambda region_id, region_config: fetch_apec_all(
            region_id, region_config, regions, domains, active_domain_ids),
        "Adzuna": lambda region_id, region_config: fetch_adzuna_all(
            region_id, region_config, regions, domains, active_domain_ids),
        "The Muse": lambda region_id, region_config: fetch_the_muse_all(
            region_id, region_config, regions, domains, active_domain_ids),
        "France Travail": lambda region_id, region_config: fetch_france_travail_all(
            region_id, region_config, regions, domains, active_domain_ids),
        "LinkedIn Posts": lambda region_id, region_config: fetch_linkedin_posts_all(
            region_id, region_config, regions, domains, active_domain_ids),
        "Dreamjob": lambda region_id, region_config: fetch_dreamjob_all(
            region_id, region_config, regions, domains, active_domain_ids),
        "Sites Maroc": lambda region_id, region_config: fetch_maroc_sites_all(
            region_id, region_config, regions, domains, active_domain_ids),
    }
    source_names = active_source_names()
    results: dict[str, tuple[list[dict], list[SourceStatus]]] = {}
    with ThreadPoolExecutor(max_workers=MAX_PARALLEL_SOURCES) as pool:
        futures = {
            name: pool.submit(run_source_across_regions, name, fetchers[name],
                              selected_regions)
            for name in source_names
        }
        for name, future in futures.items():
            results[name] = future.result()

    all_offers: dict[str, dict] = {}
    statuses: list[SourceStatus] = []
    for name in source_names:
        offers, source_statuses = results[name]
        for offer in offers:
            if offer["id"] in all_offers:
                merge_offer_metadata(all_offers[offer["id"]], offer)
            else:
                all_offers[offer["id"]] = offer
        statuses.extend(source_statuses)
    return all_offers, statuses


def save_status_json(statuses: list[SourceStatus], scan_duration_s: float,
                     active_ids: list[str] | None = None,
                     active_region_ids: list[str] | None = None,
                     budget: ScanBudget | None = None) -> None:
    active_ids = active_ids or enabled_domain_ids()
    active_region_ids = active_region_ids or enabled_region_ids()
    budget = budget or calculate_scan_budget(len(active_ids), len(active_region_ids))
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "scan_duration_s": round(scan_duration_s, 1),
        "active_domains": active_ids,
        "active_regions": active_region_ids,
        "query_count": len(active_queries(active_ids=active_ids)),
        "combination_count": budget.combination_count,
        "results_wanted": budget.results_wanted,
        "sources": [dict(asdict(st), label=status_label(st)) for st in statuses],
    }
    STATUS_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                           encoding="utf-8")


# ============================================================
# MEMOIRE / EMAIL / DASHBOARD
# ============================================================

def load_seen() -> set:
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text()))
    return set()


SEEN_MAX_ENTRIES = 5000


def save_seen(ids: set) -> None:
    """Ordre d'arrivée conservé : la troncature retire les plus anciens.

    Trier les identifiants (des hash) supprimait des entrées au hasard, qui
    pouvaient alors être ré-annoncées comme nouvelles.
    """
    SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    previous = json.loads(SEEN_FILE.read_text()) if SEEN_FILE.exists() else []
    ordered = [value for value in previous if value in ids]
    ordered += sorted(ids - set(ordered))
    SEEN_FILE.write_text(json.dumps(ordered[-SEEN_MAX_ENTRIES:], ensure_ascii=False, indent=1))


def load_offer_store(path: Path = OFFERS_FILE) -> dict[str, dict]:
    """Charge le store complet ; son absence est une migration valide."""
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} doit contenir un objet JSON")
    return {
        str(offer_id_value): dict(offer)
        for offer_id_value, offer in data.items()
        if isinstance(offer, dict)
    }


def save_offer_store(store: dict[str, dict], path: Path = OFFERS_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = {offer_id_value: store[offer_id_value] for offer_id_value in sorted(store)}
    path.write_text(json.dumps(ordered, ensure_ascii=False, indent=1), encoding="utf-8")


def source_succeeded_for_offer(offer: dict, statuses: list[SourceStatus]) -> bool:
    """Vrai si la source a répondu sainement pour toutes les régions de l'offre.

    ``empty`` est un succès réseau fiable : il confirme précisément que la
    source n'a rien renvoyé. Les statuts bloqués, en erreur, non configurés ou
    non applicables ne doivent jamais faire disparaître une offre persistée.

    Une source à couverture rotative ne confirme jamais une absence : le run
    suivant interroge d'autres requêtes du catalogue, ne pas retrouver l'offre
    n'apporte aucune information. Ses offres expirent par la fenêtre de
    fraîcheur uniquement.
    """
    source = offer.get("source")
    if source in ROTATING_COVERAGE_SOURCES:
        return False
    region_ids = offer.get("regions") or [DEFAULT_REGION_ID]
    by_region = {
        status.region_id: status.status
        for status in statuses
        if status.source == source
    }
    return bool(region_ids) and all(
        by_region.get(region_id) in {"success", "empty"}
        for region_id in region_ids
    )


def _store_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def reconcile_offer_store(found_offers: list[dict], statuses: list[SourceStatus],
                          store: dict[str, dict] | None = None,
                          run_at: datetime | None = None,
                          retention_days: int = OFFER_RETENTION_DAYS) -> dict[str, dict]:
    """Fusionne un run dans le store sans clignotement lors d'une panne source.

    Une absence après un run sain rend l'offre inactive immédiatement. Une
    absence causée par un connecteur bloqué laisse l'offre inchangée. Sont
    purgées les entrées inactives confirmées vues pour la première fois il y a
    plus de ``retention_days`` jours, et toute entrée qu'aucun run n'a revue
    depuis ``retention_days`` jours.
    """
    run_at = (run_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    run_iso = run_at.isoformat(timespec="seconds")
    current = {offer["id"]: offer for offer in found_offers}
    merged = {
        offer_id_value: dict(offer)
        for offer_id_value, offer in (store if store is not None else load_offer_store()).items()
    }

    for offer_id_value, existing in list(merged.items()):
        if offer_id_value in current:
            found = current.pop(offer_id_value)
            first_seen = existing.get("first_seen") or run_iso
            updated = dict(existing)
            updated.update(found)
            updated["first_seen"] = first_seen
            if not found.get("date_known", bool(found.get("date"))):
                # Date inconnue : sort_date vaut l'heure de la PREMIÈRE
                # observation. La rafraîchir à chaque run ferait remonter en
                # tête les offres anciennes revues, au lieu des nouvelles
                # (cas de toutes les offres LinkedIn : JobSpy 1.1.82 ne lit
                # plus leur date).
                updated["sort_date"] = existing.get("sort_date") or found["sort_date"]
            updated["last_seen_run"] = run_iso
            updated["active"] = True
            if source_succeeded_for_offer(found, statuses):
                updated["last_seen_success"] = run_iso
            else:
                updated.setdefault("last_seen_success", "")
            merged[offer_id_value] = updated
        elif source_succeeded_for_offer(existing, statuses):
            existing["active"] = False
            existing["last_seen_success"] = run_iso

    for offer_id_value, found in current.items():
        entry = dict(found)
        entry["first_seen"] = run_iso
        entry["last_seen_run"] = run_iso
        entry["last_seen_success"] = (
            run_iso if source_succeeded_for_offer(found, statuses) else ""
        )
        entry["active"] = True
        merged[offer_id_value] = entry

    cutoff = run_at - timedelta(days=retention_days)
    for offer_id_value, offer in list(merged.items()):
        first_seen = _store_datetime(offer.get("first_seen", ""))
        last_seen_run = _store_datetime(offer.get("last_seen_run", ""))
        last_seen_success = _store_datetime(offer.get("last_seen_success", ""))
        confirmed_absent = (
            not offer.get("active", True)
            and last_seen_success is not None
            and (last_seen_run is None or last_seen_success >= last_seen_run)
        )
        # Plus vue par aucun run depuis la rétention : ni affichable (la
        # fenêtre d'affichage est plus courte) ni utile à la détection de
        # disparition. Couvre les entrées qu'aucun run sain ne peut plus
        # confirmer absentes : région ou source désactivée, source rotative.
        last_sighting = last_seen_run or first_seen
        stale = last_sighting is not None and last_sighting < cutoff
        if (first_seen and first_seen < cutoff and confirmed_absent) or stale:
            del merged[offer_id_value]
    return merged


# Sources dont la réponse est la liste complète des offres encore ouvertes
# (aucun filtre de date côté serveur) : une absence confirmée y signifie une
# offre fermée. Les autres sources ne renvoient que les dernières 24 h ; une
# offre qui en sort n'est pas fermée pour autant.
CATALOG_SNAPSHOT_SOURCES = {"APEC", "The Muse"}


def dashboard_offers_from_store(store: dict[str, dict],
                                now: datetime | None = None,
                                days_window: int = MAX_OFFER_AGE_DAYS,
                                active_domain_ids: list[str] | None = None,
                                active_region_ids: list[str] | None = None) -> list[dict]:
    """Offres affichables, extraites du store persistant.

    Une offre est visible si sa source l'a revue dans les dernières 24 h, ou
    si elle a été publiée (date inconnue : vue pour la première fois) dans
    les ``days_window`` derniers jours. La collecte reste limitée à 24 h ;
    l'affichage tient la promesse « 7 derniers jours » du dashboard. Seule une
    source « catalogue complet » peut retirer une offre avant l'échéance.

    Avec ``active_domain_ids`` / ``active_region_ids``, les offres du store
    rattachées uniquement à des domaines ou régions désactivés sont masquées
    (elles restent dans le store jusqu'à leur purge par rétention).
    """
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    cutoff = now - timedelta(hours=OFFER_WINDOW_HOURS)
    window_start = now - timedelta(days=days_window)
    visible: list[dict] = []
    for offer in store.values():
        if active_domain_ids is not None and not (
                set(offer.get("domains", [])) & set(active_domain_ids)):
            continue
        if active_region_ids is not None and not (
                set(offer.get("regions") or [DEFAULT_REGION_ID]) & set(active_region_ids)):
            continue
        active = offer.get("active", True)
        verified_at = _store_datetime(offer.get("freshness_verified_at", ""))
        if active and verified_at and verified_at >= cutoff:
            visible.append(offer)
            continue
        if not active and offer.get("source") in CATALOG_SNAPSHOT_SOURCES:
            continue
        if offer.get("date_known", bool(offer.get("date"))):
            reference = _store_datetime(offer.get("sort_date", "") or offer.get("date", ""))
        else:
            # Date de publication inconnue : conservée et datée par sa
            # première observation, comme historiquement.
            reference = _store_datetime(offer.get("first_seen", ""))
        if reference is None or reference >= window_start:
            visible.append(offer)
    return visible


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
                     if shows_highlight(o, config) else "")
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
    "deferred": "muted",
}


def render_dashboard(offers: list[dict], new_ids: set,
                     statuses: list[SourceStatus] | None = None,
                     scan_duration_s: float = 0.0,
                     domains: dict | None = None,
                     config: dict | None = None,
                     active_ids: list[str] | None = None,
                     regions: dict | None = None,
                     active_region_ids: list[str] | None = None) -> None:
    domains = domains or DOMAINS
    config = config or APP_CONFIG
    regions = regions or REGIONS
    active_ids = active_ids or enabled_domain_ids(domains)
    active_region_ids = active_region_ids or enabled_region_ids(regions)
    days_window = int(config["days_window"])
    DOCS_INDEX.parent.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.now(timezone.utc)
    now = generated_at.strftime("%d/%m/%Y %H:%M UTC")
    generated_at_iso = generated_at.isoformat()
    source_names = active_source_names(config)
    statuses = [st for st in statuses or [] if st.source in source_names] or [
        source_status(name, active_region_ids[0], regions) for name in source_names]
    active_region_labels = [regions[region_id]["label"]
                            for region_id in active_region_ids]
    active_domain_labels = [domains[domain_id]["label"] for domain_id in active_ids]

    final_counts = {name: 0 for name in source_names}
    domain_counts = {domain_id: 0 for domain_id in active_ids}
    region_counts = {region_id: 0 for region_id in active_region_ids}
    for o in offers:
        final_counts[o["source"]] = final_counts.get(o["source"], 0) + 1
        for domain_id in o.get("domains", []):
            if domain_id in domain_counts:
                domain_counts[domain_id] += 1
        for region_id in o.get("regions", []):
            if region_id in region_counts:
                region_counts[region_id] += 1

    cards = []
    for o in sort_offers(offers):
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
        source = html.escape(o["source"], quote=True)
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
        highlighted = shows_highlight(o, config)
        url = safe_url(o["url"])
        link = (f'<a href="{url}" target="_blank" rel="noopener noreferrer">{title}</a>'
                if url else title)
        cards.append(f"""
      <article class="job {'is-new' if is_new else ''}" data-id="{oid}"
               data-src="{source}" data-domains="{domain_attr}" data-regions="{region_attr}"
               data-highlight="{'1' if highlighted else '0'}" data-search="{search_attr}">
        <div class="job-top"><div class="job-content">
          <h3>{link}</h3>
          <div class="meta">{company} · {location}</div>
          <div class="published">{published_line}</div>
        </div>
        <button class="apply-btn" type="button" data-offer-id="{oid}">✓ Marquer comme postulé</button></div>
        <div class="tags" translate="no">{'<span class="tag new">NOUVEAU</span>' if is_new else ''}
          {f'<span class="tag highlight">✓ {html.escape(config["highlight_label"])}</span>' if highlighted else ''}
          {'<span class="tag date-unknown">date inconnue</span>' if not date_known else ''}
          {domain_tags}{region_tags}<span class="tag src">{source}</span></div>
      </article>""")

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
                       "empty": 3, "deferred": 4, "not_applicable_region": 5,
                       "success": 6}
    for source_name in source_names:
        related = [st for st in statuses if st.source == source_name]
        if not related:
            related = [source_status(source_name, active_region_ids[0], regions)]
        # Une source nationale (APEC, France Travail…) est not_applicable
        # hors France : ce non-statut ne doit jamais masquer le résultat réel
        # de la région où elle a tourné (« non applicable » affiché alors que
        # la source était en succès en France, constaté en production).
        applicable = [st for st in related if st.status != "not_applicable_region"]
        representative = min(applicable or related,
                             key=lambda st: status_priority.get(st.status, 9))
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
    # Plusieurs marchés actifs : tous visibles d'emblée (« Toutes »), le badge
    # région de chaque carte suffit à les distinguer. Une seule région : son
    # chip reste actif, le groupe étant masqué.
    default_region_id = "*" if len(active_region_ids) > 1 else active_region_ids[0]
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
  <div class="automation-status"><span class="live-dot" aria-hidden="true"></span><span>Scan automatique · toutes les heures, 7 h–22 h (Paris)</span></div>
</header>
<main id="top">
<section class="hero" aria-labelledby="pageTitle">
  <div>
    <p class="eyebrow">Radar de stages multi-sources</p>
    <h1 id="pageTitle">Trouvez les offres qui méritent votre candidature.</h1>
    <p class="hero-copy">{tagline}. Les résultats récents sont consolidés, dédupliqués et classés automatiquement.</p>
    <div class="scan-meta">
      <span>Dernier scan <strong>{now}</strong></span>
      <span>Fenêtre <strong>{days_window} jours</strong></span>
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
  <article class="metric"><span class="metric-label">Offres actives</span><strong class="metric-value">{len(offers)}</strong><span class="metric-note">sur les {days_window} derniers jours</span></article>
  <article class="metric"><span class="metric-label">Nouvelles</span><strong class="metric-value">{new_offer_count}</strong><span class="metric-note">depuis le scan précédent</span></article>
  <article class="metric"><span class="metric-label">Marchés</span><strong class="metric-value">{len(active_region_ids)}</strong><span class="metric-note">régions analysées</span></article>
  <article class="metric"><span class="metric-label">Durée du scan</span><strong class="metric-value">{human_duration(scan_duration_s)}</strong><span class="metric-note">collecte et classement</span></article>
</section>

<div class="catalog-summary">
  <span class="catalog-icon" aria-hidden="true">⌁</span>
  <span><strong>{plural(len(active_ids), "domaine")}</strong> et <strong>{plural(len(active_region_ids), "région")}</strong> analysés : {html.escape(' · '.join(active_domain_labels + active_region_labels))}</span>
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
  <label class="search-box" role="search">
    <span aria-hidden="true">⌕</span>
    <input id="searchInput" type="search" placeholder="Poste, entreprise ou ville…" autocomplete="off" aria-label="Rechercher une offre">
  </label>
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
    <button class="fchip region-chip{" on" if default_region_id == "*" else ""}" data-region="*" aria-pressed="{"true" if default_region_id == "*" else "false"}">Toutes</button>{region_chips}
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
    DOCS_INDEX.write_text(html_page, encoding="utf-8")


# ============================================================
# MAIN
# ============================================================

def main() -> None:
    active_ids = enabled_domain_ids()
    active_region_ids = enabled_region_ids()
    budget = calculate_scan_budget(len(active_ids), len(active_region_ids))
    print("Domaines actifs : " + ", ".join(
        f"{domain_id} ({DOMAINS[domain_id]['label']})" for domain_id in active_ids
    ))
    print("Régions actives : " + ", ".join(
        f"{region_id} ({REGIONS[region_id]['label']})" for region_id in active_region_ids
    ))
    print(f"Requêtes actives dédupliquées : {len(active_queries(active_ids=active_ids))}")
    if budget.is_high:
        print(
            f"[WARN] budget de scan élevé : {budget.domain_count} domaines × "
            f"{budget.region_count} régions = {budget.combination_count} combinaisons — "
            f"durée estimée {human_duration(budget.estimated_duration_s)} ; "
            f"results_wanted ajusté à {budget.results_wanted}",
            file=sys.stderr,
        )
    started = time.monotonic()
    all_offers, statuses = run_all_sources(
        active_domain_ids=active_ids,
        active_region_ids=active_region_ids,
        budget=budget,
    )
    scan_duration = time.monotonic() - started

    seen = load_seen()
    new_offers = [o for oid, o in all_offers.items() if oid not in seen]
    new_ids = {o["id"] for o in new_offers}
    run_at = datetime.now(timezone.utc)
    offer_store = reconcile_offer_store(
        list(all_offers.values()), statuses, run_at=run_at
    )
    dashboard_offers = dashboard_offers_from_store(
        offer_store, run_at,
        active_domain_ids=active_ids, active_region_ids=active_region_ids,
    )

    final_counts = {name: 0 for name in active_source_names()}
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
        for query, stats in st.query_yield.items():
            print(f"      · '{query}' : bruts={stats['raw']} acceptés={stats['accepted']}")
    for domain_id, count in domain_counts.items():
        print(f"  - domaine {domain_id}: {count} offres")
    for region_id, count in region_counts.items():
        print(f"  - région {region_id}: {count} offres")

    save_status_json(statuses, scan_duration, active_ids, active_region_ids, budget)

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

    save_offer_store(offer_store)
    render_dashboard(dashboard_offers, new_ids, statuses, scan_duration,
                     DOMAINS, APP_CONFIG, active_ids, REGIONS, active_region_ids)
    save_seen(seen | set(all_offers.keys()))


if __name__ == "__main__":
    main()
