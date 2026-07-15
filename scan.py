#!/usr/bin/env python3
"""
Radar Stage France — agrégateur multi-sources, multi-domaines et multi-régions.

Sources :
  1. LinkedIn (offres publiques, sans compte)   — via JobSpy
  2. Indeed (régions configurées)                — via JobSpy
  3. Glassdoor (pays supportés par JobSpy)       — via JobSpy
  4. Google Jobs (agrège WTTJ, HelloWork…)       — via JobSpy
  5. APEC France (recherche publique de stages)  — sans compte
  6. Adzuna France (agrégateur officiel)         — si secrets fournis
  7. France Travail (API officielle française)   — si secrets fournis

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
  - data/seen_offers.json : mémoire des offres déjà vues (delta detection)
  - data/source_status.json : diagnostic détaillé du dernier scan
"""

import hashlib
import html
import json
import logging
import os
import re
import smtplib
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from functools import lru_cache
from pathlib import Path

import requests

# ============================================================
# CONFIG
# ============================================================

CONFIG_FILE = Path("data/config.json")
DOMAINS_FILE = Path("data/domains.json")
REGIONS_FILE = Path("data/regions.json")
SEEN_FILE = Path("data/seen_offers.json")
STATUS_FILE = Path("data/source_status.json")
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
    "LinkedIn", "Indeed", "Glassdoor", "Google Jobs", "APEC",
    "Adzuna", "France Travail",
]

# Mots qui doivent apparaître dans le titre pour garder l'offre
# (filtre anti-bruit : les scrapers remontent parfois des CDI)
TITLE_MUST_MATCH = re.compile(r"\b(stage|stagiaire|intern|internship|pfa|pfe)\b", re.I)

# On ne conserve que les offres publiées récemment.
MAX_OFFER_AGE_DAYS = int(APP_CONFIG["days_window"])

# Calibration multi-label. Le seuil 2 conserve la dominance du titre (+3)
# face à un seul signal négatif de description (-1) et permet aux intitulés
# composés explicitement catalogués de survivre à un voisin métier (-4).
TITLE_POSITIVE_WEIGHT = 3
DESCRIPTION_POSITIVE_WEIGHT = 1
TITLE_NEGATIVE_WEIGHT = -4
DESCRIPTION_NEGATIVE_WEIGHT = -1
DOMAIN_SCORE_THRESHOLD = 2

# Réseau / robustesse
REQUEST_TIMEOUT = 20          # secondes, pour tous les appels requests
MAX_CONSECUTIVE_BLOCKS = 2    # 403/429 consécutifs avant d'arrêter la source (run courant)
MAX_PARALLEL_SOURCES = 4      # parallélisme borné entre sources indépendantes
APEC_DETAIL_MAX_CALLS = 25    # budget de lectures du texte complet APEC par run
MAX_LINKEDIN_QUERIES = 6      # allocation équitable entre domaines actifs

# Volume par requête et par site (mesuré le 2026-07-10) :
#  - linkedin : 25 ≈ 13 s/requête (au-delà, la pagination ajoute ~5 s/page) ;
#    à 15 on perdait des offres de stage réelles présentes en page 2.
#  - indeed : l'API répond en ~1 s quel que soit le volume → 30.
#  - glassdoor : souvent bloqué (403), on reste modeste pour ne pas insister.
#  - google : jobspy plafonne lui-même selon ce que Google renvoie.
JOBSPY_RESULTS_WANTED = {"linkedin": 25, "indeed": 30, "glassdoor": 15, "google": 30}
# Dans la table officielle JobSpy 1.1.82, seules France et Belgium portent le
# marqueur Glassdoor parmi les quatre pays du catalogue. Indeed accepte les
# quatre valeurs, mais Glassdoor ne doit pas être appelé pour LU/MA.
GLASSDOOR_SUPPORTED_REGION_COUNTRIES = {"France", "Belgium"}

