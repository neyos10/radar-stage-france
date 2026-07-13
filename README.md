# 🎯 Radar Stage France

Pipeline autonome qui collecte toutes les 30 minutes des offres de stage en
France, les classe dans un ou plusieurs domaines, détecte les nouveautés,
envoie une alerte email optionnelle et publie un dashboard sur GitHub Pages.

Les sept sources sont LinkedIn, Indeed, Glassdoor et Google Jobs via JobSpy,
APEC, Adzuna et France Travail. Chaque source est isolée : un blocage 403/429,
un timeout ou l'absence d'une clé API n'arrête pas les autres connecteurs.

## Configuration produit

Le code métier n'est pas personnalisé dans `scan.py` :

- `data/domains.json` contient les 16 domaines, leurs requêtes et leurs
  vocabulaires positifs/négatifs. Seul `data_ia` est actif par défaut ; passer
  `enabled` à `true` active un autre domaine au prochain scan.
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
source, domaine et entreprise mise en avant. Le statut « postulé » reste dans
le `localStorage` du navigateur et n'est jamais envoyé à GitHub.

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
- un circuit breaker interrompt une source après deux blocages 403/429
  consécutifs pendant le run, sans affecter les autres.
- LinkedIn est limité à six requêtes distribuées équitablement entre domaines
  actifs afin de garder un temps de scan maîtrisé.
- le commit automatisé utilise un rebase et trois tentatives sans force-push.

Pour un benchmark ponctuel sans modifier le catalogue, l'environnement
`RADAR_DOMAINS=data_ia,dev_web,compta_gestion` surcharge les domaines actifs
uniquement pour le processus courant.

## Vérification locale

```bash
python -m py_compile scan.py
python -m unittest discover -s tests -v
```

Le pipeline ne se connecte à aucun compte des plateformes d'emploi. Il utilise
les pages publiques et les API officielles ; il est recommandé de conserver la
fréquence et les volumes raisonnables.
