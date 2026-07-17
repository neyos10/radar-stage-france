# 🎯 Radar Stage France

Pipeline autonome qui collecte toutes les heures des offres de stage dans
les régions activées, les classe dans un ou plusieurs domaines, détecte les
nouveautés, envoie une alerte email optionnelle et publie un dashboard sur
GitHub Pages. France, Belgique, Luxembourg et Maroc sont préconfigurés et
actifs par défaut.

Les sept sources actives sont LinkedIn et Indeed via JobSpy, APEC, Adzuna,
Jooble, The Muse et France Travail. Chaque source est isolée : un blocage
403/429, un timeout ou l'absence d'une clé API n'arrête pas les autres
connecteurs.

Glassdoor et Google Jobs ont été retirés du code : sur plusieurs runs de
production consécutifs, ils ont renvoyé 0 résultat brut dans 100 % des cas
(Glassdoor bloqué par son anti-bot, Google Jobs répondant vide sans erreur)
tout en consommant environ 2 minutes de scan pour rien. L'historique Git
conserve l'implémentation si l'un des deux redevient fiable côté JobSpy.

## Configuration produit

Le code métier n'est pas personnalisé dans `scan.py` :

- `data/domains.json` contient les 17 domaines actifs, leurs requêtes, leurs
  vocabulaires positifs/négatifs et leur priorité de collecte optionnelle.
- `data/regions.json` contient les paramètres natifs de localisation. Les quatre
  régions sont actives ; passer `enabled` à `false` permet d'en retirer une sans
  modifier le code.
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
Une offre est retenue à partir du score 2 et peut porter plusieurs domaines.
Le filtre de contrat reste indépendant et exige `stage`, `stagiaire`, `intern`,
`internship`, `PFA` ou `PFE` dans le titre, sauf quand l'API certifie déjà le
stage.

Le dashboard responsive propose une vue synthétique du dernier scan, la santé
des connecteurs, une recherche par poste/entreprise/ville et des filtres
combinables par statut de candidature, source, domaine, région et entreprise
mise en avant. La région par défaut est France. Le groupe Région est masqué
lorsqu'une seule région est active et réapparaît automatiquement dès la
seconde. Le statut « postulé » reste dans le `localStorage` du navigateur et
n'est jamais envoyé à GitHub.

`data/offers.json` conserve le contenu complet des offres et leurs dates de
première/dernière observation. Une offre ne disparaît pas si son connecteur est
temporairement bloqué ; elle est masquée seulement après une absence confirmée
par un run sain. Les entrées inactives sont purgées après 14 jours.

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
| `JOOBLE_API_KEY` | API Jooble | non |
| `THEMUSE_API_KEY` | API The Muse (relève la limite à 3600 req/h) | non — 500 req/h sans elle |
| `FT_CLIENT_ID` / `FT_CLIENT_SECRET` | API Offres d'emploi v2 | non |

Sans identifiants optionnels, leur source apparaît « non configurée » et le
reste du scan continue normalement. The Muse fonctionne même sans clé
(500 requêtes/heure, largement suffisant pour un run).

Le scan tourne automatiquement chaque heure. Pour un run immédiat, utiliser
**Run workflow** dans l'onglet GitHub Actions du dépôt. Le bouton
**Actualiser les offres** recharge uniquement le dernier dashboard publié ; le
site ne demande et ne conserve aucun token GitHub.

## Fraîcheur et robustesse

- JobSpy reçoit `hours_old` selon `days_window`. Les dates relatives courantes
  sont normalisées ; une date inconnue est conservée et signalée, une date
  réellement trop ancienne est rejetée et journalisée.
- APEC utilise les filtres serveur stage (`typesContrat=597171`) et Bac+5
  (`niveauEtude=20019`), puis un budget borné de lectures de détail. Si cet
  endpoint détail bloque en 403/429, un circuit breaker dédié arrête les
  lectures restantes du run au lieu de répéter des échecs identiques.
- France Travail utilise `natureContrat=FS` et la fenêtre configurée.
- The Muse utilise le filtre serveur natif `level=Internship` (confirmation
  équivalente au filtre stage d'APEC) : aucune requête par mot-clé n'est
  nécessaire, une seule pagination par run couvre tout le catalogue pour la
  région, puis le scoring par domaine s'applique localement comme pour APEC.
- APEC, Adzuna, Jooble, The Muse et France Travail ne sont jamais appelés
  hors France (limite du connecteur actuel, pas de la région) ; le
  diagnostic devient alors `not_applicable_region`.
- Jooble démarre avec un quota gratuit de 500 requêtes dont la périodicité
  (unique, journalière, mensuelle) n'est pas documentée publiquement. Par
  prudence, seul un sous-ensemble rotatif de `JOOBLE_QUERY_BUDGET` (10)
  requêtes est interrogé par run ; un curseur persisté dans
  `data/jooble_rotation.json` garantit que tous les domaines finissent par
  être couverts au fil des runs, sans jamais dépasser le budget.
- un circuit breaker interrompt une source après deux blocages 403/429
  consécutifs pendant le run, sans affecter les autres.
- LinkedIn est limité à six requêtes distribuées selon la priorité des domaines.
  Chaque domaine reçoit d'abord un créneau lorsque le budget le permet, puis les
  créneaux restants suivent une rotation pondérée. Les trois requêtes les plus
  prioritaires récupèrent aussi la description complète pour améliorer le
  scoring sans enrichir les six appels.
- le budget est calculé par `domaines actifs × régions actives`. Au-delà de six
  combinaisons, `results_wanted` est multiplié par `6 / combinaisons`, avec un
  plancher de cinq résultats. L'estimation affichée utilise `45 s + 35 s ×`
  le nombre de combinaisons effectives, plafonné à six (maximum indicatif :
  4 min 15 s). Cette réduction reste uniforme : Indeed exécute toutes les
  requêtes actives, donc aucun domaine n'y perd de créneau ; seule
  l'allocation LinkedIn, globalement plafonnée, doit être pondérée par
  priorité.
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

La suite contient plus de 70 tests sans accès réseau.

Le pipeline ne se connecte à aucun compte des plateformes d'emploi. Il utilise
les pages publiques et les API officielles ; il est recommandé de conserver la
fréquence et les volumes raisonnables.
