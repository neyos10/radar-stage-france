# 🎯 Radar Stage France

Pipeline autonome qui collecte toutes les heures des offres de stage dans
les régions activées, les classe dans un ou plusieurs domaines, détecte les
nouveautés, envoie une alerte email optionnelle et publie un dashboard sur
GitHub Pages.

Architecture, état réel des sources et bugs connus : voir
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Usage personnel : stage PFE Data, France + Maroc

La branche `perso-data-france-maroc` ramène l'outil à son usage d'origine :
un radar personnel pour un stage PFE Data (début 2027, 4 à 6 mois).

- **Domaine** : `data_ia` seul (`enabled: true`). Les 16 autres domaines
  restent dans `data/domains.json` avec `enabled: false`.
- **Régions** : `france` et `maroc` actives, traitées à égalité (aucune
  région n'est bridée ; il n'existe pas de priorité par région dans le
  code). Belgique et Luxembourg restent dans `data/regions.json`, inactives.
- **Sources** : France → LinkedIn, Indeed, APEC, Adzuna, France Travail,
  The Muse, LinkedIn Posts ; Maroc → LinkedIn (`Morocco`), Indeed
  (`country_indeed: "Morocco"`, ma.indeed.com), LinkedIn Posts. APEC, Adzuna
  (index `/fr`), France Travail et The Muse sont `not_applicable_region` au
  Maroc. Glassdoor et Google Jobs restent retirés : retestés le 25/11/2026,
  ils répondent toujours HTTP 400 et 0 résultat.
- **Requêtes Maroc** : les requêtes françaises du domaine. Les variantes
  anglaises testées sur LinkedIn Maroc (« data engineer intern », « data
  internship », « PFE data ») ont rapporté 0 offre acceptée pour ~50 s de
  scan : retirées, mais le mécanisme reste
  (`regions.json` → `linkedin_extra_queries`). Le rendement de chaque requête
  est consigné dans `source_status.json` (`query_yield`).
- **Durée** : ~11 min en local, dominée par LinkedIn France (~60 s par
  requête à cause de l'espacement des pages imposé par JobSpy ; ~145 s pour
  la requête qui récupère les descriptions).
- **Scoring data_ia** : un mot positif dans le titre, ou au moins **3** mots
  positifs distincts en description seule (`description_only_min_positives`).
  « recrutement » n'est pénalisé que dans le titre (`negative_title_only`),
  « marketing » l'est sauf dans « marketing analytics »
  (`negative_exceptions`). Les autres domaines gardent leur calibration
  historique.
- **Dashboard** : région « Toutes » par défaut, badge région sur chaque
  carte, groupe Domaine masqué (un seul domaine). Le badge
  « Visa-friendly probable » n'apparaît que sur les offres en France
  (`config.json` → `highlight_regions`).
- **Fenêtre** : la collecte reste limitée aux 24 dernières heures ; le
  dashboard affiche les offres publiées (ou vues pour la première fois) dans
  les 7 derniers jours (`days_window`). Seules APEC et The Muse, qui
  renvoient la liste complète des offres ouvertes, peuvent masquer une offre
  plus tôt (absence = offre fermée).
- **Store** : les offres rattachées uniquement à des domaines ou régions
  désactivés sont masquées à l'affichage (pas supprimées). Toute entrée
  qu'aucun run n'a revue depuis 14 jours est purgée.
- **Planification** : un seul scan, `7 6-21 * * *` UTC, soit 7h07–22h07 à
  Paris en hiver. En été, passer à `7 5-20 * * *` (détail dans
  `.github/workflows/scan.yml`).

Retour à la version multi-domaine : `git checkout v-multi-domaine`.

## Sources

Les sept sources actives sont LinkedIn et Indeed via JobSpy, APEC, Adzuna,
The Muse, France Travail et LinkedIn Posts. Chaque source est isolée :
un blocage 403/429, un timeout ou l'absence d'une clé API n'arrête pas les
autres connecteurs.

