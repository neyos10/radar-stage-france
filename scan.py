#!/usr/bin/env python3
"""
Veille Stage France V2 — agrégateur multi-sources d'offres de stage Data/IA.

Sources :
  1. LinkedIn (offres publiques, sans compte)   — via JobSpy
  2. Indeed France                               — via JobSpy
  3. Glassdoor France                            — via JobSpy
  4. Google Jobs (agrège APEC, WTTJ, HelloWork…) — via JobSpy
  5. Adzuna (agrégateur officiel, API gratuite)  — si secrets fournis
  6. France Travail (API officielle)             — si secrets fournis

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
from datetime import datetime, timezone
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
    "stage data scientist",
    "stage machine learning",
    "stage business intelligence",
]

# Mots qui doivent apparaître dans le titre pour garder l'offre
# (filtre anti-bruit : les scrapers remontent parfois des CDI)
TITLE_MUST_MATCH = re.compile(r"\b(stage|stagiaire|intern|internship|pfe)\b", re.I)

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


def make_offer(title, company, location, url, source, date="") -> dict | None:
    title = (title or "").strip()
    if not title or not TITLE_MUST_MATCH.search(title):
        return None
    company = (company or "Entreprise non précisée").strip()
    return {
        "id": offer_id(url, title, company),
        "title": title,
        "company": company,
        "location": (location or "France").strip(),
        "url": url or "",
        "source": source,
        "date": str(date or ""),
        "visa_friendly": is_visa_friendly(company),
    }


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
# SOURCE 5 : Adzuna (API officielle gratuite)
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
# SOURCE 6 : France Travail (API officielle)
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
    parts.append("<p style='color:#888;font-size:12px'>Pipeline GitHub Actions · JobSpy + Adzuna + France Travail</p>")

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

    cards = []
    for o in sorted(offers, key=lambda x: (x["id"] not in new_ids, x.get("date", "")), reverse=False)[:150]:
        is_new = o["id"] in new_ids
        cards.append(f"""
      <div class="job {'is-new' if is_new else ''}" data-src="{o['source']}">
        <h3><a href="{o['url']}" target="_blank" rel="noopener">{o['title']}</a></h3>
        <div class="meta">{o['company']} · {o['location']}</div>
        <div class="tags">{'<span class="tag new">NOUVEAU</span>' if is_new else ''}
          {'<span class="tag visa">✓ visa-friendly</span>' if o['visa_friendly'] else ''}
          <span class="tag src">{o['source']}</span></div>
      </div>""")

    filter_btns = "".join(
        f'<button class="fchip" onclick="flt(this,\'{s}\')">{s}</button>' for s in sources
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
h1{{font-size:26px}} .sub{{color:var(--encre2);font-size:13px;margin:6px 0 16px}}
.filters{{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:16px}}
.fchip{{border:1px solid rgba(255,255,255,.14);background:transparent;color:var(--encre2);border-radius:999px;padding:6px 13px;font-size:12.5px;cursor:pointer}}
.fchip.on{{background:rgba(107,91,255,.15);border-color:var(--maj);color:#CFC8FF}}
.job{{background:var(--carte);border:1px solid rgba(255,255,255,.06);border-radius:13px;padding:14px 16px;margin-bottom:10px}}
.job.is-new{{border-left:3px solid var(--saf)}}
.job h3{{font-size:15px}} .job h3 a{{color:var(--encre);text-decoration:none}}
.job h3 a:hover{{text-decoration:underline}}
.meta{{color:var(--encre2);font-size:13px;margin-top:2px}}
.tags{{display:flex;gap:6px;margin-top:8px;flex-wrap:wrap}}
.tag{{font-size:10.5px;font-weight:700;border-radius:6px;padding:3px 8px}}
.tag.new{{background:var(--saf);color:#3A2C05}}
.tag.visa{{background:rgba(62,213,152,.15);color:var(--ok)}}
.tag.src{{background:#212D44;color:var(--encre2);font-weight:500}}
</style></head>
<body><div class="wrap">
<h1>🎯 Radar Stage France</h1>
<p class="sub">Dernier scan : {now} · {len(offers)} offres · Sources : {", ".join(sources) if sources else "—"}</p>
<div class="filters"><button class="fchip on" onclick="flt(this,'*')">Toutes</button>{filter_btns}</div>
{"".join(cards) if cards else "<p>Aucune offre trouvée pour l'instant — les campagnes PFE ouvrent en septembre.</p>"}
</div>
<script>
function flt(btn,src){{
  document.querySelectorAll('.fchip').forEach(b=>b.classList.remove('on'));
  btn.classList.add('on');
  document.querySelectorAll('.job').forEach(j=>{{
    j.style.display = (src==='*'||j.dataset.src===src) ? '' : 'none';
  }});
}}
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

    # --- Adzuna ---
    for q in QUERIES:
        for o in fetch_adzuna(q):
            all_offers.setdefault(o["id"], o)

    # --- France Travail ---
    token = ft_token()
    if token:
        for q in QUERIES:
            for o in fetch_france_travail(token, q):
                all_offers.setdefault(o["id"], o)

    seen = load_seen()
    new_offers = [o for oid, o in all_offers.items() if oid not in seen]
    new_ids = {o["id"] for o in new_offers}

    print(f"Total : {len(all_offers)} offres · Nouvelles : {len(new_offers)}")
    by_src = {}
    for o in all_offers.values():
        by_src[o["source"]] = by_src.get(o["source"], 0) + 1
    for s, n in sorted(by_src.items()):
        print(f"  - {s}: {n}")

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
