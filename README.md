# GasWatch

GasWatch est un service Python autogeberge qui collecte les prix recents de carburant autour
d'un ou plusieurs points au Quebec, conserve un historique SQLite, affiche un tableau de bord
Web et envoie des rapports et alertes sur ntfy.

> Etat du projet: tableau de bord complet avec historique horaire, agregats quotidiens,
> comparaison de stations, statistiques avancees et suivi des previsions.

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
- Classement par economie nette, avec estimation du cout du detour selon le vehicule.
- Journal des pleins, consommation reelle et economies mesurees face au prix local.
- Indicateur de confiance des prix (fraicheur, regularite, couverture, completude et coherence).
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
| `MAX_DETOUR_KM` | `8` | Detour aller-retour maximal estime |
| `MIN_NET_SAVINGS` | `2.00` | Economie nette minimale en dollars |
| `PRICE_CHECK_INTERVAL_MINUTES` | `60` | Intervalle minimal valide: 10 minutes |
| `MAX_PRICE_AGE_MINUTES` | `180` | Age maximal depuis la recuperation |
| `HISTORY_DAYS` | `30` | Fenetre de calcul de la cible |
| `TARGET_PRICE_PERCENTILE` | `25` | Percentile des minimums quotidiens |
| `TARGET_PRICE_MODE` | `AUTO` | `AUTO` ou `MANUAL` |
| `DAILY_REPORT_TIME` | `07:00` | Heure locale au format `HH:MM` |
| `WEEKLY_SUMMARY_ENABLED` | `true` | Envoie un resume hebdomadaire intelligent |
| `WEEKLY_SUMMARY_DAY`, `WEEKLY_SUMMARY_TIME` | `6`, `18:00` | Jour (0=lundi) et heure du resume |
| `NTFY_ENABLED` | `false` | Active les notifications |
| `NTFY_URL`, `NTFY_TOPIC`, `NTFY_TOKEN` | — | Serveur, sujet et jeton facultatif |
| `WEB_ENABLED` | `true` | Active le tableau de bord et son API JSON |
| `WEB_PORT` | `8080` | Port interne et port local Compose |
| `WEB_BIND_ADDRESS` | `127.0.0.1` | Adresse d'exposition sur l'hote |

## Tableau de bord et historique des stations

La page `/` lit directement SQLite et affiche le dernier prix conserve pour chaque station proche,
la moyenne locale, la distance geographique, l'age du releve et la courbe de la moyenne quotidienne
des stations suivies. Chaque station est cliquable et revele son propre historique de prix sur
30 jours, avec son minimum, son maximum et sa variation. Les graphiques selectionnent le point le
plus proche lorsque la souris passe sur toute la ligne et affichent sa date et son prix exact. Les
logos compacts des principales enseignes et une fleche de variation accompagnent les prix. Le
verdict montre aussi une tendance courte et prudente, calculee uniquement a partir des sept derniers
jours disponibles dans la base locale. Une etoile place une ou plusieurs stations favorites en tete;
les autres restent accessibles sous « Voir plus ». Une station peut aussi etre exclue puis
reaffichee depuis la roue dentee « Mes reglages » dans l'en-tete. Le verdict est compact et se
deplie au toucher pour montrer son explication et ses statistiques. Toucher l'adresse d'une station
l'ouvre directement dans Apple Maps. L'icone GasWatch est aussi fournie pour l'ecran d'accueil iOS.
`/analytics` propose les periodes 24 h, 7 j, 30 j, 90 j, 6 mois, 1 an et tout l'historique
(`/statistics` reste un alias compatible).
Les courbes minimum, moyenne et maximum peuvent etre masquees separement. Les analyses incluent
la volatilite, les variations, la distribution, le calendrier des minimums, les records, les
profils par jour et heure locale, le classement detaille des stations et la precision des
previsions. `/fillups` permet d'enregistrer les pleins et affiche les litres, depenses, consommation,
cout aux 100 km et economies du mois, de l'annee et depuis l'installation. `/api/dashboard` fournit
les donnees courantes en JSON, `/api/statistics` expose les analyses et `/health` sert au
healthcheck Docker.

