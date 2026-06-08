# Claudio

Le caméléon qui orchestre le planning de votre équipe pentest, mois après mois.

- **Backend** : Python 3.12 + Flask
- **Base de données** : PostgreSQL 16 (authentification + données applicatives dans la même base)
- **Authentification** : cookie de session + mots de passe hachés en bcrypt, protection CSRF, throttling (~250 ms / tentative de connexion)
- **Interface** : intégralement en français, calendrier rendu côté serveur (vues mois / trimestre / année)
- **Rôles** : administrateur, planificateur, et spécialités auditeur (web / mobile / code) entièrement gérées en base
- **Fuseau** : horodatages affichés en heure locale Europe/Paris
- **Déploiement** : un seul `docker compose up` lance l'ensemble

---

## 1. Démarrage rapide

```bash
docker compose up --build
# → http://localhost:8000
```

Au premier démarrage :

1. PostgreSQL démarre avec un volume nommé persistant (`db_data`) ;
2. `python -m app.init_db` migre le schéma si besoin, crée le compte admin + les 4 membres de l'équipe, et insère les données par défaut (rôles, types de mission, catégories de réunion, statuts de tâche) ;
3. Flask démarre sous Gunicorn sur le port `8000`.

Arrêt (données conservées) :
```bash
docker compose down
```

Tout effacer (supprime le volume → re-seed au prochain démarrage) :
```bash
docker compose down -v
```

---

## 2. Identifiants par défaut

> ⚠️ **Changez ces mots de passe immédiatement dans tout environnement non local.**
> Chaque utilisateur peut le faire lui-même via le menu utilisateur → *Changer mon mot de passe*.

### Comptes applicatifs

| Identifiant | Nom complet     | Mot de passe          | Rôle             | Couleur |
|-------------|-----------------|-----------------------|------------------|---------|
| `admin`     | Administrateur  | `Admin!Planning2026`  | Administrateur   | violet  |
| `bob`       | Bob Durand      | `Bob!Planning2026`    | Auditeur web     | bleu    |
| `carol`     | Carole Lefevre  | `Carol!Planning2026`  | Auditeur mobile  | vert    |
| `david`     | David Garcia    | `David!Planning2026`  | Auditeur de code | orange  |
| `eve`       | Eve Moreau      | `Eve!Planning2026`    | Planificateur    | rose    |

URL de connexion : `http://localhost:8000/auth/login`

### Identifiants base de données (lus depuis `.env`)

| Clé                  | Valeur                 |
|----------------------|------------------------|
| `POSTGRES_USER`      | `planner`              |
| `POSTGRES_PASSWORD`  | `ChangeMe_DB_S3cret!`  |
| `POSTGRES_DB`        | `planning`             |
| `SECRET_KEY`         | (aléatoire, 48+ car.)  |

```bash
docker exec -it planning_db psql -U planner -d planning
```

---

## 3. Rôles et permissions

Un utilisateur peut porter **plusieurs rôles** (ex. *Planificateur* + *Auditeur web*).
Les permissions sont calculées sur l'**ensemble** des rôles détenus.

