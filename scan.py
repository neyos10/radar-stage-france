#!/usr/bin/env python3
"""
Veille Stage France V2 — agrégateur multi-sources d'offres de stage Data/IA.

Sources :
  1. LinkedIn (offres publiques, sans compte)   — via JobSpy
  2. Indeed France                               — via JobSpy
  3. Glassdoor France                            — via JobSpy
  4. Google Jobs (agrège WTTJ, HelloWork…)       — via JobSpy
  5. APEC (recherche publique de stages)          — sans compte
  6. Adzuna (agrégateur officiel, API gratuite)  — si secrets fournis
  7. France Travail (API officielle)             — si secrets fournis

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
import os
import re
import smtplib
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests

# ============================================================
# CONFIG
# ============================================================

QUERIES = [
    "stage data engineer",
    "stage data engineering PFE",
    "stage big data",
    "stage ETL",
    "stage business intelligence",
    "stage data analyst Power BI",
    "stage analytics engineer",
]

APEC_QUERIES = [
    "data",
    "business intelligence",
    "ETL",
    "big data",
    "Power BI",
    "spark",
]

FRANCE_TRAVAIL_QUERIES = [
    "data engineer",
    "data engineering",
    "big data",
    "ETL",
    "business intelligence",
    "data analyst",
    "Power BI",
]

SOURCE_NAMES = [
    "LinkedIn", "Indeed", "Glassdoor", "Google Jobs", "APEC",
    "Adzuna", "France Travail",
]

# Mots qui doivent apparaître dans le titre pour garder l'offre
# (filtre anti-bruit : les scrapers remontent parfois des CDI)
TITLE_MUST_MATCH = re.compile(r"\b(stage|stagiaire|intern|internship|pfa|pfe)\b", re.I)

# Ciblage issu du CV : Data Engineering / Big Data / ETL / BI.
# Liste positive stricte : uniquement des termes SPÉCIFIQUEMENT data,
# jamais de mots génériques (gestion, outils, processus, analyse…).
PROFILE_TITLE_MUST_MATCH = re.compile(
    r"\b(data\s*(?:engineer(?:ing)?|analyst|warehouse|lake(?:house)?|pipeline|"
    r"quality|governance|management|ops|intern(?:ship)?)|"
    r"analytics?\s+engineer(?:ing)?|big\s+data|"
    r"business\s+intelligence|informatique\s+d[eé]cisionnelle|"
    r"ing[eé]nieur(?:e)?\s+(?:big\s+)?data|etl|talend|pyspark|spark|hadoop|"
    r"kafka|dbt|snowflake|databricks|airflow|sql|power\s*bi|bi)\b",
    re.I,
)
PROFILE_TITLE_EXCLUDE = re.compile(
    r"\b(data\s+scien(?:ce|tist)|machine\s+learning|deep\s+learning|"
    r"computer\s+vision|traitement\s+du\s+langage|nlp)\b",
    re.I,
)

# Liste négative d'exclusion forte sur le TITRE : métiers hors périmètre
# (audit, compta, RH, juridique, commercial, qualité, finance, marketing).
# Rejet immédiat, même si un mot positif matche ailleurs (titre ou
# description) — sauf carve-out explicitement data (2e colonne).
TITLE_DATA_CONTEXT = re.compile(r"\b(data|analytics?|donn[ée]es|BI|d[eé]cisionnel(?:le)?)\b", re.I)
PROFILE_TITLE_HARD_EXCLUDE = [
    (re.compile(r"\baudit(?:eur|rice|eurs|rices|s)?\b", re.I), None),
    (re.compile(r"contr[ôo]le(?:ur|use)?s?\s+de\s+gestion", re.I), None),
    (re.compile(r"\bcomptab", re.I), None),
    (re.compile(r"\b(juridique|juriste|avocat|droit)\b", re.I), None),
    (re.compile(r"\b(ressources\s+humaines|RH)\b", re.I), TITLE_DATA_CONTEXT),
    (re.compile(r"\bcommercial(?:e|es|aux)?\b", re.I), TITLE_DATA_CONTEXT),
    (re.compile(r"\bqualit[ée]\b", re.I),
     re.compile(r"data\s+quality|qualit[ée]\s+(?:de\s+la\s+|des\s+)?donn[ée]es", re.I)),
    (re.compile(r"\b(finances?|financi[eè]re?s?|FP&A)\b", re.I),
     re.compile(r"data\s+finance|quant", re.I)),
    (re.compile(r"\bmarketing\b", re.I),
     re.compile(r"marketing\s+analytics|data\s+marketing", re.I)),
    # Publicité / réseaux sociaux (bruit APEC constaté : « Paid Media - SEA
    # et SMA ») — pas de carve-out, ces métiers ne sont jamais data.
    (re.compile(r"\bpaid\s+media\b|\bSEA\b|\bSMA\b|\bsocial\s+media\b|"
                r"\bcommunity\s+manager\b", re.I), None),
]


def title_hard_excluded(title: str) -> bool:
    """Titre appartenant à un métier hors périmètre CV (rejet immédiat)."""
    for pattern, carve_out in PROFILE_TITLE_HARD_EXCLUDE:
        if pattern.search(title) and not (carve_out and carve_out.search(title)):
            return True
    return False

# Repêchage par description : le titre garde le poids le plus fort, la
# description ne sauve une offre que si elle décrit de vraies missions
# Data/BI (cas "Stagiaire RH Analytics" dont les missions sont Data
# Analytics / Business Intelligence). Pas de mots vagues type "données".
PROFILE_DESCRIPTION_MUST_MATCH = re.compile(
    r"\b(data\s*analytics?|business\s+intelligence|informatique\s+d[eé]cisionnelle|"
    r"data\s+engineer(?:ing)?|ing[eé]nierie\s+(?:de\s+la\s+|des\s+)?donn[ée]es|"
    r"data\s+warehouse|data\s+lake(?:house)?|entrep[ôo]t\s+de\s+donn[ée]es|"
    r"etl|talend|power\s*bi|pyspark|spark|hadoop|kafka|dbt|snowflake|"
    r"databricks|airflow)\b",
    re.I,
)

# On ne conserve que les offres publiées récemment.
MAX_OFFER_AGE_DAYS = 7

# Réseau / robustesse
REQUEST_TIMEOUT = 20          # secondes, pour tous les appels requests
MAX_CONSECUTIVE_BLOCKS = 2    # 403/429 consécutifs avant d'arrêter la source (run courant)
MAX_PARALLEL_SOURCES = 4      # parallélisme borné entre sources indépendantes
APEC_DETAIL_MAX_CALLS = 25    # budget de lectures du texte complet APEC par run

# Volume par requête et par site (mesuré le 2026-07-10) :
#  - linkedin : 25 ≈ 13 s/requête (au-delà, la pagination ajoute ~5 s/page) ;
#    à 15 on perdait des offres de stage réelles présentes en page 2.
#  - indeed : l'API répond en ~1 s quel que soit le volume → 30.
#  - glassdoor : souvent bloqué (403), on reste modeste pour ne pas insister.
#  - google : jobspy plafonne lui-même selon ce que Google renvoie.
JOBSPY_RESULTS_WANTED = {"linkedin": 25, "indeed": 30, "glassdoor": 15, "google": 30}

VISA_FRIENDLY_KEYWORDS = [
    "capgemini", "sopra", "alten", "cgi", "inetum", "accenture", "devoteam",
    "atos", "eviden", "thales", "safran", "airbus", "orange", "bnp",
    "société générale", "societe generale", "axa", "edf", "sncf", "keyrus",
    "talan", "octo", "artefact", "ekimetrics", "dataiku", "deloitte", "kpmg",
    "pwc", "ibm", "microsoft", "amazon", "renault", "stellantis",
    "totalenergies", "crédit agricole", "credit agricole", "micropole",
    "onepoint", "wavestone", "expleo", "akkodis", "davidson", "leyton",
]

SEEN_FILE = Path("data/seen_offers.json")
STATUS_FILE = Path("data/source_status.json")
DOCS_INDEX = Path("docs/index.html")

# Raisons de rejet (diagnostic)
REASON_NO_TITLE = "titre manquant"
REASON_NOT_INTERNSHIP = "pas un stage"
REASON_EXCLUDED = "exclusion Data Science/ML"
REASON_EXCLUDED_DOMAIN = "métier hors périmètre (audit/compta/RH/finance/…)"
REASON_NO_DATE = "date absente ou invalide"
REASON_TOO_OLD = f"plus de {MAX_OFFER_AGE_DAYS} jours"
REASON_NOT_PROFILE = "hors ciblage CV"
REASON_DUPLICATE = "doublon"

# ============================================================
# STATUT PAR SOURCE
# ============================================================

@dataclass
class SourceStatus:
    """Diagnostic d'une source pour un run.

    status : success | empty | blocked | error | disabled_missing_credentials
    """
    source: str
    status: str = "empty"
    raw_count: int = 0
    accepted_count: int = 0
    rejected_count: int = 0
    reject_reasons: dict = field(default_factory=dict)
    duration_s: float = 0.0
    http_error: str = ""
    note: str = ""

    def add_reject(self, reason: str | None) -> None:
        reason = reason or "raison inconnue"
        self.rejected_count += 1
        self.reject_reasons[reason] = self.reject_reasons.get(reason, 0) + 1

    def finalize(self) -> None:
        if self.status in ("blocked", "error", "disabled_missing_credentials"):
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
        status.add_reject(REASON_DUPLICATE)
    else:
        offers[offer["id"]] = offer
        status.accepted_count += 1


# ============================================================
# HELPERS
# ============================================================

def offer_id(url: str, title: str, company: str) -> str:
    base = (url or "") + "|" + (title or "").lower() + "|" + (company or "").lower()
    return hashlib.sha1(base.encode()).hexdigest()[:16]


def is_visa_friendly(company: str) -> bool:
    c = (company or "").lower()
    return any(k in c for k in VISA_FRIENDLY_KEYWORDS)


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


def parse_offer_date(value) -> datetime | None:
    raw = "" if value is None else str(value).strip()
    if not raw or raw.lower() in {"nan", "nat", "none"}:
        return None
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


def make_offer(title, company, location, url, source, date="",
               confirmed_internship=False, description="") -> tuple[dict | None, str | None]:
    """Normalise une offre. Retourne (offre, None) ou (None, raison_du_rejet)."""
    title = clean_text(title)
    if not title:
        return None, REASON_NO_TITLE
    if not confirmed_internship and not TITLE_MUST_MATCH.search(title):
        return None, REASON_NOT_INTERNSHIP
    # Exclusions fortes, toujours sur le titre : la description ne peut pas
    # sauver une offre Data Science / ML / Deep Learning / NLP / Vision,
    # ni un métier hors périmètre (audit, compta, RH, finance, marketing…).
    if PROFILE_TITLE_EXCLUDE.search(title):
        return None, REASON_EXCLUDED
    if title_hard_excluded(title):
        return None, REASON_EXCLUDED_DOMAIN
    published_at = parse_offer_date(date)
    if not published_at:
        return None, REASON_NO_DATE
    if published_at < datetime.now(timezone.utc) - timedelta(days=MAX_OFFER_AGE_DAYS):
        return None, REASON_TOO_OLD
    # Le titre a le poids le plus fort ; à défaut, une description contenant
    # de vraies missions Data/BI repêche l'offre.
    if not PROFILE_TITLE_MUST_MATCH.search(title):
        if not (description and PROFILE_DESCRIPTION_MUST_MATCH.search(description)):
            return None, REASON_NOT_PROFILE
    company = clean_text(company, "Entreprise non précisée")
    location = clean_text(location, "France")
    url = clean_text(url)
    return {
        "id": offer_id(url, title, company),
        "title": title,
        "company": company,
        "location": location,
        "url": url,
        "source": source,
        "date": published_at.date().isoformat(),
        "visa_friendly": is_visa_friendly(company),
    }, None


def sort_offers(offers: list[dict]) -> list[dict]:
    """Plus récentes d'abord ; tri secondaire stable sur le titre."""
    return sorted(sorted(offers, key=lambda o: o.get("title", "")),
                  key=lambda o: o.get("date", ""), reverse=True)


