# 🎯 Radar Stage France

Radar autonome de stages récents, multi-sources, multi-domaines et
multi-régions. Il collecte les offres publiques, normalise les dates en UTC,
classe chaque offre dans un ou plusieurs domaines, déduplique les résultats,
conserve un historique sûr, envoie éventuellement un email et régénère un
dashboard GitHub Pages.

La collecte possède désormais deux profils explicites. Ils utilisent le même
store, mais leurs périmètres et leurs métriques restent séparés.

## Profils de scan

| Profil | Domaine | Régions | Fenêtre glissante | Usage |
|---|---|---|---:|---|
| `fast_data_ia` | Data / IA | France | 2 heures | priorité personnelle, faible latence |
| `full_general` | tous les domaines actifs | toutes les régions actives | 24 heures | couverture générale |

La fenêtre est recalculée à chaque exécution depuis une unique horloge UTC :
aucune date n'est fixée en dur. La sélection du profil suit l'ordre
`--profile` → `RADAR_PROFILE` → `full_general`, ce qui préserve le comportement
des anciens appels sans argument.

```bash
python radar.py --profile fast_data_ia
python radar.py --profile full_general
```

`data/profiles.json` contient le périmètre métier et les garde-fous techniques
de chaque profil : sources, fenêtre, pagination, requêtes, détails, timeout et
parallélisme. Les surcharges temporaires restent disponibles sans modifier les
catalogues versionnés :

Le FULL borne LinkedIn à 24 requêtes par run : les 16 domaines reçoivent
d'abord chacun une requête, puis huit créneaux sont distribués selon les
priorités du catalogue. Par rapport aux 82 requêtes LinkedIn du baseline, cela
réduit de 70,7 % ce poste réseau dominant sans retirer aucun domaine. Comme la
couverture des variantes n'est pas exhaustive, ce statut est marqué partiel et
ne peut jamais servir de preuve d'absence dans le store. Les autres sources
conservent l'ensemble de leurs requêtes dédupliquées.

Le premier benchmark FAST réel a montré que l'enrichissement des descriptions
LinkedIn dominait largement la durée, alors que ses six requêtes et les titres
sont déjà explicitement Data/IA. FAST désactive donc cet enrichissement global
coûteux (`detail_budgets.LinkedIn = 0`) sans modifier les poids, le seuil ni le
catalogue de mots-clés du scoring ; FULL conserve trois requêtes enrichies pour
la couverture généraliste.

```bash
RADAR_DOMAINS=data_ia RADAR_REGIONS=france \
RADAR_SOURCES=LinkedIn,Indeed RADAR_WINDOW_HOURS=2 \
python radar.py --profile full_general
```

## Sources

| Source | FAST | FULL | Accès |
|---|:---:|:---:|---|
| LinkedIn Jobs | ✓ | ✓ | pages publiques via JobSpy |
| Indeed | ✓ | ✓ | pages publiques via JobSpy |
| Google Jobs | — | ✓ | agrégateur via JobSpy, notamment WTTJ/HelloWork |
| APEC | ✓ | ✓ | recherche publique France |
| Adzuna | ✓ | ✓ | API France, identifiants optionnels |
| France Travail | ✓ | ✓ | API officielle, identifiants optionnels |
| LinkedIn Posts | ✓ | ✓ | X-ray search best effort, sans appel direct à LinkedIn |

Glassdoor est retiré des profils et n'est plus interrogé. Les anciennes offres
de cette source restent migrables pour préserver leur identité, mais sont
marquées comme issues d'une source retirée au lieu d'être supprimées
brutalement.

Chaque connecteur est isolé. Un timeout, une absence de secret ou un blocage
403/429 n'arrête pas les autres sources. Le circuit breaker évite de marteler
une plateforme bloquée. La source « LinkedIn Posts » n'ouvre jamais LinkedIn :
elle interroge un moteur de recherche autorisé, valide uniquement des URLs de
posts publics et se désactive proprement si le moteur présente un anti-bot.

## Fonctionnement

```text
profil + catalogues
        │
        ▼
collecteurs bornés ──► normalisation UTC ──► scoring multi-domaines
        │                                      │
        └──────── métriques par source         ▼
                              déduplication exacte puis probable
                                                │
                                                ▼
                 reconciliation consciente du profil + store atomique
                                                │
                     ┌──────────────────────────┼─────────────────────┐
                     ▼                          ▼                     ▼
                  email                    dashboard             diagnostics
```