Deux rôles système, non supprimables (`admin`, `planificateur`), pilotent les permissions.
Toutes les **spécialités** (web / mobile / code, et toute autre créée par l'admin) vivent dans
la table `roles` et sont entièrement gérables depuis l'interface.

| Rôle              | Sur le planning ? | Peut gérer (créer/éditer/supprimer) | Menus d'administration |
|-------------------|-------------------|-------------------------------------|------------------------|
| Administrateur    | ❌ (sauf si combiné à un rôle d'équipe) | Tout                  | Tous |
| Planificateur     | ✅                | Événements, projets, clients, réunions, **tâches**, **jours fériés** | Partiels |
| Auditeur (web/mobile/code) | ✅       | —                                   | ❌ |

Trois niveaux de contrôle d'accès :

- **`login_required`** — tout utilisateur connecté.
- **`manage_required`** — administrateur **ou** planificateur (`can_manage_events`) :
  événements, projets, clients, réunions, **tâches**, **jours fériés**.
- **`admin_required`** — administrateur uniquement : *Utilisateurs*, *Types de mission*,
  *Types d'absence*.

Garde-fous : un administrateur **ne peut pas retirer son propre rôle admin** ni se supprimer.
Un utilisateur apparaît sur le planning dès qu'il détient **au moins un rôle d'équipe**
(planificateur ou une spécialité auditeur).

---

## 4. Concepts métier

- **Client** — l'entreprise auditée.
- **Projet** — conteneur de plus haut niveau, rattaché à un client. Regroupe les missions,
  réunions et tâches d'un engagement. Statut *Actif* / *Clos*.
- **Mission / Événement** — un créneau sur le planning, avec participants, période et type.
  Les missions techniques (Audit de code, MEEXT, Revue SSI…) suivent un workflow de devis ;
  les absences (Congé, Formation) bloquent la disponibilité mais sont hors projet.
- **Sous-type** — précise une mission technique et porte la **spécialité** (web / mobile / code)
  héritée par l'événement.
- **Réunion** — point ponctuel rattaché à un projet (cadrage, restitution, point client…).
- **Tâche** — suivi de travail rattaché à un projet, avec statut et échéance. Des **modèles**
  de tâche peuvent être générés automatiquement à la fin d'une mission technique.

---

## 5. Fonctionnalités

### Authentification (`/auth/*`)
- Connexion identifiant + mot de passe, protégée CSRF par Flask-WTF.
- Mots de passe stockés en **bcrypt** (coût 12). Jamais journalisés.
- Chaque tentative de connexion dure au minimum 250 ms (anti-brute-force / anti-énumération).
- **Changer mon mot de passe** (`/auth/password`) : depuis le menu utilisateur, en confirmant
  le mot de passe actuel.
- Cookie de session HTTP-only, SameSite=Lax. `FORCE_HTTPS=1` derrière un proxy TLS marque le cookie `Secure`.
- **SSO OpenID Connect (optionnel)** : un bouton « Connexion SSO » apparaît sur la page de
  connexion dès que le SSO est configuré. Compatible avec tout fournisseur OIDC conforme
  (Keycloak, Authentik, Google, Entra ID, Okta, GitLab…). Le SSO **coexiste** avec la connexion
  identifiant + mot de passe.

#### Activer le SSO (OIDC)
D'abord, déclarez l'application auprès de votre fournisseur d'identité avec l'URL de redirection :
`https://<votre-domaine>/auth/oidc/callback`. Ensuite, deux méthodes de configuration au choix :

- **Via l'interface (recommandé)** — menu **Administration → SSO / OIDC** (admin uniquement).
  Saisissez Client ID, secret et URL de découverte ; pas de redémarrage requis. Le secret est
  **chiffré au repos** (clé dérivée de `SECRET_KEY`) et n'est jamais réaffiché.
- **Via variables d'environnement** — renseignez `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`,
  `OIDC_DISCOVERY_URL` dans `.env` (voir `.env.example`). **Si ces variables sont présentes,
  elles font autorité** et l'écran d'administration passe en lecture seule (idéal pour un
  déploiement piloté par IaC).

Réglages optionnels (UI ou env) : `OIDC_SCOPES`, `OIDC_USERNAME_CLAIM` (défaut
`preferred_username` ; pour Google, utilisez `email`), `OIDC_NAME_CLAIM`, `OIDC_BUTTON_LABEL`,
`OIDC_AUTO_CREATE` (créer automatiquement les comptes inconnus — désactivé par défaut),
`OIDC_DEFAULT_ROLES`.

Sans auto-création, l'utilisateur doit déjà exister localement (username identique au claim) ;
sa première connexion SSO lie alors son identité fédérée (`oidc_sub`) à son compte.

### Planning (`/planning/<année>/<mois>`)
- Calendrier **mois / trimestre / année** (Lundi → Vendredi ; week-ends masqués).
- Affiche tous les membres de l'équipe (hors admins purs), codés par couleur.
- Navigation période précédente / suivante.
- Le `+` d'une journée crée un événement pré-rempli ; cliquer sur un événement l'édite.
- **Jours fériés** français calculés automatiquement (y compris Pâques / Ascension / Pentecôte),
  et **personnalisables** (voir §Jours fériés).
- Cellules « au moins un auditeur disponible » mises en évidence.
- Synthèse **« Cette semaine »** en tête : événements par type, tâches par statut, réunions par catégorie.
- L'heure de début n'est affichée que pour les réunions ; les missions techniques (jour entier) la masquent.

### Missions techniques — workflow de devis
- Cycle : **Préplanifié** → réunion de cadrage → **devis validé** → **Planifié**.
- **FPR (Fiche Pré-Requis)** : une mission ne peut passer en *Planifié* tant que la FPR n'est pas reçue ; lien vers la FPR stockable.
- **JH (jours-homme)** : la date de fin se calcule à partir du nombre de JH réparti sur les pentesters
  (`fin = début + ⌈JH / nb pentesters⌉ jours ouvrés`, jours fériés exclus). En édition, le champ JH
  est pré-rempli automatiquement (jours ouvrés × nombre de pentesters).
- **Détection de conflits** : un participant déjà en congé/formation sur la période est refusé ;
  un chevauchement entre deux missions techniques déclenche un avertissement (non bloquant).
- Champ **« Difficultés rencontrées »** (notes post-mortem) visible dans la synthèse projets.
- Contrôle d'unicité (titre + dates) avant enregistrement.

### Recherche (`/search`)
- **Recherche textuelle** dans les projets, missions, réunions et tâches (insensible à la casse, sous-chaîne).
- **Recherche de disponibilité** : trouve le prochain créneau consécutif où *N* pentesters sont
  libres simultanément (filtrable par spécialité), et préremplit un nouvel événement.

### Projets (`/projects`)
- CRUD des projets et clients.
- **Détail** d'un projet : missions, réunions et tâches associées.
- **Synthèse** transverse (par année) et **export CSV**.

### Tâches (`/admin/tasks`) — *admin + planificateur*
- Tableau des tâches avec statuts personnalisables (couleur + emoji).
- **Modèles** de tâche réutilisables ; un modèle marqué *auto-mission* génère une tâche sur le projet
  à chaque mission technique créée (échéance = fin de mission + N jours ouvrés).
- Le renommage d'une mission propage le titre à ses tâches auto-générées.

### Réunions (`/meetings`) — *admin + planificateur*
- Réunions ponctuelles rattachées à un projet, avec catégorie et plage horaire optionnelle.

### Jours fériés (`/admin/holidays`) — *admin + planificateur*
- Liste annuelle des jours fériés français, navigable par année.
- **Marquer un férié comme « travaillé exceptionnellement »** : il est alors compté comme un jour
  ouvré partout (planning, disponibilités, calcul des JH).
- **Ajouter un jour chômé personnalisé** (pont, fermeture…) non national, traité comme non travaillé.
- Entièrement personnalisable par déploiement, sans toucher au code.

### Administration — *admin uniquement*
- **Utilisateurs** (`/admin/users`) : CRUD complet, rôles multiples, couleur, mot de passe.
- **Types de mission / Types d'absence** (`/admin/mission-types`) : catégories d'événements et leurs
  sous-types (qui portent la spécialité). Supprimer un type ne supprime pas les événements liés.
- **API / Swagger** (`/admin/api`) : gestion des jetons d'API en lecture seule et documentation
  interactive (voir § 5bis).

### API d'intégration (lecture seule) — *admin pour la gestion*

API JSON versionnée (`/api/v1`) pensée pour alimenter des outils externes — typiquement
l'import **Pwndoc**. Elle est **découplée des comptes utilisateurs** : un client machine n'est pas
un membre de l'équipe et n'apparaît jamais sur le planning.

- **Authentification** : jeton porteur — `Authorization: Bearer <jeton>`. Les jetons sont créés
  depuis *Administration → API / Swagger*, stockés **hachés** (SHA-256), et la valeur en clair n'est
  affichée **qu'une seule fois**. Un jeton se révoque d'un clic.
- **Portée** : `read_only` (le « rôle » lecture seule). La couche d'autorisation est prête pour des
  portées plus larges sans changement de schéma.
- **Documentation** : Swagger UI sur `/admin/api/docs` (accès admin authentifié), spec OpenAPI 3.0
  servie sur `/admin/api/openapi.json`.

| Méthode | Route | Description |
|---------|-------|-------------|
| `GET` | `/api/v1/projects` | Liste des projets : `id`, `name`, `reference_interne`. |
| `GET` | `/api/v1/projects/{id}` | Un projet : `id`, `name`, `reference_interne`. |

> `reference_interne` correspond au champ « Référence interne » du projet (numéro de devis).

**Exemple d'appel** (récupérer le nom et la référence interne d'un projet) :

```bash
# 1. Créer un jeton dans Administration → API / Swagger, puis :
TOKEN="cld_le_jeton_copie_a_la_creation"
BASE="http://localhost:8000"   # ou l'URL HTTPS de production

# Lister tous les projets
curl -s -H "Authorization: Bearer $TOKEN" "$BASE/api/v1/projects" | jq

# Récupérer le projet d'id 1
curl -s -H "Authorization: Bearer $TOKEN" "$BASE/api/v1/projects/1" | jq
# → { "id": 1, "name": "Audit applicatif ACME", "reference_interne": "REF-2026-042" }
```

Réponses d'erreur normalisées : `{ "error": { "code": "...", "message": "..." } }`
(`401` jeton absent/invalide/révoqué, `404` projet introuvable).

### Traçabilité
- Chaque événement enregistre son **créateur**, son **dernier éditeur** et les horodatages
  (heure locale Europe/Paris), consultables via le panneau *Détails* et l'infobulle du calendrier.
- **Historique des dates** : chaque changement de période/statut d'une mission ou réunion est journalisé.

### Confort
- **Thème clair / sombre** (bascule persistée), menu utilisateur regroupant nom/rôle, changement de
  mot de passe, thème et déconnexion.

---

## 6. Modèle de données

Tout réside dans PostgreSQL — authentification et données applicatives partagent la même base :

- **`users`**, **`roles`**, **`user_roles`** — comptes, spécialités gérées en base, et association M2M.
- **`clients`**, **`projects`** — hiérarchie client → projet (statut actif/clos).
- **`meeting_types`**, **`mission_subtypes`** — catégories d'événements (drapeaux *technique* /
  *bloquant* / *avec client*) et leurs sous-types portant la spécialité.
