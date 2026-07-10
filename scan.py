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
(429) ou si une clé manque, on continue avec les autres.

Sorties :
  - Email HTML si nouvelles offres (SMTP)
  - docs/index.html : LE dashboard (GitHub Pages) avec toutes les offres
  - data/seen_offers.json : mémoire des offres déjà vues (delta detection)
"""

import hashlib
import json
import os
import re
import smtplib
import sys
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
]

APEC_QUERIES = [
    "data",
    "business intelligence",
    "ETL",
    "big data",
    "Power BI",
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
PROFILE_TITLE_MUST_MATCH = re.compile(
    r"\b(data\s*(?:engineer(?:ing)?|analyst|warehouse|pipeline|quality|governance|"
    r"management|ops|intern(?:ship)?)|analytics?\s+engineer(?:ing)?|big\s+data|"
    r"business\s+intelligence|informatique\s+d[eé]cisionnelle|ing[eé]nieur(?:e)?\s+"
    r"(?:big\s+)?data|etl|talend|pyspark|spark|power\s*bi|bi)\b",
    re.I,
)
PROFILE_TITLE_EXCLUDE = re.compile(
    r"\b(data\s+scien(?:ce|tist)|machine\s+learning|deep\s+learning|"
    r"computer\s+vision|traitement\s+du\s+langage|nlp)\b",
    re.I,
)

# On ne conserve que les offres publiées récemment.
MAX_OFFER_AGE_DAYS = 7

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
DOCS_INDEX = Path("docs/index.html")

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
               confirmed_internship=False) -> dict | None:
    title = clean_text(title)
    if not title or (not confirmed_internship and not TITLE_MUST_MATCH.search(title)):
        return None
    if PROFILE_TITLE_EXCLUDE.search(title):
        return None
    if not PROFILE_TITLE_MUST_MATCH.search(title):
        return None
    published_at = parse_offer_date(date)
    if not published_at:
        return None
    if published_at < datetime.now(timezone.utc) - timedelta(days=MAX_OFFER_AGE_DAYS):
        return None
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
    }


def publication_age(date_value: str) -> str:
    published = datetime.fromisoformat(date_value).date()
    days = max(0, (datetime.now(timezone.utc).date() - published).days)
    if days == 0:
        return "aujourd'hui"
    if days == 1:
        return "hier"
    return f"il y a {days} jours"


# ============================================================
# SOURCE 1-4 : JobSpy (LinkedIn / Indeed / Glassdoor / Google)
# ============================================================

def fetch_jobspy(query: str) -> list[dict]:
    try:
        from jobspy import scrape_jobs
    except ImportError:
        print("[WARN] python-jobspy non installé — sources LinkedIn/Indeed/Glassdoor/Google ignorées", file=sys.stderr)
        return []

    offers = []
    # On sépare les sites pour qu'un blocage n'entraîne pas les autres
    for site in (["linkedin"], ["indeed", "glassdoor"], ["google"]):
        try:
            kwargs = dict(
                site_name=site,
                search_term=query,
                location="France",
                results_wanted=15,
                country_indeed="France",
                job_type="internship",
                hours_old=MAX_OFFER_AGE_DAYS * 24,
            )
            if site == ["google"]:
                # Google Jobs exige sa propre syntaxe de recherche
                kwargs["google_search_term"] = f"{query} France stage"
                kwargs.pop("job_type", None)
            df = scrape_jobs(**kwargs)
            for _, row in df.iterrows():
                o = make_offer(
                    row.get("title"), row.get("company"),
                    ", ".join(filter(None, [str(row.get("city") or ""), str(row.get("state") or "")])) or "France",
                    row.get("job_url"),
                    {"linkedin": "LinkedIn", "indeed": "Indeed", "glassdoor": "Glassdoor", "google": "Google Jobs"}.get(str(row.get("site")), str(row.get("site"))),
                    row.get("date_posted"),
                )
                if o:
                    offers.append(o)
        except Exception as e:
            print(f"[WARN] JobSpy {site} '{query}' a échoué (blocage probable, on continue) : {e}", file=sys.stderr)
    return offers


# ============================================================
# SOURCE 5 : APEC (recherche publique de stages, sans compte)
# ============================================================

def fetch_apec(query: str) -> list[dict]:
    try:
        resp = requests.post(
            "https://www.apec.fr/cms/webservices/rechercheOffre",
            json={
                "motsCles": query,
                "lieux": [], "fonctions": [], "statutPoste": [],
                "typesContrat": [597171],  # identifiant APEC officiel : STAGE
                "typesConvention": [], "niveauxExperience": [],
                "idsEtablissement": [], "secteursActivite": [],
                "typesTeletravail": [], "idNomZonesDeplacement": [],
                "positionNumbersExcluded": [], "typeClient": "CADRE",
                "sorts": [{"type": "DATE", "direction": "DESCENDING"}],
                # Une seule page large pour couvrir les 7 derniers jours sans
                # multiplier les appels à la recherche publique.
                "pagination": {"range": 100, "startIndex": 0},
                "activeFiltre": True, "pointGeolocDeReference": {},
            },
            headers={"Accept": "application/json",
                     "User-Agent": "radar-stage-france/1.0"},
            timeout=20,
        )
        resp.raise_for_status()
        offers = []
        for r in resp.json().get("resultats", []):
            number = r.get("numeroOffre", "")
            url = f"https://www.apec.fr/candidat/recherche-stage.html/stage/detail-offre/{number}"
            o = make_offer(
                r.get("intitule"), r.get("nomCommercial"), r.get("lieuTexte"),
                url, "APEC", r.get("datePublication"), confirmed_internship=True,
            )
            if o:
                offers.append(o)
        return offers
    except Exception as e:
        print(f"[WARN] APEC '{query}' a échoué : {e}", file=sys.stderr)
        return []


# ============================================================
# SOURCE 6 : Adzuna (API officielle gratuite)
# ============================================================

def fetch_adzuna(query: str) -> list[dict]:
    app_id = os.environ.get("ADZUNA_APP_ID")
    app_key = os.environ.get("ADZUNA_APP_KEY")
    if not app_id or not app_key:
        return []
    try:
        resp = requests.get(
            "https://api.adzuna.com/v1/api/jobs/fr/search/1",
            params={
                "app_id": app_id, "app_key": app_key,
                "what": query, "where": "France",
                "results_per_page": 20, "sort_by": "date",
                "content-type": "application/json",
            },
            timeout=20,
        )
        resp.raise_for_status()
        offers = []
        for r in resp.json().get("results", []):
            o = make_offer(
                r.get("title"), (r.get("company") or {}).get("display_name"),
                (r.get("location") or {}).get("display_name"),
                r.get("redirect_url"), "Adzuna", r.get("created", "")[:10],
            )
            if o:
                offers.append(o)
        return offers
    except Exception as e:
        print(f"[WARN] Adzuna '{query}' a échoué : {e}", file=sys.stderr)
        return []


# ============================================================
# SOURCE 7 : France Travail (API officielle)
# ============================================================

def ft_token() -> str | None:
    cid = os.environ.get("FT_CLIENT_ID")
    csec = os.environ.get("FT_CLIENT_SECRET")
    if not cid or not csec:
        return None
    try:
        resp = requests.post(
            "https://entreprise.francetravail.fr/connexion/oauth2/access_token?realm=%2Fpartenaire",
            data={"grant_type": "client_credentials", "client_id": cid,
                  "client_secret": csec, "scope": "api_offresdemploiv2 o2dsoffre"},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=20,
        )
        resp.raise_for_status()
        return resp.json()["access_token"]
    except Exception as e:
        print(f"[WARN] Auth France Travail échouée : {e}", file=sys.stderr)
        return None


def fetch_france_travail(token: str, query: str) -> list[dict]:
    try:
        resp = requests.get(
            "https://api.francetravail.io/partenaire/offresdemploi/v2/offres/search",
            headers={"Authorization": f"Bearer {token}"},
            params={"motsCles": query, "typeContrat": "FS", "sort": "1", "range": "0-19"},
            timeout=20,
        )
        if resp.status_code == 204:
            return []
        resp.raise_for_status()
        offers = []
        for r in resp.json().get("resultats", []):
            url = (r.get("origineOffre") or {}).get("urlOrigine") or \
                  f"https://candidat.francetravail.fr/offres/recherche/detail/{r.get('id')}"
            o = make_offer(
                r.get("intitule"), (r.get("entreprise") or {}).get("nom"),
                (r.get("lieuTravail") or {}).get("libelle"),
                url, "France Travail", (r.get("dateCreation") or "")[:10],
                confirmed_internship=True,
            )
            if o:
                offers.append(o)
        return offers
    except Exception as e:
        print(f"[WARN] France Travail '{query}' a échoué : {e}", file=sys.stderr)
        return []


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
        parts.append(f"<h3>{src} ({len(lst)})</h3>")
        for o in lst:
            badge = " ✅ <b>visa-friendly probable</b>" if o["visa_friendly"] else ""
            parts.append(
                f"<p><a href='{o['url']}'><b>{o['title']}</b></a><br>"
                f"{o['company']} · {o['location']}{badge}</p>"
            )
    parts.append(f"<hr><p><a href='{dashboard_url}'>📊 Ouvrir le dashboard complet</a></p>")
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


def render_dashboard(offers: list[dict], new_ids: set) -> None:
    DOCS_INDEX.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
    sources = sorted({o["source"] for o in offers})
    repository = os.environ.get("GITHUB_REPOSITORY", "neyos10/radar-stage-france")
    actions_url = f"https://github.com/{repository}/actions/workflows/scan.yml"

    cards = []
    for o in sorted(offers, key=lambda x: x.get("date", ""), reverse=True)[:150]:
        is_new = o["id"] in new_ids
        published = datetime.fromisoformat(o["date"]).strftime("%d/%m/%Y")
        age = publication_age(o["date"])
        cards.append(f"""
      <article class="job {'is-new' if is_new else ''}" data-id="{o['id']}"
               data-src="{o['source']}" data-visa="{'1' if o['visa_friendly'] else '0'}">
        <div class="job-top"><div>
          <h3><a href="{o['url']}" target="_blank" rel="noopener">{o['title']}</a></h3>
          <div class="meta">{o['company']} · {o['location']}</div>
          <div class="published">Publiée {age} · {published}</div>
        </div>
        <button class="apply-btn" type="button" onclick="toggleApplied(event,'{o['id']}')">✓ Marquer comme postulé</button></div>
        <div class="tags">{'<span class="tag new">NOUVEAU</span>' if is_new else ''}
          {'<span class="tag visa">✓ visa-friendly</span>' if o['visa_friendly'] else ''}
          <span class="tag match">Profil CV</span><span class="tag src">{o['source']}</span></div>
      </article>""")

    filter_btns = "".join(
        f'<button class="fchip source-chip" onclick="setSource(this,\'{source}\')">'
        f'{source} <b>({sum(o["source"] == source for o in offers)})</b></button>'
        for source in SOURCE_NAMES
    )

    html = f"""<!DOCTYPE html>