Le classifieur conserve sa calibration : +3 par terme positif dans le titre,
+1 dans la description, −4 par terme négatif dans le titre et −1 dans la
description. Le seuil reste 2. Une offre peut porter plusieurs domaines ; les
mots-clés positifs réellement trouvés et le score sont conservés pour expliquer
le classement.

Le filtre de contrat exige `stage`, `stagiaire`, `intern`, `internship`, `PFA`
ou `PFE` dans le titre, sauf si une API certifie déjà la nature stage. Les dates
relatives sont converties vers UTC. Une date absente n'est jamais inventée :
elle est signalée et suit la règle prudente historique.

La déduplication utilise d'abord les identifiants externes et les URLs
canoniques, puis une empreinte probable normalisée poste/entreprise/lieu. Une
fusion enrichit les sources et métadonnées sans changer l'identifiant historique
de l'offre.

## Persistance sûre

- `data/offers.json` : store partagé, identité, profil de dernière observation,
  compteurs d'absence et état d'expiration probable ;
- `data/seen_offers.json` : mémoire horodatée des offres déjà annoncées, migrée
  automatiquement depuis l'ancien tableau d'identifiants et purgée après
  45 jours ;
- `data/source_status_fast_data_ia.json` et
  `data/source_status_full_general.json` : diagnostics indépendants ;
- `data/source_status.json` : alias rétrocompatible du dernier statut ;
- `data/scan_metrics.json` : historique borné des 50 derniers runs par profil ;
- `data/xray_state.json` : curseur de rotation X-ray séparé par profil ;
- `docs/index.html` : dashboard GitHub Pages.

Les écritures importantes passent par un fichier temporaire, sont synchronisées
et remplacées atomiquement. Un scan partiel ne peut pas désactiver une offre
hors de ses domaines, régions ou sources. Une offre n'est considérée comme
probablement expirée qu'après au moins 72 heures et trois absences pertinentes
confirmées par des runs sains ; une redécouverte réinitialise ces compteurs.

## Dashboard

Le dashboard reste statique et responsive. Il présente séparément le dernier
FAST et le dernier FULL, la santé des connecteurs et les nouvelles offres de
chaque profil. Les fonctions existantes sont conservées :

- recherche poste / entreprise / ville ;
- filtres par candidature, source, domaine, région et entreprise mise en avant ;
- tri par date ou pertinence ;
- mots-clés qui expliquent le classement ;
- section séparée pour les offres probablement expirées ;
- bouton « Marquer comme postulé » stocké uniquement dans le `localStorage` du
  navigateur.

Le navigateur n'envoie aucun statut de candidature au dépôt.

## Installation

1. Publier le projet dans un dépôt GitHub.
2. Dans **Settings → Pages**, choisir `Deploy from a branch`, branche `main`,
   dossier `/docs`.
3. Dans **Settings → Secrets and variables → Actions**, créer les secrets
   nécessaires.

| Secret | Usage | Obligatoire |
|---|---|---|
| `FT_CLIENT_ID` | OAuth France Travail | seulement pour France Travail |
| `FT_CLIENT_SECRET` | OAuth France Travail | seulement pour France Travail |
| `ADZUNA_APP_ID` | API Adzuna France | seulement pour Adzuna |
| `ADZUNA_APP_KEY` | API Adzuna France | seulement pour Adzuna |
| `SMTP_USER` | compte SMTP expéditeur | seulement pour les emails |
| `SMTP_PASS` | mot de passe d'application SMTP | seulement pour les emails |
| `ALERT_TO` | destinataire, sinon `SMTP_USER` | non |

Sans secrets optionnels, les connecteurs concernés apparaissent « non
configurés » et le reste du scan continue. Aucun secret n'est écrit dans les
JSON, le dashboard ou les logs.

### Activer France Travail