- **`events`**, **`event_participants`** — missions/événements (dates, heures, statut, FPR, projet,
  créateur/éditeur) et participants (M2M).
- **`tasks`**, **`task_statuses`** — tâches (modèles inclus, échéances, lien projet/mission) et statuts.
- **`meetings`**, **`meeting_categories`** — réunions ponctuelles et leurs catégories.
- **`event_date_history`**, **`meeting_date_history`** — historiques de changement de dates.
- **`holiday_overrides`** — personnalisation du calendrier ouvré (fériés travaillés / jours chômés sur mesure).

---

## 7. Arborescence du projet

```
Planning/
├── docker-compose.yml      # orchestre web + db
├── Dockerfile              # image Flask
├── requirements.txt        # dépendances Python
├── .env                    # secrets / accès DB
├── db/init.sql             # extension pgcrypto
└── app/
    ├── __init__.py         # app factory Flask + enregistrement des blueprints
    ├── extensions.py
    ├── models.py           # tous les modèles SQLAlchemy
    ├── holidays.py         # jours fériés français + résolution des overrides
    ├── init_db.py          # migrations, seed équipe + données par défaut
    ├── auth.py             # /auth — connexion, déconnexion, mot de passe
    ├── planning.py         # /planning — calendrier + CRUD événements + dispo
    ├── projects.py         # /projects — projets, synthèse, export CSV
    ├── clients.py          # clients
    ├── meetings.py         # /meetings — réunions
    ├── tasks.py            # /admin/tasks — tâches + modèles
    ├── mission_types.py    # /admin/mission-types — types + sous-types
    ├── holiday_admin.py    # /admin/holidays — jours fériés personnalisables
    ├── users.py            # /admin/users — comptes + décorateurs de permissions
    ├── search.py           # /search — recherche textuelle + disponibilité
    ├── dashboard.py        # « Ma semaine »
    ├── static/css/style.css
    └── templates/
```