<html lang="fr"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Radar Stage France — Dashboard</title>
<style>
:root{{--nuit:#101726;--carte:#1A2436;--encre:#E9EEF6;--encre2:#93A1B8;--maj:#6B5BFF;--saf:#F5B841;--ok:#3ED598}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--nuit);color:var(--encre);font:15px/1.5 system-ui,sans-serif;padding:26px 16px 70px}}
.wrap{{max-width:840px;margin:0 auto}}
h1{{font-size:26px}} .sub{{color:var(--encre2);font-size:13px;margin-top:6px}}
.hero{{display:flex;align-items:flex-start;justify-content:space-between;gap:18px;margin-bottom:14px}}
.scan-btn{{background:var(--maj);color:white;text-decoration:none;border-radius:10px;padding:9px 14px;font-size:13px;font-weight:700;white-space:nowrap}}
.profile-note{{background:rgba(107,91,255,.09);border:1px solid rgba(107,91,255,.25);color:#CFC8FF;border-radius:11px;padding:10px 13px;font-size:12.5px;margin-bottom:14px}}
.filter-panel{{background:rgba(255,255,255,.025);border:1px solid rgba(255,255,255,.06);border-radius:12px;padding:11px;margin-bottom:16px}}
.filter-group{{display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin:5px 0}}
.filter-label{{color:var(--encre2);font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;min-width:54px}}
.fchip{{border:1px solid rgba(255,255,255,.14);background:transparent;color:var(--encre2);border-radius:999px;padding:6px 13px;font-size:12.5px;cursor:pointer}}
.fchip.on{{background:rgba(107,91,255,.15);border-color:var(--maj);color:#CFC8FF}}
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
@media(max-width:620px){{.hero{{display:block}}.scan-btn{{display:inline-block;margin-top:12px}}.job-top{{display:block}}.apply-btn{{margin-top:10px}}.filter-label{{width:100%}}}}
</style></head>
<body><div class="wrap">
<div class="hero"><div><h1>🎯 Radar Stage France</h1>
<p class="sub">Dernier scan : {now} · {len(offers)} offres des {MAX_OFFER_AGE_DAYS} derniers jours · Sources : {", ".join(sources) if sources else "—"}</p></div>
<a class="scan-btn" href="{actions_url}" target="_blank" rel="noopener">↻ Relancer le scan</a></div>
<p class="profile-note">Ciblage CV : Data Engineering, Big Data, Spark/PySpark, ETL/Talend, Data Warehouse, BI et Power BI.</p>
<div class="filter-panel">
  <div class="filter-group"><span class="filter-label">Statut</span>
    <button class="fchip status-chip on" onclick="setStatus(this,'todo')">À postuler <b id="todoCount"></b></button>
    <button class="fchip status-chip" onclick="setStatus(this,'applied')">Déjà postulé <b id="appliedCount"></b></button>
    <button class="fchip status-chip" onclick="setStatus(this,'all')">Tous</button>
  </div>
  <div class="filter-group"><span class="filter-label">Source</span>
    <button class="fchip source-chip on" onclick="setSource(this,'*')">Toutes</button>{filter_btns}
  </div>
  <div class="filter-group"><span class="filter-label">Options</span>
    <button id="visaFilter" class="fchip" aria-pressed="false" onclick="toggleVisa(this)">✓ Visa-friendly probable</button>
  </div>
</div>
<p id="emptyState" class="empty" hidden>Aucune offre ne correspond à ces filtres.</p>
<div id="jobs">{"".join(cards)}</div>
<p class="storage-note">Le statut de candidature est enregistré uniquement dans ce navigateur et n'est jamais envoyé sur GitHub.</p>
</div>
<script>
const STORAGE_KEY='radar-stage-france-applied-v1';
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
  document.getElementById('emptyState').hidden=visible!==0;
}}
refresh();
</script>
</body></html>"""
    DOCS_INDEX.write_text(html, encoding="utf-8")


# ============================================================
# MAIN
# ============================================================

def main() -> None:
    all_offers: dict[str, dict] = {}

    # --- JobSpy : LinkedIn + Indeed + Glassdoor + Google Jobs ---
    for q in QUERIES:
        for o in fetch_jobspy(q):
            all_offers.setdefault(o["id"], o)

    # --- APEC ---
    for q in APEC_QUERIES:
        for o in fetch_apec(q):
            all_offers.setdefault(o["id"], o)

    # --- Adzuna ---
    for q in QUERIES:
        for o in fetch_adzuna(q):
            all_offers.setdefault(o["id"], o)

    # --- France Travail ---
    token = ft_token()
    if token:
        for q in FRANCE_TRAVAIL_QUERIES:
            for o in fetch_france_travail(token, q):
                all_offers.setdefault(o["id"], o)

    seen = load_seen()
    new_offers = [o for oid, o in all_offers.items() if oid not in seen]
    new_ids = {o["id"] for o in new_offers}

    print(f"Total : {len(all_offers)} offres · Nouvelles : {len(new_offers)}")
    by_src = {}
    for o in all_offers.values():
        by_src[o["source"]] = by_src.get(o["source"], 0) + 1
    for source in SOURCE_NAMES:
        print(f"  - {source}: {by_src.get(source, 0)}")

    dashboard_url = os.environ.get("DASHBOARD_URL", "")
    if new_offers and os.environ.get("SMTP_USER"):
        try:
            send_email(new_offers, dashboard_url)
            print("Email envoyé.")
        except Exception as e:
            print(f"[ERREUR] email : {e}", file=sys.stderr)

    render_dashboard(list(all_offers.values()), new_ids)
    save_seen(seen | set(all_offers.keys()))


if __name__ == "__main__":
    main()
