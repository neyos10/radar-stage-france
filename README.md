# 🎯 Radar Stage France

Pipeline autonome qui collecte toutes les 30 minutes des offres de stage dans
les régions activées, les classe dans un ou plusieurs domaines, détecte les
nouveautés, envoie une alerte email optionnelle et publie un dashboard sur
GitHub Pages. France, Belgique, Luxembourg et Maroc sont préconfigurés et
actifs par défaut.

Les sept sources sont LinkedIn, Indeed, Glassdoor et Google Jobs via JobSpy,
APEC, Adzuna et France Travail. Chaque source est isolée : un blocage 403/429,
un timeout ou l'absence d'une clé API n'arrête pas les autres connecteurs.

## Configuration produit

Le code métier n'est pas personnalisé dans `scan.py` :

- `data/domains.json` contient les 16 domaines actifs, leurs requêtes et leurs
  vocabulaires positifs/négatifs.
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

Le classifieur attribue par domaine +3 par terme positif dans le titre, +1 dans
la description, -4 par terme négatif dans le titre et -1 dans la description.
Une offre est retenue à partir du score 2 et peut porter plusieurs domaines.
Le filtre de contrat reste indépendant et exige `stage`, `stagiaire`, `intern`,
`internship`, `PFA` ou `PFE` dans le titre, sauf quand l'API certifie déjà le
stage.

Le dashboard propose des filtres combinables par statut de candidature,
source, domaine, région et entreprise mise en avant. La région par défaut est
France. Le groupe Région est masqué lorsqu'une seule région est active et
réapparaît automatiquement dès la seconde. Le statut « postulé » reste dans le
`localStorage` du navigateur et n'est jamais envoyé à GitHub.

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
| `FT_CLIENT_ID` / `FT_CLIENT_SECRET` | API Offres d'emploi v2 | non |

Sans identifiants optionnels, leur source apparaît « non configurée » et le
reste du scan continue normalement.

Pour le premier lancement : Actions → **Radar Stage France** → **Run workflow**.
Le bouton « Actualiser » du dashboard recharge le dernier résultat publié ;
une page GitHub Pages statique ne possède volontairement aucun jeton permettant
de déclencher elle-même `workflow_dispatch`.

## Fraîcheur et robustesse

- JobSpy reçoit `hours_old` selon `days_window`. Les dates relatives courantes
  sont normalisées ; une date inconnue est conservée et signalée, une date
  réellement trop ancienne est rejetée et journalisée.
- APEC utilise les filtres serveur stage (`typesContrat=597171`) et Bac+5
  (`niveauEtude=20019`), puis un budget borné de lectures de détail.
- France Travail utilise `natureContrat=FS` et la fenêtre configurée.
- APEC et France Travail ne sont jamais appelés hors France ; le diagnostic
  devient alors `not_applicable_region`. JobSpy 1.1.82 ne proposant Glassdoor
  que pour France et Belgique parmi le catalogue, Luxembourg et Maroc sont
  également ignorés proprement pour cette source.
- un circuit breaker interrompt une source après deux blocages 403/429
  consécutifs pendant le run, sans affecter les autres.
- LinkedIn est limité à six requêtes distribuées équitablement entre domaines
  actifs afin de garder un temps de scan maîtrisé.
- le budget est calculé par `domaines actifs × régions actives`. Au-delà de six
  combinaisons, `results_wanted` est multiplié par `6 / combinaisons`, avec un
  plancher de cinq résultats. L'estimation affichée utilise `45 s + 35 s ×`
  le nombre de combinaisons effectives, plafonné à six (maximum indicatif :
  4 min 15 s).
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

La suite contient 70 tests sans accès réseau.

Le pipeline ne se connecte à aucun compte des plateformes d'emploi. Il utilise
les pages publiques et les API officielles ; il est recommandé de conserver la
fréquence et les volumes raisonnables.
