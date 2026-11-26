# Architecture — Radar Stage France

État décrit : commit `5fe41bc` (tag `v-multi-domaine`), revu le 25/11/2026.
Les changements de la branche `perso-data-france-maroc` sont résumés en §6.

Fichiers : `scan.py` (pipeline, ~2 450 lignes), `radar_xray.py` (moteurs
X-ray pour les posts LinkedIn), `data/*.json` (catalogues + état persistant),
`docs/` (dashboard GitHub Pages), `.github/workflows/scan.yml`, `tests/`
(163 tests `unittest`, sans réseau, tous verts au départ).

## 1. Flux d'un run (`python scan.py`)

```
catalogues (config/domains/regions.json)
  → run_all_sources : 7 sources en parallèle (ThreadPool, 4 workers),
    régions séquentielles À L'INTÉRIEUR d'une source
  → make_offer : titre « stage/intern/PFE… » → date ≤ 24 h → scoring domaine
    → dédoublonnage (id = sha1(url|titre|entreprise)), fusion multi-domaine/région
  → seen_offers.json : delta « nouvelles offres »
  → reconcile_offer_store : fusion dans offers.json (first_seen, last_seen_*, active)
  → dashboard_offers_from_store : sélection des offres visibles
  → source_status.json, email SMTP (si secrets), docs/index.html
  → le workflow committe data/ + docs/ sur main (rebase + 3 essais, jamais de force push)
```

## 2. Sources

| Source | Accès | État réel (dernier run prod, 19/07/2026) |
|---|---|---|
| LinkedIn | JobSpy, `hours_old=24`, pagination jusqu'à 1000 | fonctionne (≈10 s/requête) |
| Indeed | JobSpy, `country_indeed` par région | fonctionne, faible volume |
| APEC | API JSON publique `rechercheOffre`, filtres stage + Bac+5 | recherche OK ; lecture de détail bloquée en 403/429 (circuit breaker dédié) |
| Adzuna | API officielle, index `/fr`, `max_days_old=1` | fonctionne : secrets **déjà présents** dans le dépôt |
| France Travail | API OAuth2 Offres v2, `natureContrat=FS`, `publieeDepuis=1` | fonctionne : secrets **déjà présents** |
| The Muse | API publique, `level=Internship`, clé optionnelle | fonctionne (France uniquement) |
| LinkedIn Posts | X-ray : Tavily → Exa → DuckDuckGo, jamais linkedin.com | bloqué au dernier run (crédits Exa épuisés, DDG en anti-bot) ; Tavily ajouté juste après, jamais vu en prod. Créneaux 6 h et 18 h UTC uniquement |
| Glassdoor, Google Jobs, Jooble | — | **retirés du code** en juillet. Retesté le 25/11/2026 : Glassdoor HTTP 400, Google 0 résultat |

APEC, France Travail, Adzuna et The Muse renvoient `not_applicable_region`
hors France, jamais une erreur.

## 3. Mécanismes clés

- **Filtre contrat** : `TITLE_MUST_MATCH` (stage, stagiaire, intern,
  internship, pfa, pfe) sauf si l'API certifie le stage (APEC, FT, The Muse).
- **Scoring par domaine** : +3 par mot positif dans le titre, +1 dans la
  description, −4 par négatif dans le titre, −1 dans la description ; chaque
  mot compte une fois par zone ; seuil 2 ; multi-label. Il n'existe **pas** de
  règle « 3 mots en description » : 2 mots positifs en description suffisaient.
- **Fraîcheur** : 24 h glissantes. Les sources dont le filtre natif garantit
  la fraîcheur posent `freshness_verified_at` ; date inconnue = conservée et
  badgée « date inconnue ».
- **Priorités** : seuls les domaines ont une `priority`, utilisée par
  `linkedin_queries`. Comme `MAX_LINKEDIN_QUERIES = 1000`, toutes les requêtes
  passent : la priorité ne change que l'ordre (et donc quelles 3 requêtes
  reçoivent la description complète). **Aucune priorité de région** n'existe.