---

## 8. Sécurité

| Sujet                     | Mesure                                                                          |
|---------------------------|---------------------------------------------------------------------------------|
| Stockage des mots de passe| `bcrypt` coût 12, salé par utilisateur                                          |
| Transport des identifiants| `FORCE_HTTPS=1` derrière un reverse proxy TLS → cookie `Secure`                 |
| Fixation de session       | Flask-Login régénère à la connexion ; cookie HTTP-only, SameSite=Lax           |
| CSRF                      | Flask-WTF sur tous les POST                                                     |
| Injection SQL             | ORM SQLAlchemy, requêtes paramétrées                                            |
| Open redirect             | Le paramètre `next` n'accepte que des chemins relatifs same-origin             |
| Brute force / énumération | 250 ms minimum par tentative ; réponse identique pour identifiant inconnu / mauvais mot de passe |
| Élévation de privilèges   | `admin_required` / `manage_required` sur les routes sensibles ; un admin ne peut pas se rétrograder |
| Exposition du conteneur   | L'app tourne en utilisateur non-root `planner`                                 |
| Exposition de la base     | Port Postgres `5432` exposé sur l'hôte — retirer le mapping en production       |
| Secrets                   | Lus depuis `.env` ; faire tourner `SECRET_KEY` et `POSTGRES_PASSWORD` avant déploiement |