# Garde-fou combinatoire. Au-delà de six couples domaine × région, le volume
# demandé à JobSpy est multiplié par 6/Z (Z = nombre de combinaisons), avec
# un plancher de cinq résultats pour garder chaque recherche utile. L'estimation
# affichée est volontairement prudente : 45 s fixes + 35 s par combinaison
# effective, plafonnée à six, soit environ 4 min 15 s pour un gros catalogue.
SCAN_COMBINATION_BUDGET = 6
MIN_JOBSPY_RESULTS_WANTED = 5
SCAN_FIXED_ESTIMATE_S = 45
SCAN_SECONDS_PER_EFFECTIVE_COMBINATION = 35


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
    scale = min(1.0, SCAN_COMBINATION_BUDGET / combinations)
    wanted = {
        site: max(MIN_JOBSPY_RESULTS_WANTED, int(round(base * scale)))
        for site, base in JOBSPY_RESULTS_WANTED.items()
    }
    effective_combinations = combinations * scale
    estimated = int(round(
        SCAN_FIXED_ESTIMATE_S
        + SCAN_SECONDS_PER_EFFECTIVE_COMBINATION * effective_combinations
    ))
    return ScanBudget(domain_count, region_count, combinations, scale, wanted, estimated)

# Raisons de rejet (diagnostic)
REASON_NO_TITLE = "titre manquant"
REASON_NOT_INTERNSHIP = "pas un stage"
REASON_TOO_OLD = f"plus de {MAX_OFFER_AGE_DAYS} jours"
REASON_NO_DOMAIN = "aucun domaine"
REASON_DUPLICATE = "doublon"

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
    region_id: str = DEFAULT_REGION_ID
    region_label: str = "France"
    status: str = "empty"
    raw_count: int = 0
    accepted_count: int = 0
    rejected_count: int = 0
    reject_reasons: dict = field(default_factory=dict)
    unknown_date_count: int = 0
    duration_s: float = 0.0
    http_error: str = ""
    note: str = ""

    def add_reject(self, reason: str | None) -> None:
        reason = reason or "raison inconnue"
        self.rejected_count += 1
        self.reject_reasons[reason] = self.reject_reasons.get(reason, 0) + 1

    def finalize(self) -> None:
        if self.status in ("blocked", "error", "disabled_missing_credentials",
                           "not_applicable_region"):
            return
        self.status = "success" if self.accepted_count else "empty"


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
    if st.status == "blocked":
        code = st.http_error.replace("HTTP ", "") if st.http_error else "403/429"
        return f"bloqué {code}"
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
    """Allocation round-robin bornée entre domaines pour tenir sous 3 min."""
    selected = active_domains(domains, active_ids)
    buckets = [list(dict.fromkeys(domain["queries"])) for domain in selected.values()]
    plan: list[str] = []
    seen: set[str] = set()
    depth = 0
    while len(plan) < MAX_LINKEDIN_QUERIES and any(depth < len(bucket) for bucket in buckets):
        for bucket in buckets:
            if depth < len(bucket):
                query = bucket[depth].strip()
                if query and query not in seen:
                    plan.append(query)
                    seen.add(query)
                    if len(plan) == MAX_LINKEDIN_QUERIES:
                        break
        depth += 1
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
               regions: dict | None = None) -> tuple[dict | None, str | None]:
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
    published_at = parse_offer_date(date)
    if published_at and published_at < datetime.now(timezone.utc) - timedelta(days=MAX_OFFER_AGE_DAYS):
        _log_old_date_rejection(source, title, date, published_at)
        return None, REASON_TOO_OLD
    matched_domains, domain_scores = score_offer_domains(
        title, description, domains=domains, active_ids=active_ids
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
        # Une date inconnue est classée prudemment au moment du scan : les
        # sources sont déjà bornées nativement à 7 jours via hours_old ou API.
        "sort_date": (published_at or datetime.now(timezone.utc)).isoformat(),
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


def human_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds} s"
    return f"{seconds // 60} min {seconds % 60:02d} s"


# ============================================================
# SOURCES 1-4 : JobSpy (LinkedIn / Indeed / Glassdoor / Google)
# ============================================================