- **Budget** : `calculate_scan_budget` n'est plus qu'informatif (`scale`
  toujours 1.0, simple avertissement au-delà de 6 combinaisons). Plafonds
  techniques : 1000 résultats JobSpy, 100 pages par API, arrêt après 2 pages
  sans nouveauté.
- **Circuit breaker** : 2 blocages 403/429 consécutifs arrêtent la source pour
  le run courant (JobSpy, Adzuna, FT, The Muse, LinkedIn Posts, détail APEC).
  Les 403 avalés par JobSpy sont détectés dans ses logs.
- **Store `offers.json`** : une absence après un run sain passe l'offre en
  `active=false` ; une absence due à une source bloquée ne change rien ; une
  source rotative (LinkedIn Posts) ne confirme jamais une absence. Purge des
  inactives confirmées après 14 jours (`days_window` + 7).
- **Dashboard** : HTML statique, CSP stricte (`script-src 'self'`), tout est
  échappé, URL http(s) seulement, filtres côté client, « postulé » en
  `localStorage`.

## 4. Prévu vs réel

| Fonction | Statut |
|---|---|
| Profils `fast` / `full` | **Absent de main.** Implémenté sur `agent/fast-full-profiles` (commit `5b3f53c`, 16/07), jamais fusionné, très divergent (+12 000 lignes) |
| Ingestion des alertes Gmail | **Absent**, sur aucune branche |
| Posts LinkedIn X-ray | **Implémenté** (Tavily/Exa/DDG, rotation, 2 créneaux/jour), jamais productif en prod à ce jour |
| Email d'alerte | Implémenté, **inactif** : secrets SMTP absents |
| Cron horaire | **Suspendu** depuis le 20/07 (paiement GitHub Actions en échec) |

## 5. Incohérences, code mort, bugs

1. **Fenêtre affichée ≠ fenêtre réelle** : le dashboard annonce « 7 derniers
   jours », mais une offre JobSpy disparaît dès qu'elle sort des 24 h de
   `hours_old` (absence « confirmée » à tort). En pratique : moins de 24 h de visibilité.
2. **Fuite du store** : une offre d'une région désactivée, ou d'une source
   bloquée en permanence ou rotative, n'est jamais confirmée absente, donc
   jamais purgée.
3. `save_seen` garde `sorted(ids)[-5000:]` : un tri sur des hash, donc la
   troncature supprime des id au hasard plutôt que les plus anciens.
4. Badge entreprise : correspondance par sous-chaîne (`octo` ⊂ « Octopus »,
   `axa` ⊂ « Saxa »…).
5. Groupe Domaine pas masqué avec un seul domaine (CSS `opacity: .82`, pas
   `display: none`), contrairement au groupe Région.
6. Le test `test_workflow_runs_hourly…` passe alors que le cron est commenté
   (il cherche la chaîne dans le fichier, commentaires compris).
7. Code mort : `merge_offer_domains` (alias), `data/jooble_rotation.json`,
   secret `JOOBLE_API_KEY`, champ `ScanBudget.scale`, `SCAN_*_ESTIMATE`.
