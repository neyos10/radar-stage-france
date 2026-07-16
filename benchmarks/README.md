# Benchmarks FAST/FULL

Ce dossier sépare strictement les preuves historiques des nouvelles mesures.

`fast-data-ia-pre-optimization-live-2026-07-16.json` contient une mesure réseau
réelle et isolée du FAST : 142,3 s, 25 requêtes/pages distantes reconstituées,
24 pages comptabilisées par source et 6 offres
retenues. Elle précède la désactivation de l'enrichissement global des
descriptions LinkedIn en FAST. Une seconde mesure a été refusée par le quota de
l'environnement d'exécution ; aucune durée post-optimisation n'est donc
inventée. DuckDuckGo HTML a réellement répondu par un challenge HTTP 202 et le
circuit breaker X-ray s'est arrêté après deux appels.

`xray-provider-evaluation-2026-07-16.json` trace séparément l'évaluation des
providers : DuckDuckGo HTML était disponible via `requests` et a été testé en
réseau (challenge HTTP 202) ; `ddgs` et `duckduckgo_search` n'étaient pas
installés dans le projet ni dans l'environnement validé, donc aucun succès les
concernant n'est inventé. L'interface `XRaySearchProvider` permet de remplacer
le provider sans toucher au pipeline.

## Baseline historique

`baseline-legacy-full-2026-07-15.json` est une **reconstitution**, pas un
nouveau benchmark réseau. Il provient du `data/source_status.json` versionné du
run du 15 juillet 2026 et des tailles des stores observées avant la refonte.
L'ancien schéma ne comptait ni chaque requête réseau, ni les timeouts, ni les
lectures de détail : ces champs valent donc `null`, sans estimation inventée.

Pour reconstituer le même format depuis un autre snapshot :

```bash
python scripts/capture_scan_baseline.py \
  --status data/source_status.json \
  --offers data/offers.json \
  --seen data/seen_offers.json
```

## Mesures des nouveaux profils

Les deux profils peuvent être mesurés séquentiellement sans toucher aux
fichiers de production :

```bash
python scripts/benchmark_profiles.py
```

Par défaut, les rapports sont écrits dans le répertoire temporaire système
`radar-stage-benchmarks`. Pour choisir une destination :

```bash
python scripts/benchmark_profiles.py \
  --profiles fast_data_ia full_general \
  --output-dir /tmp/radar-stage-benchmarks
```

Chaque commande déléguée à `radar.py` comporte `--benchmark`. Ce mode interdit
l'email, la détection « nouvelle », ainsi que toute modification de
`offers.json`, `seen_offers.json`, des statuts de production et du dashboard.

Les mesures réelles dépendent des plateformes, des blocages et des secrets
optionnels. Un échec réseau reste un résultat mesuré à documenter ; il ne doit
jamais être présenté comme une collecte réussie.

## Projection GitHub Actions

Le calculateur ne choisit aucune fréquence. Il arrondit prudemment chaque p95 à
la minute supérieure et applique le plafond planifiable `3000 - 50 = 2950` :

```bash
python budget.py \
  --fast-p95-seconds 120 --fast-runs 100 \
  --full-p95-seconds 900 --full-runs 10 \
  --other-workflows-minutes 100
```

Remplacer les exemples par les p95 observés et les nombres mensuels envisagés
après plusieurs runs. Aucun horaire final FAST/FULL n'est configuré à ce stade.

Avec les seules durées actuellement disponibles — FAST réel **avant**
optimisation à 142,287697 s et FULL historique **reconstitué** à 3068,6 s — un
scénario purement unitaire d'un run de chaque profil arrondit à `3 + 52 = 55`
minutes. Il laisserait 2 895 minutes après la réserve de 50 minutes. Ce calcul
valide l'outil ; il ne constitue ni une cadence proposée ni une projection p95
des profils optimisés.
