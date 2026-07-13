# 🎯 Radar Stage France V2 — LinkedIn + Indeed + Glassdoor + Google Jobs + APEC + Adzuna + France Travail

Pipeline 100% gratuit et autonome qui scanne **toutes les 30 minutes** les offres de stage
Data/IA en France sur 7 sources, les fusionne, détecte les nouveautés, et :
- t'envoie un **email** uniquement s'il y a du nouveau
- met à jour un **dashboard web** (GitHub Pages) où TOUTES les offres s'affichent,
  filtrables par source, avec badges NOUVEAU et visa-friendly

## 📡 Les sources

| Source | Comment | Clé nécessaire ? |
|---|---|---|
| **LinkedIn** | JobSpy — offres publiques, SANS compte (ton profil ne risque rien) | Non |
| **Indeed France** | JobSpy — le plus fiable, pas de rate-limit | Non |
| **Glassdoor France** | JobSpy | Non |
| **Google Jobs** | JobSpy — agrège WTTJ, HelloWork & co | Non |
| **APEC** | Recherche publique de stages cadres et jeunes diplômés | Non |
| **Adzuna** | API officielle gratuite — centaines de sites français agrégés | Oui (gratuite, 2 min) |
| **France Travail** | API officielle | Oui (gratuite, 5 min) |

⚠️ Honnêteté sur LinkedIn/Glassdoor : le scraping des pages publiques peut être
bloqué temporairement (erreur 429) selon l'humeur du site et l'IP GitHub. Le
script est conçu pour **continuer avec les autres sources** quand ça arrive.
Indeed, APEC, Adzuna et France Travail, eux, sont stables. Sur une semaine, tu ne
rates quasiment rien.

## 🚀 Installation (~15-20 min, une seule fois)

### 1. Le repo
Crée un repo GitHub (ex. `radar-stage-france`), uploade tous ces fichiers
(glisser-déposer sur github.com marche très bien — pense à inclure le dossier
caché `.github/`, plus simple : uploade le zip décompressé via "Add file →
Upload files").

### 2. Les clés (toutes gratuites)
- **Adzuna** : [developer.adzuna.com](https://developer.adzuna.com) → Register
  → récupère `app_id` et `app_key`
- **France Travail** : [francetravail.io](https://francetravail.io) → compte →
  créer une application → abonner à l'API "Offres d'emploi v2" → `client_id` + `client_secret`
- **Gmail** : active la validation 2 étapes puis crée un **mot de passe
  d'application** sur myaccount.google.com/apppasswords (16 caractères)

### 3. Les secrets GitHub
Repo → Settings → Secrets and variables → Actions → New repository secret :

| Secret | Valeur | Obligatoire |
|---|---|---|
| `SMTP_USER` | ton Gmail | ✅ (pour les emails) |
| `SMTP_PASS` | mot de passe d'application 16 car. | ✅ |
| `ALERT_TO` | adresse de réception (peut = SMTP_USER) | optionnel |
| `ADZUNA_APP_ID` / `ADZUNA_APP_KEY` | clés Adzuna | recommandé |
| `FT_CLIENT_ID` / `FT_CLIENT_SECRET` | clés France Travail | recommandé |

Sans les clés Adzuna/FT, le pipeline tourne quand même avec les 4 sources JobSpy et APEC.

### 4. Le dashboard public
Repo → Settings → Pages → Deploy from a branch → `main` + dossier `/docs` → Save.
Ton dashboard : `https://<ton-pseudo>.github.io/radar-stage-france/`
→ **mets-le en favori sur ton téléphone**, c'est là que tout s'affiche.

### 5. Premier test
Onglet Actions → "Veille Stage Data/IA France" → **Run workflow** → 2-3 min →
vérifie les logs, le dashboard, et ta boîte mail.

## ⚙️ Personnaliser
- Requêtes : liste `QUERIES` en haut de `scan.py`
- Heure du scan : ligne `cron:` dans `.github/workflows/scan.yml` (UTC)
- Entreprises visa-friendly : liste `VISA_FRIENDLY_KEYWORDS` dans `scan.py`
- Filtre anti-bruit : `TITLE_MUST_MATCH` impose un stage, puis la liste
  positive Data/IA et les exclusions fortes de `PROFILE_TITLE_HARD_EXCLUDE`
  écartent audit, comptabilité, RH, commercial, CRM, finance, etc.

## 🔎 Fraîcheur, filtres natifs et dashboard

- LinkedIn et Indeed utilisent `hours_old=168`, traduit par JobSpy en filtre
  natif des 7 derniers jours. Le filtre natif `job_type=internship` n'est pas
  cumulé : certaines entreprises classent leurs stages en temps plein, donc le
  type est validé localement avec `stage/stagiaire/intern/PFE` dans le titre.
  LinkedIn est borné à cinq requêtes complémentaires (pagination plus lente) ;
  Indeed conserve toutes les variantes, dont `"stage 6 mois" data engineer`.
- Une date relative (`« à l'instant »`, `« aujourd'hui »`, `« 2 hours ago »`)
  est normalisée. Une date absente ou inconnue est conservée par précaution et
  signalée par un badge ; seules les dates effectivement parsées au-delà de
  7 jours sont rejetées.
- APEC reçoit les filtres serveur `typesContrat=597171` (stage),
  `niveauEtude=20019` (Bac+5) et tous les types d'annonceur pertinents. Le code
  `20020` signifie strictement supérieur à Bac+5, il n'est donc pas utilisé
  pour un PFE d'école d'ingénieur.
- `Actualiser les offres` recharge le dernier dashboard publié. Un nouveau
  scan doit être lancé via `Ouvrir GitHub Actions` puis `Run workflow`, car une
  page GitHub Pages statique ne possède pas de jeton autorisant
  `workflow_dispatch`.
- Les statuts « À postuler / Déjà postulé » restent uniquement dans le
  `localStorage` du navigateur et ne sont jamais envoyés au repo.

## 🧠 À raconter en entretien (c'est un vrai projet data engineering)
Extraction multi-sources (APIs REST publique/OAuth2/key + scraping tolérant aux pannes),
normalisation et déduplication par hash, détection de delta contre un état
persisté versionné dans Git, notification SMTP conditionnelle, publication
statique automatisée — le tout orchestré en serverless par GitHub Actions cron.

## ⚖️ Note légale
Ce pipeline ne se connecte à AUCUN compte : il lit uniquement les pages
publiques (comme un navigateur) et des API officielles. Ne pas augmenter
massivement `results_wanted` ni la fréquence du cron — reste raisonnable,
un scan/jour suffit largement.