8. README périmé : « LinkedIn limité à six requêtes », « results_wanted
   réduit au-delà de 6 combinaisons », « purge après 14 jours » (purge
   conditionnelle), `hours_old selon days_window` (c'est 24 h fixe).
9. Coût : 17 domaines × 4 régions = 87 requêtes, run de 43 min. Le dépôt
   étant privé, un run horaire consomme environ 30 000 min/mois, bien au-delà
   des 2 000 min gratuites. C'est la cause probable de l'échec de paiement.
10. Libellés : « 1 domaines », « Automatisation active · toutes les heures »
    affiché alors que le cron est suspendu.

Constatés pendant les runs réels du 25/11/2026 :

11. JobSpy 1.1.82 ne lit plus aucune date LinkedIn (`date_posted = None` sur
    100 % des offres). Comme `sort_date` était réécrit à chaque run pour les
    dates inconnues, le tri « plus récent d'abord » ne voulait plus rien dire.
12. Une offre JobSpy sans ville était localisée « France » en dur, même au
    Maroc, et une ville `NaN` donnait la chaîne « nan ».
13. La recherche APEC n'avait pas de circuit breaker : sur des 403, elle
    tentait toutes les requêtes.
14. Le store n'est pas re-scoré quand le catalogue change : une offre
    acceptée reste affichée jusqu'à sa sortie de fenêtre.
15. Depuis le poste local, APEC (y compris la page d'accueil) et The Muse
    répondent par un challenge anti-bot (403 / 404). À confirmer depuis les
    runners GitHub.

## 6. Branche `perso-data-france-maroc`

Périmètre : `data_ia` seul, régions `france` + `maroc` (les autres restent dans
les catalogues avec `enabled: false`). Retour à la version multi-domaine :
`git checkout v-multi-domaine`.

| # | Statut sur la branche |
|---|---|
| 1 | **Corrigé** : affichage sur 7 jours (publication, ou 1re observation si date inconnue) ; seules APEC et The Muse (catalogue complet) masquent plus tôt une offre absente |
| 2 | **Corrigé** : purge de toute entrée non revue depuis 14 jours ; masquage à l'affichage des offres de domaines ou régions inactifs |
| 3 | **Corrigé** : ordre d'arrivée conservé |
| 4 | **Corrigé** : correspondance par mot entier, badge limité à la France (`highlight_regions`) |
| 5 | **Corrigé** |
| 6 | **Corrigé** : le test ne lit que les lignes actives |
| 7 | Laissé en l'état (inoffensif ; `JOOBLE_API_KEY` peut être supprimé des secrets) |
| 8 | **Corrigé** dans le README |
| 9 | Réduit : 1 × 2 combinaisons, ~11 min, cron 7 h–22 h, `timeout-minutes: 30`. Le dépôt privé reste au-dessus du quota gratuit (voir le workflow) |
| 10 | **Corrigé** |
| 11 | **Contourné** : pour une date inconnue, `sort_date` = 1re observation (offre la plus récemment découverte en tête). La lecture de date LinkedIn reste cassée côté JobSpy |
| 12 | **Corrigé** (libellé de la région, `clean_text`) |
| 13 | **Corrigé** |
| 14 | Laissé (nettoyage ponctuel des 7 entrées concernées lors de la recalibration) |
| 15 | Hors code |

Calibration `data_ia` : vocabulaire demandé + variantes mesurées sur le run
réel (data management, gouvernance des données, analytics engineer, analyste
données, computer vision, AI engineer, « stage data »…) ; 3 mots positifs
minimum en description seule ; négatifs titre seul pour le bruit « IA »
observé (commerciale, SEO, Salesforce, full stack, Java, documentation).
Descriptions LinkedIn limitées à la 1re requête (145 s contre 50 s par
requête). Variantes anglaises au Maroc mesurées à 0 offre acceptée, donc
retirées (`linkedin_extra_queries: []`).

### Mise à jour du 26/11/2026 — sources

- Runs GitHub Actions : APEC et The Muse répondent 403 (anti-bot) aussi
  depuis les runners. Désactivées via `disabled_sources`, code conservé.
- Nouvelles sources Maroc : **Dreamjob** (RSS « Stages ») et **Sites Maroc**
  (Stagiaires.ma, ReKrute, Emploi.ma via l'index Tavily). Premier test réel :
  2 + 5 offres PFE IT/data au Maroc, contre 6 pour LinkedIn + Indeed réunis.
- LinkedIn Posts : nouveau format de requête Tavily et fenêtre d'une
  semaine. Rendement toujours quasi nul (posts peu indexés par les moteurs).
- `TITLE_MUST_MATCH` accepte les pluriels (stagiaires, stages, interns).
- Les identifiants d'API sont masqués dans toutes les erreurs enregistrées
  (`safe_error`) : une clé Adzuna avait fuité dans `source_status.json`.