Le journal des pleins permet aussi de filtrer par vehicule et dates, modifier ou supprimer une
entree, ajouter une note, joindre un recu JPG/PNG/PDF (5 Mo maximum) et exporter la selection en
CSV. Les recus sont stockes dans SQLite avec la sauvegarde et ne sont jamais envoyes au fournisseur
de prix. La consommation reelle est calculee uniquement entre deux pleins complets consecutifs;
elle est comparee a la consommation theorique configuree pour chaque vehicule.

Chaque prix affiche un score de confiance sur 100. Il combine l'age du releve, la regularite des
collectes sur 24 heures, le nombre habituel de stations, les champs manquants et l'ecart du prix
avec la mediane locale. Le detail explique toute baisse de confiance au lieu de masquer la donnee.

Le classement du tableau de bord maximise l'economie nette sur le plein moyen du vehicule. Faute
de fournisseur d'itineraire, le detour est estime prudemment comme deux fois la distance
geographique. Chaque fiche affiche la distance normale, le detour, son cout, l'economie brute,
l'economie nette et un verdict. Le prix moyen du secteur sert de reference de comparaison.

La base conserve une observation par station, carburant et collecte reussie dans
`price_observations`, meme si le prix n'a pas change. Un identifiant de collecte rend les reprises
idempotentes. Les heures sont stockees en UTC; l'interface emploie `TZ` pour les regroupements
horaires et l'affichage local. `daily_price_statistics` conserve les agregats quotidiens a long
terme sans surponderer les stations. Les migrations reconstruisent ces agregats a partir des
donnees historiques sans supprimer les observations existantes.

GasWatch demande toutes les stations retournees dans le rayon configure (jusqu'a la limite REST
documentee de 500 resultats) et les affiche sans appliquer les preferences de marque. Les favoris
et preferences continuent d'influencer les recommandations et alertes. Il ne tente pas de
reconstituer le jeu de donnees provincial.

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
du meme carburant. La recommandation d'achat reste distincte des previsions. L'interface affiche
une tendance courte et, uniquement quand au moins sept jours donnent un ajustement exploitable,
des estimations a 24 h et 48 h. Chaque prevision est conservee, evaluee a echeance et comparee au
prix reellement observe; l'erreur absolue moyenne et la part a plus ou moins 2 c/L sont publiees
dans les statistiques. Il s'agit d'un signal indicatif, pas d'une prevision de marche. En mode
`MANUAL`, la cible vient de `MANUAL_TARGET_PRICE_CENTS`.

`FILL_NOW` signifie que le prix atteint la cible et se situe nettement sous l'historique, ou que
l'autonomie estimee est insuffisante. `GOOD_PRICE`, `NORMAL_PRICE`, `WAIT` et `HIGH_PRICE` suivent
les seuils en cents. Sans historique suffisant, le resultat est `INSUFFICIENT_DATA`.

Les alertes indiquent maintenant le percentile historique, l'economie nette apres detour et le
niveau probablement bas du reservoir lorsqu'il est configure. Le resume hebdomadaire regroupe les
meilleurs prix par zone avec les depenses et economies des sept derniers jours, afin d'eviter une
succession de petites notifications.

## Sauvegarde SQLite

Ne copiez pas simplement une base active. L'API interne `Repository.backup()` utilise le mecanisme
de sauvegarde coherent de SQLite. Exemple dans le conteneur:

```bash
docker compose exec gaswatch python -c \
  "from app.database import Repository; Repository('/app/data/gaswatch.db').backup('/app/data/gaswatch-backup.db')"
```

Copiez ensuite `gaswatch-backup.db` hors du volume. Les evolutions de schema utilisent la table
`schema_version`; la version 5 ajoute les identifiants de collecte, les agregats quotidiens, le
suivi des previsions, le journal editable et les recus sans supprimer de donnees.

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
- La distance est geographique. L'estimation de detour aller-retour est clairement presentee comme
  telle et ne remplace pas un calcul routier; aucun fournisseur d'itineraire n'est encore integre.
- L'API ne fournit au plus que les stations correspondant a la requete ponctuelle et ses conditions
  interdisent la reconstruction massive du jeu de donnees. GasWatch reste volontairement local.
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