Glassdoor, Google Jobs et Jooble ont été retirés du code : sur plusieurs
runs de production consécutifs, ils ont produit 0 offre acceptée dans 100 %
des cas (Glassdoor bloqué par son anti-bot, Google Jobs répondant vide sans
erreur, Jooble sans aucun filtre de type de contrat — 137/139 de ses
résultats n'étaient réellement pas des stages) tout en consommant du temps
de scan. L'historique Git conserve les implémentations si l'un d'eux
redevient exploitable un jour.

## Configuration produit

Le code métier n'est pas personnalisé dans `scan.py` :

- `data/domains.json` contient les 17 domaines (seul `data_ia` est actif sur
  cette branche), leurs requêtes, leurs vocabulaires positifs/négatifs et leur
  priorité de collecte optionnelle.
- `data/regions.json` contient les paramètres natifs de localisation. France
  et Maroc sont actives ; `enabled` active ou retire une région sans modifier
  le code.
  Les valeurs `country_indeed` confirmées pour `python-jobspy==1.1.82` sont
  exactement `France`, `Belgium`, `Luxembourg` et `Morocco` dans la
  [documentation officielle JobSpy](https://pypi.org/project/python-jobspy/).
- `data/config.json` contrôle le nom, la tagline, la fenêtre de fraîcheur et le
  badge d'entreprises mises en avant.
- les requêtes strictement identiques entre domaines actifs sont mutualisées ;
  les domaines désactivés ne déclenchent aucun appel réseau.

Le domaine `marketing_communication` a été ajouté après un échantillonnage réel
des offres LinkedIn Maroc classées « aucun domaine » (`REASON_NO_DOMAIN`) : sur
9 intitulés de stage confirmés extraits en direct via JobSpy le 17 juillet
2026, 6 ne matchaient aucun des 16 domaines précédents, dont 3 étaient des
stages marketing/communication (« Stagiaire Activations & Marketing »,
« Stagiaire en Marketing Digital », « Digital Marketing Intern ») — un
vocabulaire absent du catalogue et même explicitement placé en négatif dans
plusieurs domaines existants. Le même échantillon a aussi mis en évidence deux
mots-clés manquants dans des domaines existants, corrigés dans le même
correctif : « data analysis » (`data_ia`) et « business developer »
(`commerce_bizdev`), ainsi qu'une variante RH moderne, « people & culture »
(`rh`).

Le classifieur attribue par domaine +3 par terme positif dans le titre, +1 dans
la description, -4 par terme négatif dans le titre et -1 dans la description.
Une offre est retenue à partir du score 2 et peut porter plusieurs domaines ;
sans mot positif dans le titre, la description doit en citer au moins
`description_only_min_positives` distincts (2 par défaut, 3 pour `data_ia`).
Le filtre de contrat reste indépendant et exige `stage`, `stagiaire`, `intern`,
`internship`, `PFA` ou `PFE` dans le titre, sauf quand l'API certifie déjà le
stage.

Le dashboard responsive propose une vue synthétique du dernier scan, la santé
des connecteurs, une recherche par poste/entreprise/ville et des filtres
combinables par statut de candidature, source, domaine, région et entreprise
mise en avant. La région par défaut est « Toutes » dès que plusieurs régions
sont actives. Les groupes Région et Domaine sont masqués lorsqu'une seule
valeur est active. Le statut « postulé » reste dans le `localStorage` du navigateur et
n'est jamais envoyé à GitHub.

`data/offers.json` conserve le contenu complet des offres et leurs dates de
première/dernière observation. Une offre ne disparaît pas si son connecteur est
temporairement bloqué. Elle reste affichée 7 jours après sa publication (ou sa
première observation si la date est inconnue), sauf absence confirmée par APEC
ou The Muse. Les entrées inactives confirmées et celles qu'aucun run n'a revues
depuis 14 jours sont purgées.

## Installation

1. Publier le projet dans un dépôt GitHub public.
2. Dans Settings → Pages, choisir `Deploy from a branch`, branche `main`,
   dossier `/docs`.
3. Ajouter dans Settings → Secrets and variables → Actions les secrets utiles :

| Secret | Usage | Obligatoire |
|---|---|---|
| `SMTP_USER` | compte SMTP expéditeur | pour les emails |
| `SMTP_PASS` | mot de passe d'application | pour les emails |
| `ALERT_TO` | destinataire, sinon `SMTP_USER` | non |
| `ADZUNA_APP_ID` / `ADZUNA_APP_KEY` | API Adzuna | non |
| `THEMUSE_API_KEY` | API The Muse (relève la limite à 3600 req/h) | non — 500 req/h sans elle |
| `FT_CLIENT_ID` / `FT_CLIENT_SECRET` | API Offres d'emploi v2 | non |
| `TAVILY_API_KEY` | API Tavily pour LinkedIn Posts (1000 recherches/mois gratuites) | non |
| `EXA_API_KEY` | API Exa pour LinkedIn Posts (repli si Tavily absent/épuisé) | non |

Sans identifiants optionnels, leur source apparaît « non configurée » et le
reste du scan continue normalement. The Muse fonctionne même sans clé
(500 requêtes/heure, largement suffisant pour un run).

Le scan tourne automatiquement chaque heure de 7 h à 22 h (Paris). Pour un run immédiat, utiliser
**Run workflow** dans l'onglet GitHub Actions du dépôt. Le bouton
**Actualiser les offres** recharge uniquement le dernier dashboard publié ; le
site ne demande et ne conserve aucun token GitHub.

## Fraîcheur et robustesse

- JobSpy reçoit `hours_old=24` (fenêtre de collecte fixe). Les dates relatives courantes
  sont normalisées ; une date inconnue est conservée et signalée, une date
  réellement trop ancienne est rejetée et journalisée.
- APEC utilise les filtres serveur stage (`typesContrat=597171`) et Bac+5
  (`niveauEtude=20019`), puis un budget borné de lectures de détail. Si cet
  endpoint détail bloque en 403/429, un circuit breaker dédié arrête les
  lectures restantes du run au lieu de répéter des échecs identiques.
  Comme The Muse, la recherche APEC ne liste que des offres
  actuellement ouvertes : leur fraîcheur est considérée vérifiée côté source
  (`freshness_verified`), une date de publication ancienne ne vaut jamais
  rejet « plus de 24 heures ».
- France Travail utilise `natureContrat=FS` et la fenêtre configurée.
- The Muse utilise le filtre serveur natif `level=Internship` (confirmation
  équivalente au filtre stage d'APEC) : aucune requête par mot-clé n'est
  nécessaire, une seule pagination par run couvre tout le catalogue pour la
  région, puis le scoring par domaine s'applique localement comme pour APEC.
  L'API ne propose aucun paramètre `hours_old` : la fraîcheur n'est donc pas
  revérifiée localement (`freshness_verified`, même mécanique que JobSpy) —
  une offre "Internship" toujours listée par l'API est traitée comme
  toujours active, indépendamment de sa date de publication d'origine.
- APEC, Adzuna, The Muse et France Travail ne sont jamais appelés
  hors France (limite du connecteur actuel, pas de la région) ; le
  diagnostic devient alors `not_applicable_region`.
- **LinkedIn Posts** est une source *X-ray* : elle n'ouvre jamais linkedin.com,
  ne se connecte à aucun compte et ne contourne aucun CAPTCHA. Elle lit
  uniquement ce qu'un moteur de recherche public expose déjà sur les URLs
  `linkedin.com/posts/...` — titre, extrait, URL. Trois providers en chaîne,
  du plus prioritaire au repli final : **Tavily** (API officielle, quota
  gratuit de 1000 recherches/mois renouvelé chaque mois), **Exa** (API
  officielle elle aussi, mais crédits d'essai à usage unique — épuisés en
  production après ~19 runs) puis **DuckDuckGo** en dernier repli. DuckDuckGo
  soumet systématiquement ces requêtes à son défi anti-bot (« bots use
  DuckDuckGo too »), aussi bien en local que sur les runners GitHub Actions —
  la garde `XRayBlockedError` détecte ce blocage et ne tente jamais de le
  lever. Quand un provider échoue, la cause de **chaque** tentative de la
  chaîne (pas seulement la dernière) reste consultable dans le statut de la
  source, pour ne jamais confondre un blocage DuckDuckGo avec des crédits
  Exa/Tavily épuisés.

  Pour préserver le quota gratuit Tavily, la source ne tourne que sur deux
  créneaux par jour (`LINKEDIN_POSTS_RUN_HOURS_UTC`, 6h et 18h UTC) ; hors
  créneau le statut est `deferred` et n'affecte aucune offre déjà collectée.
  Budget par créneau : `LINKEDIN_POSTS_QUERY_BUDGET` (4 requêtes) × 2
  régions ≈ 16 crédits/jour, soit ~480/mois sur les 1000 gratuits. Curseur de
  rotation persisté dans `data/linkedin_posts_rotation.json`.

  Contrairement aux autres sources, LinkedIn Posts n'interroge qu'un
  sous-ensemble rotatif du catalogue par run : ne pas retrouver une offre à
  un run donné ne prouve jamais sa disparition (la requête qui l'avait
  trouvée n'a simplement pas été rejouée). Ses offres expirent donc par
  fraîcheur (24 h après leur dernière vérification), jamais par
  non-réobservation. Un post accepté a soit une date précise extraite de son
  contenu, soit une fraîcheur garantie nativement par le moteur (`time_range`
  Tavily) — jamais aucune des deux, rejeté plutôt que conservé par
  précaution.
- un circuit breaker interrompt une source après deux blocages 403/429
  consécutifs pendant le run, sans affecter les autres.
- LinkedIn exécute toutes les requêtes actives (plafond anti-boucle 1000),
  ordonnées par priorité de domaine ; les trois premières récupèrent aussi la
  description complète. JobSpy espace lui-même ses pages LinkedIn.
- le budget `domaines actifs × régions actives` est purement informatif :
  aucun volume n'est réduit (`results_wanted` reste à 1000 pour LinkedIn et
  Indeed), un avertissement s'affiche seulement au-delà de six combinaisons.
  Usage perso : 1 × 2 = 2 combinaisons.
- le commit automatisé utilise un rebase et trois tentatives sans force-push.

Pour un benchmark ponctuel sans modifier le catalogue, les environnements
`RADAR_DOMAINS=data_ia,dev_web` et
`RADAR_REGIONS=france,belgique,luxembourg,maroc` surchargent les sélections
uniquement pour le processus courant.

Mesures réelles du 13 juillet 2026, sans écriture dans les données :

- référence 16 domaines × France : 121,5 s, 129 offres uniques ; budget
  appliqué `linkedin=9`, `indeed=11`, `glassdoor=6`, `google=11` ;
- Data/IA × France/Belgique/Luxembourg/Maroc : 303,7 s, 38 offres uniques
  (31/2/2/3 par région), sans réduction car quatre combinaisons restent sous
  le seuil de six.

## Vérification locale

```bash
python -m py_compile scan.py
python -m unittest discover -s tests -v
```

La suite contient 178 tests sans accès réseau.

Le pipeline ne se connecte à aucun compte des plateformes d'emploi. Il utilise
les pages publiques et les API officielles ; il est recommandé de conserver la
fréquence et les volumes raisonnables.
