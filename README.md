# GasWatch

GasWatch est un service Python autogeberge qui collecte les prix recents de carburant autour
d'un ou plusieurs points au Quebec, conserve un historique SQLite, affiche un tableau de bord
Web et envoie des rapports et alertes sur ntfy.

> Etat du projet: fondation fonctionnelle (v0.1). Le connecteur, l'historique, les
> recommandations, ntfy, Docker et la CI sont presents. Certaines fonctions avancees du cahier
> des charges restent planifiees; voir **Limites actuelles**.

## Source et attribution

GasWatch utilise l'API REST publique documentee de
[Gas Quebec](https://www.gasquebec.ca/api), endpoint `/api/stations/nearby`. Les donnees de prix
proviennent de Regie essence Quebec (Regie de l'energie du Quebec) et sont presentees par Gas
Quebec. L'attribution renvoyee par l'API est conservee dans SQLite et dans les notifications.

Les prix sont declares par les stations et peuvent differer du prix a la pompe. GasWatch les
qualifie de **recents**, jamais de temps reel. L'application effectue une requete par combinaison
zone/type de carburant a l'intervalle configure et respecte les reponses HTTP 429. Elle n'effectue
aucune extraction massive.

## Fonctionnalites v0.1

- Recherche geographique pour ordinaire, super et diesel, rayon maximal de 200 km.
- Plusieurs zones et plusieurs vehicules, avec mutualisation des requetes d'un meme carburant.
- Preferences de marque, favoris par identifiant et mode exclusif.
- Historique SQLite en mode WAL, migrations versionnees et sauvegarde avec l'API SQLite.
- Minimum, moyenne, mediane, maximum et cible par percentile des minimums quotidiens.
- Recommandation deterministe, avec priorite a l'autonomie lorsqu'un niveau est configure.
- Rapport quotidien et alertes ntfy avec anti-doublon persistant.
- Tableau de bord Web adaptatif, sans framework JavaScript ni conteneur supplementaire.
- Ordonnancement adapte au fuseau horaire, sans chevauchement des taches.
- Conteneur non privilegie, healthcheck, Compose et publication GHCR multi-architecture.

## Installation Docker Compose

```bash
cp .env.example .env
# Modifier .env: coordonnees, vehicules et ntfy.
docker compose pull
docker compose up -d
docker compose logs -f gaswatch
```

Le volume nomme `gaswatch-data` conserve `/app/data/gaswatch.db`. Le tableau de bord est accessible
sur `http://127.0.0.1:8080` depuis le serveur. Cette liaison locale evite de publier une interface
sans authentification sur Internet. Pour un acces distant, utilisez un VPN ou un reverse proxy
HTTPS avec authentification.
Pour construire la branche locale au lieu de tirer GHCR, executez
`docker build -t gaswatch:local .` puis adaptez temporairement `image` dans Compose.

## Configuration

Toutes les options sont documentees dans `.env.example`. Les groupes principaux sont:

| Variable | Defaut | Role |
|---|---:|---|
| `TZ` | `America/Toronto` | Fuseau du rapport et changements d'heure |
| `DATABASE_PATH` | `/app/data/gaswatch.db` | Base persistante |
| `HOME_LATITUDE`, `HOME_LONGITUDE` | requis | Point simple, si `LOCATIONS` est vide |
| `SEARCH_RADIUS_KM` | `15` | Rayon geographique, pas routier |
| `LOCATIONS` | vide | Cles de plusieurs zones, separees par virgules |
| `VEHICLES` | vide | Cles des vehicules, separees par virgules |
| `PREFERRED_STATIONS` | vide | Marques preferees |
| `PREFERRED_ONLY` | `false` | Exclure les autres marques |
| `PRICE_CHECK_INTERVAL_MINUTES` | `60` | Intervalle minimal valide: 10 minutes |
| `MAX_PRICE_AGE_MINUTES` | `180` | Age maximal depuis la recuperation |
| `HISTORY_DAYS` | `30` | Fenetre de calcul de la cible |
| `TARGET_PRICE_PERCENTILE` | `25` | Percentile des minimums quotidiens |
| `TARGET_PRICE_MODE` | `AUTO` | `AUTO` ou `MANUAL` |
| `DAILY_REPORT_TIME` | `07:00` | Heure locale au format `HH:MM` |
| `NTFY_ENABLED` | `false` | Active les notifications |
| `NTFY_URL`, `NTFY_TOPIC`, `NTFY_TOKEN` | — | Serveur, sujet et jeton facultatif |
| `WEB_ENABLED` | `true` | Active le tableau de bord et son API JSON |
| `WEB_PORT` | `8080` | Port interne et port local Compose |
| `WEB_BIND_ADDRESS` | `127.0.0.1` | Adresse d'exposition sur l'hote |

## Tableau de bord et historique des stations

La page `/` lit directement SQLite et affiche le dernier prix conserve pour chaque station proche,
la moyenne locale, la distance geographique, l'age du releve et la courbe de la moyenne quotidienne
des stations suivies. Chaque station est cliquable et revele son propre historique de prix sur
30 jours, avec son minimum, son maximum et sa variation.
`/api/dashboard` fournit les memes donnees en JSON et `/health` sert au healthcheck Docker.

La base conserve les stations et les observations dans `stations` et `price_observations`. Pour
limiter sa croissance, un prix identique n'est enregistre qu'une fois par station et par jour;
un changement de prix est toujours journalise. L'heure UTC de recuperation, la zone, le carburant,
la distance et l'attribution de la source sont conserves. Le volume Docker rend cet historique
persistant apres les redemarrages et mises a jour.

GasWatch demande au plus dix stations par zone et par carburant, conformement aux conditions
d'usage ponctuel de Gas Quebec. Il ne tente pas de reconstituer le jeu de donnees provincial.

### Plusieurs vehicules

```dotenv
VEHICLES=ASCENT,CAR_2
VEHICLE_ASCENT_NAME=Subaru Ascent
VEHICLE_ASCENT_FUEL=REGULAR
VEHICLE_ASCENT_CONSUMPTION=11.5
VEHICLE_ASCENT_TANK_CAPACITY=73
VEHICLE_ASCENT_AVERAGE_FILL_LITERS=50
VEHICLE_CAR_2_NAME=Voiture 2
VEHICLE_CAR_2_FUEL=PREMIUM
VEHICLE_CAR_2_CONSUMPTION=8.5
VEHICLE_CAR_2_TANK_CAPACITY=55
VEHICLE_CAR_2_AVERAGE_FILL_LITERS=40
```

Le niveau, la distance quotidienne et la reserve sont facultatifs. L'autonomie est une estimation,
pas une mesure. Un plein moyen ne peut pas depasser la capacite du reservoir.

### Plusieurs zones

Definissez `LOCATIONS=HOME,WORK`, puis pour chaque cle:
`LOCATION_HOME_NAME`, `LOCATION_HOME_LATITUDE`, `LOCATION_HOME_LONGITUDE` et
`LOCATION_HOME_RADIUS_KM`. Les zones sont analysees independamment.

## Recommandations et cible

En mode `AUTO`, GasWatch attend au moins `MINIMUM_HISTORY_DAYS` minimums quotidiens puis calcule
le percentile configure. Il compare le minimum actuel aux minimums historiques du meme secteur et
du meme carburant. Il ne predit pas le prix futur. En mode `MANUAL`, la cible vient de
`MANUAL_TARGET_PRICE_CENTS`.

`FILL_NOW` signifie que le prix atteint la cible et se situe nettement sous l'historique, ou que
l'autonomie estimee est insuffisante. `GOOD_PRICE`, `NORMAL_PRICE`, `WAIT` et `HIGH_PRICE` suivent
les seuils en cents. Sans historique suffisant, le resultat est `INSUFFICIENT_DATA`.

## Sauvegarde SQLite

Ne copiez pas simplement une base active. L'API interne `Repository.backup()` utilise le mecanisme
de sauvegarde coherent de SQLite. Exemple dans le conteneur:

```bash
docker compose exec gaswatch python -c \
  "from app.database import Repository; Repository('/app/data/gaswatch.db').backup('/app/data/gaswatch-backup.db')"
```

Copiez ensuite `gaswatch-backup.db` hors du volume. Les evolutions de schema utilisent la table
`schema_version`; la v0.1 installe la version 1 sans supprimer de donnees.

## Developpement et tests

```bash
python3.12 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
ruff check .
ruff format --check .
pytest
docker build -t gaswatch:test .
```

Les tests utilisent des donnees fictives et ne contactent jamais le fournisseur reel.

## GitHub Actions et GHCR

`ci.yml` execute Ruff, pytest, une validation de configuration et un build Docker. `docker.yml`
publie `linux/amd64` et `linux/arm64` vers `ghcr.io/allen-jacob/gaswatch` sur `main`, sur un tag
`v*` et manuellement. Il utilise seulement `GITHUB_TOKEN` avec `packages: write`. Pour une image
publique, rendez le package GHCR public; pour une image privee, connectez le serveur avec un PAT
ayant `read:packages` avant `docker compose pull`.

## Limites actuelles

- L'endpoint geographique ne fournit pas l'heure de publication du prix; GasWatch enregistre
  l'heure UTC de recuperation et laisse `published_at` vide.
- La distance est la distance geographique fournie par l'API. Aucun cout ou detour routier n'est
  affirme. Les fonctions de cout aller-retour existent, mais ne pilotent pas encore le choix sans
  fournisseur d'itineraire.
- L'API ne fournit au plus que les stations correspondant a la requete ponctuelle et ses conditions
  interdisent la reconstruction massive du jeu de donnees. GasWatch reste volontairement local.
- Les variations 24 h/7 j/30 j et les rapports techniques d'erreur persistante viendront dans une
  iteration suivante.
- Le rapport est cree apres les prochaines collectes. Lors du tout premier demarrage, il faut
  plusieurs jours pour obtenir une cible automatique fiable.

## Depannage

- **Aucune station:** verifiez les coordonnees, le rayon, le carburant et `PREFERRED_ONLY`.
- **Rapport absent:** activez ntfy, verifiez le sujet et attendez une collecte fraiche.
- **HTTP 429:** augmentez l'intervalle; GasWatch attend le cycle suivant et ne boucle pas.
- **Conteneur unhealthy:** consultez `docker compose logs gaswatch` et les permissions du volume.
- **Validation au demarrage:** les erreurs nomment la variable manquante ou incoherente; aucun
  secret n'est inclus dans les logs.

## Mise a jour

```bash
docker compose pull
docker compose up -d
```

Conservez une sauvegarde SQLite avant une mise a jour majeure.