def fetch_jobspy_site(site: str, label: str, scrape_fn=None,
                      region_id: str = DEFAULT_REGION_ID,
                      region_config: dict | None = None,
                      regions: dict | None = None,
                      domains: dict | None = None,
                      active_ids: list[str] | None = None,
                      results_wanted: int | None = None) -> tuple[list[dict], SourceStatus]:
    """Interroge un seul site JobSpy pour toutes les requêtes.

    Chaque site est isolé : une panne Glassdoor n'influence jamais Indeed.
    Un circuit breaker arrête la source après MAX_CONSECUTIVE_BLOCKS
    blocages 403/429 consécutifs (elle sera retentée au prochain cron).
    """
    regions = regions or REGIONS
    domains = domains or DOMAINS
    region_config = region_config or regions[region_id]
    status = source_status(label, region_id, regions)
    started = time.monotonic()
    if (site == "glassdoor"
            and region_config["country_indeed"] not in GLASSDOOR_SUPPORTED_REGION_COUNTRIES):
        status.status = "not_applicable_region"
        status.note = "Glassdoor non supporté par JobSpy 1.1.82 pour ce pays"
        status.duration_s = round(time.monotonic() - started, 1)
        print(f"[INFO] Glassdoor · {status.region_label} : non applicable à cette région")
        return [], status
    if scrape_fn is None:
        try:
            from jobspy import scrape_jobs as scrape_fn
        except ImportError:
            status.status = "error"
            status.note = "python-jobspy non installé"
            status.duration_s = round(time.monotonic() - started, 1)
            return [], status

    offers: dict[str, dict] = {}
    consecutive_blocks = attempted = failed = 0
    site_queries = source_queries(site, domains, active_ids)
    logger_name = {"linkedin": "LinkedIn", "indeed": "Indeed",
                   "glassdoor": "Glassdoor", "google": "Google"}[site]

    class CaptureHandler(logging.Handler):
        def __init__(self):
            super().__init__()
            self.records: list[tuple[int, str]] = []

        def emit(self, record):
            self.records.append((record.levelno, record.getMessage()))

    for query in site_queries:
        if consecutive_blocks >= MAX_CONSECUTIVE_BLOCKS:
            status.note = (f"circuit breaker : requêtes restantes ignorées après "
                           f"{consecutive_blocks} blocages consécutifs")
            break
        attempted += 1
        # hours_old borne la fraîcheur au plus près de la source. Le filtre
        # titre local reste l'autorité sur le type de contrat, car les sites
        # étiquettent parfois mal leurs offres.
        if site == "linkedin":
            native_location = region_config["linkedin_location"]
        elif site == "google":
            native_location = region_config["google_location"]
        else:
            native_location = region_config["country_indeed"]
        kwargs = dict(
            site_name=[site],
            search_term=query,
            location=native_location,
            results_wanted=(results_wanted if results_wanted is not None
                            else JOBSPY_RESULTS_WANTED.get(site, 15)),
            country_indeed=region_config["country_indeed"],
            hours_old=MAX_OFFER_AGE_DAYS * 24,
            verbose=int(os.environ.get("JOBSPY_VERBOSE", "1")),
        )
        if site == "linkedin":
            # Une seule requête enrichie par run suffit au scoring de la
            # description et garde la collecte LinkedIn dans un budget borné.
            kwargs["linkedin_fetch_description"] = query == site_queries[0]
        if site == "google":
            # google_search_term remplace toute la requête côté JobSpy : on
            # évite la redondance « stage … stage » et on garde une syntaxe
            # Google propre (la requête contient déjà « stage »).
            kwargs["google_search_term"] = f"{query} {region_config['google_location']}"
        capture = CaptureHandler()
        jobspy_logger = logging.getLogger(f"JobSpy:{logger_name}")
        jobspy_logger.addHandler(capture)
        try:
            df = scrape_fn(**kwargs)
        except Exception as exc:
            jobspy_logger.removeHandler(capture)
            failed += 1
            code = block_code(exc)
            if code:
                consecutive_blocks += 1
                status.http_error = f"HTTP {code}"
                print(f"[WARN] {label} '{query}' bloqué ({status.http_error})", file=sys.stderr)
            else:
                status.http_error = status.http_error or str(exc)[:200]
                print(f"[WARN] {label} '{query}' a échoué : {exc}", file=sys.stderr)
            continue
        finally:
            if capture in jobspy_logger.handlers:
                jobspy_logger.removeHandler(capture)

        # JobSpy journalise parfois un 403/429 puis retourne un DataFrame vide
        # sans lever d'exception (cas Glassdoor). On transforme ce signal en
        # vrai statut blocked pour le dashboard et le circuit breaker.
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
            # Certains connecteurs (notamment Glassdoor) avalent l'exception,
            # loggent une erreur puis renvoient un DataFrame vide. Sans ce
            # contrôle le dashboard affichait à tort « 0 offre ».
            failed += 1
            status.http_error = status.http_error or captured_error[:200]
            print(f"[WARN] {label} '{query}' a échoué : {captured_error}", file=sys.stderr)
            continue
        raw = len(df)
        status.raw_count += raw
        print(f"[INFO] {label} '{query}' : {raw} résultats bruts")
        for _, row in df.iterrows():
            offer, reason = make_offer(
                row.get("title"), row.get("company"),
                ", ".join(filter(None, [str(row.get("city") or ""), str(row.get("state") or "")])) or "France",
                row.get("job_url"), label, row.get("date_posted"),
                description=clean_text(row.get("description")),
                domains=domains, active_ids=active_ids,
                region_id=region_id, regions=regions,
            )
            register_offer(status, offers, offer, reason)

    status.duration_s = round(time.monotonic() - started, 1)
    if consecutive_blocks >= MAX_CONSECUTIVE_BLOCKS or (
            attempted and failed == attempted and status.http_error.startswith("HTTP")):
        status.status = "blocked"
    elif attempted and failed == attempted:
        status.status = "error"
    else:
        status.finalize()
    return list(offers.values()), status