def publication_age(date_value: str) -> str:
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

def fetch_jobspy_site(site: str, label: str, scrape_fn=None) -> tuple[list[dict], SourceStatus]:
    """Interroge un seul site JobSpy pour toutes les requêtes.

    Chaque site est isolé : une panne Glassdoor n'influence jamais Indeed.
    Un circuit breaker arrête la source après MAX_CONSECUTIVE_BLOCKS
    blocages 403/429 consécutifs (elle sera retentée au prochain cron).
    """
    status = SourceStatus(source=label)
    started = time.monotonic()
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
    for query in QUERIES:
        if consecutive_blocks >= MAX_CONSECUTIVE_BLOCKS:
            status.note = (f"circuit breaker : requêtes restantes ignorées après "
                           f"{consecutive_blocks} blocages consécutifs")
            break
        attempted += 1
        # job_type="internship" : mesuré le 2026-07-10 sur LinkedIn, le filtre
        # natif donne un bien meilleur signal (6 titres stage/23 bruts contre
        # 1/30 sans le filtre — sans lui, la page se remplit de CDI).
        # Indeed l'ignore silencieusement quand hours_old est fourni (limite
        # jobspy) : c'est notre filtre titre « pas un stage » qui trie.
        # linkedin_fetch_description=True coûterait ~0,5 s par offre
        # (~+75 s/scan) pour un gain marginal : volontairement désactivé,
        # le repêchage par description est réservé à APEC.
        kwargs = dict(
            site_name=[site],
            search_term=query,
            location="France",
            results_wanted=JOBSPY_RESULTS_WANTED.get(site, 15),
            country_indeed="France",
            job_type="internship",
            hours_old=MAX_OFFER_AGE_DAYS * 24,
            verbose=1,
        )
        if site == "google":
            # google_search_term remplace toute la requête côté JobSpy : on
            # évite la redondance « stage … stage » et on garde une syntaxe
            # Google propre (la requête contient déjà « stage »).
            kwargs["google_search_term"] = f"{query} France"
            kwargs.pop("job_type")
        try:
            df = scrape_fn(**kwargs)
        except Exception as exc:
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
        consecutive_blocks = 0
        raw = len(df)
        status.raw_count += raw
        print(f"[INFO] {label} '{query}' : {raw} résultats bruts")
        for _, row in df.iterrows():
            offer, reason = make_offer(
                row.get("title"), row.get("company"),
                ", ".join(filter(None, [str(row.get("city") or ""), str(row.get("state") or "")])) or "France",
                row.get("job_url"), label, row.get("date_posted"),
                description=clean_text(row.get("description")),
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


def apec_search_payload(query: str) -> dict:
    return {
        "motsCles": query,
        "lieux": [], "fonctions": [], "statutPoste": [],
        "typesContrat": [597171],  # identifiant APEC officiel : STAGE
        # typesConvention = type d'ANNONCEUR (libellés vérifiés dans le bundle
        # Angular d'apec.fr) : 143684 entreprise, 143685 cabinet de
        # recrutement, 143686 agence, 143687 SSII/ESN. Le code absent, 143706
        # (« offres partenaires » : flux Proaxian, Talents Handicap…), est la
        # source principale du bruit hors-sujet constaté — on l'exclut donc
        # côté serveur. NB : « niveauEtude » n'est PAS appliqué : le champ est
        # mono-valeur (20019=Bac+5, 20020=>Bac+5) et écarte les offres non
        # taguées (80 → 39 résultats sur « data »), donc trop restrictif pour
        # un PFE.
        "typesConvention": [143684, 143685, 143686, 143687],
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


def fetch_apec_all() -> tuple[list[dict], SourceStatus]:
    """APEC : correspondance CV sur titre + texteOffre (poids au titre),
    lecture bornée du texte complet quand le snippet ne suffit pas."""
    status = SourceStatus(source="APEC")
    started = time.monotonic()

    raw_by_number: dict[str, dict] = {}
    failed = 0
    for query in APEC_QUERIES:
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
                                   description=clean_text(r.get("texteOffre")))
        if offer is None and reason == REASON_NOT_PROFILE and detail_budget > 0:
            # Titre insuffisant et snippet tronqué : lire le texte complet
            # avant de rejeter (ex. « Stagiaire RH Analytics » dont les
            # missions réelles sont Data Analytics / BI).
            detail_budget -= 1
            full_text = fetch_apec_detail_description(number)
            if full_text:
                offer, reason = make_offer(*args, confirmed_internship=True,
                                           description=full_text)
        register_offer(status, offers, offer, reason)
    if detail_budget == 0:
        status.note = f"budget de {APEC_DETAIL_MAX_CALLS} lectures de détail épuisé"

    status.duration_s = round(time.monotonic() - started, 1)
    if failed == len(APEC_QUERIES):
        status.status = "blocked" if status.http_error.startswith("HTTP") else "error"
    else:
        status.finalize()
    return list(offers.values()), status


# ============================================================
# SOURCE 6 : Adzuna (API officielle gratuite)
# ============================================================

def fetch_adzuna_all() -> tuple[list[dict], SourceStatus]:
    status = SourceStatus(source="Adzuna")
    started = time.monotonic()
    app_id = os.environ.get("ADZUNA_APP_ID")
    app_key = os.environ.get("ADZUNA_APP_KEY")
    if not app_id or not app_key:
        status.status = "disabled_missing_credentials"
        status.note = "secrets ADZUNA_APP_ID / ADZUNA_APP_KEY absents"
        return [], status

    offers: dict[str, dict] = {}
    consecutive_blocks = attempted = failed = 0
    for query in QUERIES:
        if consecutive_blocks >= MAX_CONSECUTIVE_BLOCKS:
            status.note = "circuit breaker : requêtes restantes ignorées"
            break
        attempted += 1
        try:
            resp = requests.get(
                "https://api.adzuna.com/v1/api/jobs/fr/search/1",
                params={
                    "app_id": app_id, "app_key": app_key,
                    "what": query, "where": "France",
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


def fetch_france_travail_all() -> tuple[list[dict], SourceStatus]:
    status = SourceStatus(source="France Travail")
    started = time.monotonic()
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
    for query in FRANCE_TRAVAIL_QUERIES:
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
                        "publieeDepuis": 7, "sort": "1", "range": "0-49"},
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

def run_all_sources() -> tuple[dict[str, dict], list[SourceStatus]]:
    """Lance toutes les sources avec un parallélisme borné.

    Chaque source reste séquentielle en interne (pas de martèlement d'un
    même site) ; une source en panne n'arrête jamais les autres.
    """
    tasks = {
        "LinkedIn": lambda: fetch_jobspy_site("linkedin", "LinkedIn"),
        "Indeed": lambda: fetch_jobspy_site("indeed", "Indeed"),
        "Glassdoor": lambda: fetch_jobspy_site("glassdoor", "Glassdoor"),
        "Google Jobs": lambda: fetch_jobspy_site("google", "Google Jobs"),
        "APEC": fetch_apec_all,
        "Adzuna": fetch_adzuna_all,
        "France Travail": fetch_france_travail_all,
    }
    results: dict[str, tuple[list[dict], SourceStatus]] = {}
    with ThreadPoolExecutor(max_workers=MAX_PARALLEL_SOURCES) as pool:
        futures = {name: pool.submit(fn) for name, fn in tasks.items()}
        for name, future in futures.items():
            try:
                results[name] = future.result()
            except Exception as exc:  # ceinture : jamais d'arrêt global
                print(f"[ERREUR] {name} : {exc}", file=sys.stderr)
                results[name] = ([], SourceStatus(source=name, status="error",
                                                  http_error=str(exc)[:200]))

    all_offers: dict[str, dict] = {}
    statuses: list[SourceStatus] = []
    for name in SOURCE_NAMES:
        offers, st = results[name]
        for o in offers:
            all_offers.setdefault(o["id"], o)  # dédoublonnage inter-sources
        statuses.append(st)
    return all_offers, statuses


def save_status_json(statuses: list[SourceStatus], scan_duration_s: float) -> None:
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "scan_duration_s": round(scan_duration_s, 1),
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


def send_email(new_offers: list[dict], dashboard_url: str) -> None:
    smtp_user = os.environ["SMTP_USER"]
    smtp_pass = os.environ["SMTP_PASS"]
    to_addr = os.environ.get("ALERT_TO", smtp_user)

    n = len(new_offers)
    subject = f"🎯 {n} nouvelle{'s' if n>1 else ''} offre{'s' if n>1 else ''} de stage Data/IA (France)"

    by_source = {}
    for o in new_offers:
        by_source.setdefault(o["source"], []).append(o)

    parts = [f"<h2>{n} nouvelle{'s' if n>1 else ''} offre{'s' if n>1 else ''}</h2>"]
    for src, lst in sorted(by_source.items()):
        parts.append(f"<h3>{html.escape(src)} ({len(lst)})</h3>")
        for o in lst:
            badge = " ✅ <b>visa-friendly probable</b>" if o["visa_friendly"] else ""
            url = safe_url(o["url"])
            title = html.escape(o["title"])
            link = f"<a href='{url}'><b>{title}</b></a>" if url else f"<b>{title}</b>"
            parts.append(
                f"<p>{link}<br>"
                f"{html.escape(o['company'])} · {html.escape(o['location'])}{badge}</p>"
            )
    parts.append(f"<hr><p><a href='{html.escape(dashboard_url, quote=True)}'>📊 Ouvrir le dashboard complet</a></p>")
    parts.append("<p style='color:#888;font-size:12px'>Pipeline GitHub Actions · JobSpy + APEC + Adzuna + France Travail</p>")

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
}


def render_dashboard(offers: list[dict], new_ids: set,
                     statuses: list[SourceStatus] | None = None,
                     scan_duration_s: float = 0.0) -> None:
    DOCS_INDEX.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
    statuses = statuses or [SourceStatus(source=name) for name in SOURCE_NAMES]
    repository = os.environ.get("GITHUB_REPOSITORY", "neyos10/radar-stage-france")
    actions_url = f"https://github.com/{repository}/actions/workflows/scan.yml"

    final_counts = {name: 0 for name in SOURCE_NAMES}
    for o in offers:
        final_counts[o["source"]] = final_counts.get(o["source"], 0) + 1

    cards = []
    for o in sort_offers(offers)[:150]:
        is_new = o["id"] in new_ids
        published = datetime.fromisoformat(o["date"]).strftime("%d/%m/%Y")
        age = publication_age(o["date"])
        oid = html.escape(str(o["id"]), quote=True)
        title = html.escape(o["title"])
        company = html.escape(o["company"])
        location = html.escape(o["location"])
        source = html.escape(o["source"], quote=True)
        url = safe_url(o["url"])
        link = (f'<a href="{url}" target="_blank" rel="noopener noreferrer">{title}</a>'
                if url else title)
        cards.append(f"""
      <article class="job {'is-new' if is_new else ''}" data-id="{oid}"
               data-src="{source}" data-visa="{'1' if o['visa_friendly'] else '0'}">
        <div class="job-top"><div>
          <h3>{link}</h3>
          <div class="meta">{company} · {location}</div>
          <div class="published">Publiée {age} · {published}</div>
        </div>
        <button class="apply-btn" type="button" onclick="toggleApplied(event,'{oid}')">✓ Marquer comme postulé</button></div>
        <div class="tags" translate="no">{'<span class="tag new">NOUVEAU</span>' if is_new else ''}
          {'<span class="tag visa">✓ visa-friendly</span>' if o['visa_friendly'] else ''}
          <span class="tag match">Profil CV</span><span class="tag src">{source}</span></div>
      </article>""")

    status_rows = "".join(
        f'<span class="src-stat st-{STATUS_CSS.get(st.status, "warn")}">'
        f'<b>{html.escape(st.source)}</b> · '
        f'{html.escape(status_label(st, final_counts.get(st.source, 0)))}</span>'
        for st in statuses
    )

    source_chips = []
    for st in statuses:
        name = html.escape(st.source, quote=True)
        count = final_counts.get(st.source, 0)
        hint = html.escape(chip_hint(st, count))
        note_attr = ("" if count else
                     f' data-note="{html.escape(status_label(st, count), quote=True)}"')
        source_chips.append(
            f'<button class="fchip source-chip" data-src="{name}"{note_attr} '
            f"onclick=\"setSource(this,'{name}')\">{name} <b>{hint}</b></button>"
        )
    filter_btns = "".join(source_chips)

    html_page = f"""<!DOCTYPE html>
<html lang="fr"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="google" content="notranslate">
<title>Radar Stage France — Dashboard</title>
<style>
:root{{--nuit:#101726;--carte:#1A2436;--encre:#E9EEF6;--encre2:#93A1B8;--maj:#6B5BFF;--saf:#F5B841;--ok:#3ED598;--err:#FF7B7B}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--nuit);color:var(--encre);font:15px/1.5 system-ui,sans-serif;padding:26px 16px 70px}}
.wrap{{max-width:840px;margin:0 auto}}
h1{{font-size:26px}} .sub{{color:var(--encre2);font-size:13px;margin-top:6px}}
.hero{{display:flex;align-items:flex-start;justify-content:space-between;gap:18px;margin-bottom:14px}}
.hero-actions{{text-align:right}}
.scan-btn{{display:inline-block;background:var(--maj);color:white;text-decoration:none;border:0;border-radius:10px;padding:9px 14px;font-size:13px;font-weight:700;white-space:nowrap;cursor:pointer}}
.scan-btn.refresh-btn{{background:transparent;border:1px solid var(--maj);color:#CFC8FF;margin-right:6px}}
.scan-hint{{color:var(--encre2);font-size:11px;margin-top:6px;max-width:280px}}
.reset-btn:disabled{{opacity:.4;cursor:not-allowed}}
.profile-note{{background:rgba(107,91,255,.09);border:1px solid rgba(107,91,255,.25);color:#CFC8FF;border-radius:11px;padding:10px 13px;font-size:12.5px;margin-bottom:14px}}
.sources-panel{{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:14px}}
.src-stat{{font-size:11.5px;border-radius:8px;padding:5px 9px;border:1px solid rgba(255,255,255,.08);color:var(--encre2);background:rgba(255,255,255,.02)}}
.src-stat.st-ok{{border-color:rgba(62,213,152,.35);color:var(--ok)}}
.src-stat.st-warn{{border-color:rgba(245,184,65,.3);color:var(--saf)}}
.src-stat.st-bad{{border-color:rgba(255,123,123,.4);color:var(--err)}}
.src-stat.st-muted{{opacity:.6}}
.filter-panel{{background:rgba(255,255,255,.025);border:1px solid rgba(255,255,255,.06);border-radius:12px;padding:11px;margin-bottom:16px}}
.filter-group{{display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin:5px 0}}
.filter-label{{color:var(--encre2);font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;min-width:54px}}
.fchip{{border:1px solid rgba(255,255,255,.14);background:transparent;color:var(--encre2);border-radius:999px;padding:6px 13px;font-size:12.5px;cursor:pointer}}
.fchip.on{{background:rgba(107,91,255,.15);border-color:var(--maj);color:#CFC8FF}}
.reset-btn{{border-color:rgba(255,123,123,.35);color:#FFA8A8}}
.active-filters{{color:var(--encre2);font-size:12px}}
.job{{background:var(--carte);border:1px solid rgba(255,255,255,.06);border-radius:13px;padding:14px 16px;margin-bottom:10px}}
.job.is-new{{border-left:3px solid var(--saf)}}
.job.applied{{opacity:.72;border-color:rgba(62,213,152,.35)}}
.job-top{{display:flex;align-items:flex-start;justify-content:space-between;gap:14px}}
.job h3{{font-size:15px}} .job h3 a{{color:var(--encre);text-decoration:none}}
.job h3 a:hover{{text-decoration:underline}}
.meta{{color:var(--encre2);font-size:13px;margin-top:2px}}
.published{{color:#B8C3D5;font-size:11.5px;margin-top:4px}}
.apply-btn{{border:1px solid rgba(62,213,152,.45);background:rgba(62,213,152,.08);color:var(--ok);border-radius:9px;padding:7px 10px;font-size:11.5px;font-weight:700;cursor:pointer;white-space:nowrap}}
.apply-btn.undo{{border-color:rgba(255,255,255,.16);background:transparent;color:var(--encre2)}}
.tags{{display:flex;gap:6px;margin-top:8px;flex-wrap:wrap}}
.tag{{font-size:10.5px;font-weight:700;border-radius:6px;padding:3px 8px}}
.tag.new{{background:var(--saf);color:#3A2C05}}
.tag.visa{{background:rgba(62,213,152,.15);color:var(--ok)}}
.tag.match{{background:rgba(107,91,255,.15);color:#CFC8FF}}
.tag.src{{background:#212D44;color:var(--encre2);font-weight:500}}
.empty{{color:var(--encre2);text-align:center;padding:30px 10px}}
.storage-note{{color:#66748A;font-size:11px;text-align:center;margin-top:18px}}
@media(max-width:620px){{.hero{{display:block}}.hero-actions{{text-align:left;margin-top:12px}}.scan-hint{{max-width:none}}.job-top{{display:block}}.apply-btn{{margin-top:10px}}.filter-label{{width:100%}}}}
</style></head>
<body><div class="wrap">
<div class="hero"><div><h1>🎯 Radar Stage France</h1>
<p class="sub">Dernier scan : {now} · {len(offers)} offres des {MAX_OFFER_AGE_DAYS} derniers jours · durée du scan : {human_duration(scan_duration_s)}</p></div>
<div class="hero-actions">
<button class="scan-btn refresh-btn" type="button" onclick="location.reload()">↻ Actualiser les offres</button>
<a class="scan-btn" href="{actions_url}" target="_blank" rel="noopener noreferrer">⚙ Ouvrir GitHub Actions</a>
<p class="scan-hint">« Actualiser » recharge les offres du dernier scan (toutes les 30 min). Pour lancer un scan immédiat : GitHub Actions puis « Run workflow ».</p></div></div>
<p class="profile-note">Ciblage CV : Data Engineering, Big Data, Spark/PySpark, ETL/Talend, Data Warehouse, BI et Power BI.</p>
<div class="sources-panel" translate="no">{status_rows}</div>
<div class="filter-panel">
  <div class="filter-group"><span class="filter-label">Statut</span>
    <button class="fchip status-chip on" data-status="todo" onclick="setStatus(this,'todo')">À postuler <b id="todoCount"></b></button>
    <button class="fchip status-chip" data-status="applied" onclick="setStatus(this,'applied')">Déjà postulé <b id="appliedCount"></b></button>
    <button class="fchip status-chip" data-status="all" onclick="setStatus(this,'all')">Tous</button>
  </div>
  <div class="filter-group" translate="no"><span class="filter-label">Source</span>
    <button class="fchip source-chip on" data-src="*" onclick="setSource(this,'*')">Toutes</button>{filter_btns}
  </div>
  <div class="filter-group" translate="no"><span class="filter-label">Options</span>
    <button id="visaFilter" class="fchip" aria-pressed="false" onclick="toggleVisa(this)">✓ Visa-friendly probable</button>
  </div>
  <div class="filter-group"><span class="filter-label">Actifs</span>
    <span id="activeFilters" class="active-filters" translate="no"></span>
    <button id="resetBtn" class="fchip reset-btn" onclick="resetFilters()" disabled>✕ Réinitialiser les filtres</button>
  </div>
</div>
<p id="emptyState" class="empty" hidden>Aucune offre ne correspond à ces filtres.</p>
<div id="jobs">{"".join(cards)}</div>
<p class="storage-note">Le statut de candidature est enregistré uniquement dans ce navigateur et n'est jamais envoyé sur GitHub.</p>
</div>
<script>
const STORAGE_KEY='radar-stage-france-applied-v1';
const STATUS_LABELS={{todo:'À postuler',applied:'Déjà postulé',all:'Tous'}};
let activeSource='*', activeStatus='todo', visaOnly=false;
function readApplied(){{
  try{{const value=JSON.parse(localStorage.getItem(STORAGE_KEY)||'[]');return new Set(Array.isArray(value)?value:[]);}}
  catch(_error){{return new Set();}}
}}
let applied=readApplied();
function saveApplied(){{localStorage.setItem(STORAGE_KEY,JSON.stringify([...applied]));}}
function setSource(btn,src){{
  activeSource=src;
  document.querySelectorAll('.source-chip').forEach(b=>b.classList.remove('on'));
  btn.classList.add('on');refresh();
}}
function setStatus(btn,status){{
  activeStatus=status;
  document.querySelectorAll('.status-chip').forEach(b=>b.classList.remove('on'));
  btn.classList.add('on');refresh();
}}
function toggleVisa(btn){{
  visaOnly=!visaOnly;btn.classList.toggle('on',visaOnly);
  btn.setAttribute('aria-pressed',visaOnly?'true':'false');refresh();
}}
function resetFilters(){{
  activeSource='*';activeStatus='todo';visaOnly=false;
  document.querySelectorAll('.source-chip').forEach(b=>b.classList.toggle('on',b.dataset.src==='*'));
  document.querySelectorAll('.status-chip').forEach(b=>b.classList.toggle('on',b.dataset.status==='todo'));
  const visa=document.getElementById('visaFilter');
  visa.classList.remove('on');visa.setAttribute('aria-pressed','false');
  refresh();
}}
function updateActiveFilters(){{
  const parts=['Statut : '+STATUS_LABELS[activeStatus],
               'Source : '+(activeSource==='*'?'Toutes':activeSource)];
  if(visaOnly)parts.push('Visa-friendly uniquement');
  document.getElementById('activeFilters').textContent=parts.join(' · ');
  const isDefault=activeStatus==='todo'&&activeSource==='*'&&!visaOnly;
  document.getElementById('resetBtn').disabled=isDefault;
}}
function toggleApplied(event,id){{
  event.preventDefault();
  applied.has(id)?applied.delete(id):applied.add(id);saveApplied();refresh();
}}
function refresh(){{
  let visible=0,todo=0,done=0;
  document.querySelectorAll('.job').forEach(job=>{{
    const isApplied=applied.has(job.dataset.id);
    isApplied?done++:todo++;
    job.classList.toggle('applied',isApplied);
    const action=job.querySelector('.apply-btn');
    action.textContent=isApplied?'↩ Remettre à postuler':'✓ Marquer comme postulé';
    action.classList.toggle('undo',isApplied);
    const sourceOk=activeSource==='*'||job.dataset.src===activeSource;
    const statusOk=activeStatus==='all'||(activeStatus==='applied'?isApplied:!isApplied);
    const visaOk=!visaOnly||job.dataset.visa==='1';
    const show=sourceOk&&statusOk&&visaOk;
    job.style.display=show?'':'none';if(show)visible++;
  }});
  document.getElementById('todoCount').textContent='('+todo+')';
  document.getElementById('appliedCount').textContent='('+done+')';
  const empty=document.getElementById('emptyState');
  if(visible===0){{
    let msg='Aucune offre ne correspond à ces filtres.';
    if(activeSource!=='*'){{
      const chip=document.querySelector('.source-chip[data-src="'+activeSource+'"]');
      if(chip&&chip.dataset.note){{msg=activeSource+' : '+chip.dataset.note+' — aucune offre à afficher.';}}
    }}
    empty.textContent=msg;
  }}
  empty.hidden=visible!==0;
  updateActiveFilters();
}}
refresh();
</script>
</body></html>"""
    DOCS_INDEX.write_text(html_page, encoding="utf-8")


# ============================================================
# MAIN
# ============================================================

def main() -> None:
    started = time.monotonic()
    all_offers, statuses = run_all_sources()
    scan_duration = time.monotonic() - started

    seen = load_seen()
    new_offers = [o for oid, o in all_offers.items() if oid not in seen]
    new_ids = {o["id"] for o in new_offers}

    final_counts = {name: 0 for name in SOURCE_NAMES}
    for o in all_offers.values():
        final_counts[o["source"]] = final_counts.get(o["source"], 0) + 1

    print(f"\nTotal : {len(all_offers)} offres · Nouvelles : {len(new_offers)} · "
          f"Durée : {human_duration(scan_duration)}")
    for st in statuses:
        reasons = ", ".join(
            f"{k} ×{v}" for k, v in sorted(st.reject_reasons.items(), key=lambda kv: -kv[1])
        ) or "—"
        extra = f" · {st.note}" if st.note else ""
        http = f" · {st.http_error}" if st.http_error else ""
        print(f"  - {st.source}: {status_label(st, final_counts[st.source])} "
              f"[{st.status}] bruts={st.raw_count} acceptés={st.accepted_count} "
              f"rejetés={st.rejected_count} durée={st.duration_s}s · rejets : {reasons}{extra}{http}")

    save_status_json(statuses, scan_duration)

    dashboard_url = os.environ.get("DASHBOARD_URL", "")
    if new_offers:
        if os.environ.get("SMTP_USER") and os.environ.get("SMTP_PASS"):
            try:
                send_email(new_offers, dashboard_url)
                print("Email envoyé.")
            except Exception as e:
                print(f"[ERREUR] email : {e}", file=sys.stderr)
        else:
            print("[INFO] SMTP non configuré (SMTP_USER/SMTP_PASS absents) — pas d'email envoyé.")

    render_dashboard(list(all_offers.values()), new_ids, statuses, scan_duration)
    save_seen(seen | set(all_offers.keys()))


if __name__ == "__main__":
    main()