---

## 9. Tâches courantes

### Tout réinitialiser (supprime le volume → re-seed)
```bash
docker compose down -v
docker compose up --build
```

### Re-seed (idempotent — ne recrée pas les comptes existants)
```bash
docker exec -it planning_web python -m app.init_db
```

### Changer un mot de passe depuis un shell
```bash
docker exec -it planning_web python -c "
from app import create_app
from app.extensions import db
from app.models import User
app = create_app()
with app.app_context():
    u = db.session.execute(db.select(User).where(User.username=='admin')).scalar_one()
    u.set_password('nouveau-mot-de-passe-solide')
    db.session.commit()
    print('ok')
"
```

Ou, plus simplement : se connecter et utiliser *Changer mon mot de passe* (tout utilisateur) ou le
menu *Utilisateurs* (admin).

### Consulter les logs
```bash
docker compose logs -f web
docker compose logs -f db
```

---

## 10. Checklist de durcissement (production)

- [ ] Faire tourner `SECRET_KEY` dans `.env`.
- [ ] Faire tourner `POSTGRES_PASSWORD`.
- [ ] Changer tous les mots de passe des comptes seedés.
- [ ] Retirer le mapping `ports: 5432` de Postgres dans `docker-compose.yml`.
- [ ] Placer l'app derrière un reverse proxy TLS (Caddy, nginx, Traefik) et définir `FORCE_HTTPS=1`.
- [ ] Sauvegarder le volume `db_data` régulièrement.

---

## 11. Licence

Distribué sous licence **MIT** — voir le fichier [`LICENSE`](LICENSE). Vous êtes
libre de l'utiliser, le modifier et le déployer, y compris à des fins
commerciales, pour votre propre équipe pentest.