# ============================================================
# SOURCE 5 : APEC (recherche publique de stages, sans compte)
# ============================================================

APEC_HEADERS = {"Accept": "application/json", "User-Agent": "radar-stage-france/1.0"}
APEC_ADVERTISER_TYPES = [143684, 143685, 143686, 143687, 143706]
APEC_BAC5_LEVEL = 20019


def apec_search_payload(query: str) -> dict:
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
        # Une seule page large pour couvrir les 7 derniers jours sans
        # multiplier les appels à la recherche publique.
        "pagination": {"range": 100, "startIndex": 0},
        "activeFiltre": True, "pointGeolocDeReference": {},
    }


def fetch_apec_detail_description(number: str) -> str:
    """Texte complet d'une offre APEC (le snippet de recherche est
    tronqué vers 280 caractères, souvent avant les missions)."""
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
        return re.sub(r"<[^>]+>", " ", text)
    except Exception as exc:
        print(f"[WARN] APEC détail {number} a échoué : {exc}", file=sys.stderr)
        return ""


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
        return [], status

    queries = list(dict.fromkeys(
        source_keywords(query) for query in active_queries(domains, active_ids)
    ))
    raw_by_number: dict[str, dict] = {}
    failed = 0
    for query in queries:
        try:
            resp = requests.post(
                "https://www.apec.fr/cms/webservices/rechercheOffre",
                json=apec_search_payload(query),
                headers=APEC_HEADERS, timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            results = resp.json().get("resultats", [])
        except Exception as exc:
            failed += 1
            code = block_code(exc)
            status.http_error = f"HTTP {code}" if code else (status.http_error or str(exc)[:200])
            print(f"[WARN] APEC '{query}' a échoué : {exc}", file=sys.stderr)
            continue
        print(f"[INFO] APEC '{query}' : {len(results)} résultats bruts")
        for r in results:
            number = str(r.get("numeroOffre") or "")
            if number:
                raw_by_number.setdefault(number, r)  # dédoublonnage inter-requêtes
    status.raw_count = len(raw_by_number)

    offers: dict[str, dict] = {}
    detail_budget = APEC_DETAIL_MAX_CALLS
    for number, r in raw_by_number.items():
        url = f"https://www.apec.fr/candidat/recherche-stage.html/stage/detail-offre/{number}"
        args = (r.get("intitule"), r.get("nomCommercial"), r.get("lieuTexte"),
                url, "APEC", r.get("datePublication"))
        offer, reason = make_offer(*args, confirmed_internship=True,
                                   description=clean_text(r.get("texteOffre")),
                                   domains=domains, active_ids=active_ids,
                                   region_id=region_id, regions=regions)
        if offer is None and reason == REASON_NO_DOMAIN and detail_budget > 0:
            # Titre insuffisant et snippet tronqué : lire le texte complet
            # avant de rejeter : un titre générique peut cacher des missions
            # spécifiques à l'un des domaines actifs.
            detail_budget -= 1
            full_text = fetch_apec_detail_description(number)
            if full_text:
                offer, reason = make_offer(*args, confirmed_internship=True,
                                           description=full_text,
                                           domains=domains, active_ids=active_ids,
                                           region_id=region_id, regions=regions)
        register_offer(status, offers, offer, reason)
    if detail_budget == 0:
        status.note = f"budget de {APEC_DETAIL_MAX_CALLS} lectures de détail épuisé"

    status.duration_s = round(time.monotonic() - started, 1)
    if failed == len(queries):
        status.status = "blocked" if status.http_error.startswith("HTTP") else "error"
    else:
        status.finalize()
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
        return [], status
    app_id = os.environ.get("ADZUNA_APP_ID")
    app_key = os.environ.get("ADZUNA_APP_KEY")
    if not app_id or not app_key:
        status.status = "disabled_missing_credentials"
        status.note = "secrets ADZUNA_APP_ID / ADZUNA_APP_KEY absents"
        return [], status

    offers: dict[str, dict] = {}
    consecutive_blocks = attempted = failed = 0
    for query in active_queries(domains, active_ids):
        if consecutive_blocks >= MAX_CONSECUTIVE_BLOCKS:
            status.note = "circuit breaker : requêtes restantes ignorées"
            break
        attempted += 1
        try:
            resp = requests.get(
                "https://api.adzuna.com/v1/api/jobs/fr/search/1",
                params={
                    "app_id": app_id, "app_key": app_key,
                    "what": query, "where": region_config["label"],
                    "results_per_page": 20, "sort_by": "date",
                    "content-type": "application/json",
                },
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            results = resp.json().get("results", [])
        except Exception as exc:
            failed += 1
            code = block_code(exc)
            if code:
                consecutive_blocks += 1
                status.http_error = f"HTTP {code}"
            else:
                status.http_error = status.http_error or str(exc)[:200]
            print(f"[WARN] Adzuna '{query}' a échoué : {exc}", file=sys.stderr)
            continue
        consecutive_blocks = 0
        status.raw_count += len(results)
        print(f"[INFO] Adzuna '{query}' : {len(results)} résultats bruts")
        for r in results:
            offer, reason = make_offer(
                r.get("title"), (r.get("company") or {}).get("display_name"),
                (r.get("location") or {}).get("display_name"),
                r.get("redirect_url"), "Adzuna", (r.get("created") or "")[:10],
                description=clean_text(r.get("description")),
                domains=domains, active_ids=active_ids,
                region_id=region_id, regions=regions,
            )
            register_offer(status, offers, offer, reason)

    status.duration_s = round(time.monotonic() - started, 1)
    if consecutive_blocks >= MAX_CONSECUTIVE_BLOCKS or (
            attempted and failed == attempted and status.http_error.startswith("HTTP")):
        status.status = "blocked"
    elif attempted and failed == attempted:
        status.status = "error"
    else:
        status.finalize()
    return list(offers.values()), status


# ============================================================
# SOURCE 7 : France Travail (API officielle Offres d'emploi v2)
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
        return [], status
    client_id = os.environ.get("FT_CLIENT_ID")
    client_secret = os.environ.get("FT_CLIENT_SECRET")
    if not client_id or not client_secret:
        status.status = "disabled_missing_credentials"
        status.note = "secrets FT_CLIENT_ID / FT_CLIENT_SECRET absents"
        return [], status

    token = ft_token(client_id, client_secret)
    if not token:
        status.status = "error"
        status.note = "authentification OAuth échouée (voir logs)"
        status.duration_s = round(time.monotonic() - started, 1)
        return [], status

    offers: dict[str, dict] = {}
    consecutive_blocks = attempted = failed = 0
    queries = list(dict.fromkeys(
        source_keywords(query) for query in active_queries(domains, active_ids)
    ))
    for query in queries:
        if consecutive_blocks >= MAX_CONSECUTIVE_BLOCKS:
            status.note = "circuit breaker : requêtes restantes ignorées"
            break
        attempted += 1
        try:
            resp = requests.get(
                "https://api.francetravail.io/partenaire/offresdemploi/v2/offres/search",
                headers={"Authorization": f"Bearer {token}"},
                # Les stages n'ont pas de code typeContrat dans l'API v2 :
                # c'est natureContrat=FS (convention de stage).
                params={"motsCles": query, "natureContrat": "FS",
                        "publieeDepuis": MAX_OFFER_AGE_DAYS,
                        "sort": "1", "range": "0-49"},
                timeout=REQUEST_TIMEOUT,
            )
            if resp.status_code == 204:  # aucune offre pour cette requête
                results = []
            else:
                resp.raise_for_status()
                results = resp.json().get("resultats", [])
        except Exception as exc:
            failed += 1
            code = block_code(exc)
            if code:
                consecutive_blocks += 1
                status.http_error = f"HTTP {code}"
            else:
                status.http_error = status.http_error or str(exc)[:200]
            print(f"[WARN] France Travail '{query}' a échoué : {exc}", file=sys.stderr)
            continue
        consecutive_blocks = 0
        status.raw_count += len(results)
        print(f"[INFO] France Travail '{query}' : {len(results)} résultats bruts")
        for r in results:
            url = (r.get("origineOffre") or {}).get("urlOrigine") or \
                  f"https://candidat.francetravail.fr/offres/recherche/detail/{r.get('id')}"
            offer, reason = make_offer(
                r.get("intitule"), (r.get("entreprise") or {}).get("nom"),
                (r.get("lieuTravail") or {}).get("libelle"),
                url, "France Travail", (r.get("dateCreation") or "")[:10],
                confirmed_internship=True,
                description=clean_text(r.get("description")),
                domains=domains, active_ids=active_ids,
                region_id=region_id, regions=regions,
            )
            register_offer(status, offers, offer, reason)

    status.duration_s = round(time.monotonic() - started, 1)
    if consecutive_blocks >= MAX_CONSECUTIVE_BLOCKS or (
            attempted and failed == attempted and status.http_error.startswith("HTTP")):
        status.status = "blocked"
    elif attempted and failed == attempted:
        status.status = "error"
    else:
        status.finalize()
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
            print(f"[ERREUR] {source_name} · {region_config['label']} : {exc}",
                  file=sys.stderr)
            status = source_status(
                source_name, region_id, selected_regions, status="error",
                http_error=str(exc)[:200],
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
        "Glassdoor": jobspy_fetcher("glassdoor", "Glassdoor"),
        "Google Jobs": jobspy_fetcher("google", "Google Jobs"),
        "APEC": lambda region_id, region_config: fetch_apec_all(
            region_id, region_config, regions, domains, active_domain_ids),
        "Adzuna": lambda region_id, region_config: fetch_adzuna_all(
            region_id, region_config, regions, domains, active_domain_ids),
        "France Travail": lambda region_id, region_config: fetch_france_travail_all(
            region_id, region_config, regions, domains, active_domain_ids),
    }
    results: dict[str, tuple[list[dict], list[SourceStatus]]] = {}
    with ThreadPoolExecutor(max_workers=MAX_PARALLEL_SOURCES) as pool:
        futures = {
            name: pool.submit(run_source_across_regions, name, fetcher,
                              selected_regions)
            for name, fetcher in fetchers.items()
        }
        for name, future in futures.items():
            results[name] = future.result()

    all_offers: dict[str, dict] = {}
    statuses: list[SourceStatus] = []
    for name in SOURCE_NAMES:
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


def save_seen(ids: set) -> None:
    SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    SEEN_FILE.write_text(json.dumps(sorted(ids)[-5000:], ensure_ascii=False, indent=1))


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
    statuses = statuses or [source_status(name, active_region_ids[0], regions)
                            for name in SOURCE_NAMES]
    active_region_labels = [regions[region_id]["label"]
                            for region_id in active_region_ids]

    final_counts = {name: 0 for name in SOURCE_NAMES}
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
    for o in sort_offers(offers)[:150]:
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
        highlighted = bool(o.get("highlighted"))
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
                       "empty": 3, "not_applicable_region": 4, "success": 5}
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
    dashboard_config_json = json.dumps({
        "highlightLabel": str(config["highlight_label"]),
        "domainLabels": {
            domain_id: domains[domain_id]["label"] for domain_id in active_ids
        },
        "regionLabels": {
            region_id: regions[region_id]["label"] for region_id in active_region_ids
        },
        "activeRegionIds": active_region_ids,
        "defaultRegion": default_region_id,
        "pageGeneratedAt": generated_at_iso,
        "workflowFile": "scan.yml",
        "workflowRef": "main",
        "repository": "neyos10/radar-stage-france",
    }, ensure_ascii=False).replace("</", "<\\/")

    html_page = f"""<!DOCTYPE html>
<html lang="fr" translate="no"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="google" content="notranslate">
<meta name="radar-generated-at" content="{html.escape(generated_at_iso, quote=True)}">
<meta name="theme-color" content="#07111f">
<meta http-equiv="Content-Security-Policy" content="default-src 'self'; connect-src 'self' https://api.github.com; img-src 'self' data:; style-src 'self'; script-src 'self'; base-uri 'self'; form-action 'none'; object-src 'none'">
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
  <div class="automation-status"><span class="live-dot" aria-hidden="true"></span><span>Automatisation active · toutes les 30 min</span></div>
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
    <button id="scanTriggerBtn" class="button button-primary" type="button">
      <span class="button-icon" aria-hidden="true">↻</span><span class="button-label">Lancer un scan</span>
    </button>
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
</section>
<p class="footer-note">Le statut de candidature et l’accès GitHub restent uniquement dans ce navigateur. Aucun secret n’est intégré au site ni envoyé ailleurs qu’à l’API GitHub.</p>
</main>
</div>

<dialog id="scanDialog" class="scan-dialog" aria-labelledby="scanDialogTitle">
  <div class="dialog-content">
    <button id="scanDialogClose" class="dialog-close" type="button" aria-label="Fermer">×</button>
    <div class="dialog-icon" aria-hidden="true">↻</div>
    <p class="eyebrow">GitHub Actions</p>
    <h2 id="scanDialogTitle">Lancer un nouveau scan</h2>
    <p class="dialog-copy">Le workflow sera déclenché et suivi directement ici. À la fin, le dashboard se rechargera automatiquement.</p>
    <div class="security-note"><span aria-hidden="true">◇</span><span>Le token est conservé uniquement dans la session de cet onglet. Il n’est jamais écrit dans Git, un fichier ou le dashboard.</span></div>
    <div class="token-setup">
      <label for="githubToken">Token GitHub finement limité</label>
      <div class="token-row">
        <input id="githubToken" type="password" placeholder="github_pat_…" autocomplete="off" spellcheck="false">
        <button id="toggleTokenVisibility" class="button button-secondary" type="button">Afficher</button>
      </div>
      <p class="token-help">Première utilisation uniquement : <a id="tokenCreationLink" href="https://github.com/settings/personal-access-tokens/new" target="_blank" rel="noopener noreferrer">créer un token sécurisé préconfiguré</a>, choisir « Only select repositories » → <strong id="tokenRepositoryName">radar-stage-france</strong>, puis vérifier « Actions: Read and write ».</p>
    </div>
    <div id="runState" class="run-state" data-kind="idle" hidden aria-live="polite">
      <div class="run-state-head"><span class="state-dot" aria-hidden="true"></span><span id="runStateTitle"></span></div>
      <p id="runStateCopy" class="run-state-copy"></p>
      <a id="runDetailsLink" class="run-link" href="https://github.com" target="_blank" rel="noopener noreferrer" hidden>Voir les détails techniques</a>
    </div>
    <div class="dialog-actions">
      <button id="forgetTokenBtn" class="button button-danger" type="button" hidden>Oublier l’accès</button>
      <button id="runNowBtn" class="button button-primary" type="button"><span class="button-icon" aria-hidden="true">↻</span>Lancer maintenant</button>
    </div>
  </div>
</dialog>
<div id="toast" class="toast" role="status" aria-live="polite"></div>
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
    for st in statuses:
        reasons = ", ".join(
            f"{k} ×{v}" for k, v in sorted(st.reject_reasons.items(), key=lambda kv: -kv[1])
        ) or "—"
        extra = f" · {st.note}" if st.note else ""
        http = f" · {st.http_error}" if st.http_error else ""
        print(f"  - {st.source} · {st.region_label}: {status_label(st, st.accepted_count)} "
              f"[{st.status}] bruts={st.raw_count} acceptés={st.accepted_count} "
              f"rejetés={st.rejected_count} dates_inconnues={st.unknown_date_count} "
              f"durée={st.duration_s}s · rejets : {reasons}{extra}{http}")
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

    render_dashboard(list(all_offers.values()), new_ids, statuses, scan_duration,
                     DOMAINS, APP_CONFIG, active_ids, REGIONS, active_region_ids)
    save_seen(seen | set(all_offers.keys()))


if __name__ == "__main__":
    main()