1. Créer un compte sur le portail officiel
   [francetravail.io](https://francetravail.io/inscription).
2. Ouvrir le produit officiel
   [API Offres d'emploi](https://francetravail.io/produits-partages/catalogue/offres-emploi),
   choisir **Utiliser l'API**, puis créer une application et demander l'accès
   au produit. Le portail fournit alors l'identifiant client et le secret
   client de l'application. L'API annonce notamment la recherche paginée et
   une limite de 10 appels par seconde ; le radar reste en dessous grâce à ses
   budgets et timeouts.
3. Dans le dépôt GitHub, ouvrir **Settings → Secrets and variables → Actions →
   New repository secret**, puis créer exactement `FT_CLIENT_ID` et
   `FT_CLIENT_SECRET` avec les deux valeurs fournies. Ne les placer dans aucun
   fichier du dépôt.

Le radar demande un jeton OAuth une seule fois par run France Travail, avec le
scope `api_offresdemploiv2 o2dsoffre`, puis utilise le filtre stage
`natureContrat=FS` et le filtre journalier natif avant son contrôle UTC exact.

### Activer Adzuna

1. S'inscrire sur le portail développeur officiel
   [Adzuna API](https://developer.adzuna.com/signup) et décrire l'usage du
   projet. L'inscription fournit un `app_id` et un `app_key`.
2. Dans **Settings → Secrets and variables → Actions**, créer exactement
   `ADZUNA_APP_ID` et `ADZUNA_APP_KEY`.

Le connecteur utilise exclusivement l'index `/jobs/fr`, les paramètres
officiels `app_id`/`app_key`, le tri par date et `max_days_old=1`, puis applique
la fenêtre FAST 2 h ou FULL 24 h en UTC côté Python.

## GitHub Actions

Le workflow utilise un seul job Linux, sans matrice. FAST et FULL partagent le
verrou :

```yaml
concurrency:
  group: radar-stage-store
  cancel-in-progress: false
```

Le bouton **Run workflow** permet de choisir `fast_data_ia` ou `full_general`.
Le cron historique unique est conservé temporairement avec `full_general` par
défaut pour la rétrocompatibilité. **Aucune cadence finale FAST/FULL n'est
encore définie** : elle sera décidée après plusieurs mesures p95 et projection
du budget. Le commit automatique inclut tous les stores, statuts, métriques et
le dashboard générés, avec rebase et trois tentatives sans force-push.

## Benchmark sans effet de bord

Un benchmark collecte et produit des métriques, mais n'envoie pas d'email, ne
marque aucune offre comme nouvelle et ne modifie ni store, ni mémoire, ni
dashboard :

```bash
python radar.py --profile fast_data_ia --benchmark \
  --benchmark-output /tmp/fast.json
python radar.py --profile full_general --benchmark \
  --benchmark-output /tmp/full.json
```

Pour enchaîner les deux profils :

```bash
python scripts/benchmark_profiles.py
```

La méthode et le baseline historique reconstitué sont documentés dans
[`benchmarks/README.md`](benchmarks/README.md). Les rapports distinguent les
mesures réelles des champs indisponibles ; un blocage réseau n'est jamais
présenté comme une réussite.

Pour JobSpy, `network_request_count` est une estimation prudente reconstruite
depuis les pages et les logs exposés par la bibliothèque ; ses appels HTTP
internes d'enrichissement LinkedIn ne sont pas tous observables. Les compteurs
APEC, Adzuna, France Travail et X-ray sont, eux, incrémentés explicitement par
le radar.

## Budget GitHub Actions

Le calculateur applique l'arrondi facturable prudent à la minute supérieure,
un quota de 3 000 minutes et une réserve minimale de 50 minutes, soit au plus
2 950 minutes planifiables :

```bash
python budget.py \
  --fast-p95-seconds 120 --fast-runs 100 \
  --full-p95-seconds 900 --full-runs 10 \
  --other-workflows-minutes 100
```

Les valeurs sont des entrées de scénario, pas des fréquences recommandées. Les
horaires définitifs ne doivent être choisis qu'avec des p95 mesurés sur
plusieurs runs réels.

## Vérification locale

```bash
python -m py_compile radar.py scan.py radar_io.py radar_profiles.py radar_store.py radar_xray.py budget.py
python -m unittest discover -s tests -v
```

La suite est sans appel réseau réel. Les collecteurs sont simulés avec des
fixtures pour tester profils, fenêtres, pagination, déduplication,
réconciliation, expiration, benchmark, écritures atomiques, workflow et
calculateur de budget.
