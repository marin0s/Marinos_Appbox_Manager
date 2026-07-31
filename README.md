# Marinos AppBox Manager

Marinos AppBox Manager est un control plane auto-hébergé pour provisionner et administrer des AppBox multimédias Plex et Jellyfin sur plusieurs nœuds Docker.

> Version de production actuelle : **1.6.0-alpha.4**  
> Version en préparation : **1.6.0-alpha.5**

## Fonctions principales

- gestion centralisée des nœuds et de leurs agents ;
- déploiement distant d'AppBox Plex, Jellyfin et Tautulli ;
- placement manuel ou automatique ;
- réservation de ports et profils de provisioning ;
- inventaire Docker distribué et réconciliation desired/observed state ;
- détection des conteneurs manquants, dérives et orphelins ;
- images de référence pour réutiliser une médiathèque préparée ;
- workflows transactionnels, journal d'audit et suivi des jobs.

## Architecture

```text
CRONOS
└── Control Plane AppBox Manager
    ├── interface Web et API
    ├── base SQLite
    ├── orchestrateur de jobs
    └── file de commandes des agents

Nœuds distants
└── marinos-appbox-agent
    ├── inventaire Docker
    ├── métriques et heartbeat
    ├── exécution des déploiements
    └── construction/restauration des images de référence
```

CRONOS est uniquement le control plane et ne reçoit pas d'AppBox. Les opérations Docker sont exécutées sur les nœuds cibles par l'agent.

## Développement

Branches utilisées :

- `main` : versions validées ;
- `develop` : intégration ;
- `feature/*` : évolutions ciblées ;
- `release/*` : préparation d'une livraison.

Les règles du projet sont détaillées dans [AGENTS.md](AGENTS.md) et [CONTRIBUTING.md](CONTRIBUTING.md).

## Documentation

- [Architecture](docs/architecture.md)
- [Images de référence](docs/reference-images.md)
- [Processus de livraison](docs/release-process.md)
- [Roadmap](ROADMAP.md)
- [Historique des versions](CHANGELOG.md)

## Déploiement local

```bash
cd /opt/appbox-manager-poc
docker compose up -d --build
curl -fsS http://127.0.0.1:8090/health
```

La base de données, les secrets et les données runtime ne doivent jamais être commités.

## État du projet

Le chantier prioritaire est la version **1.6.0-alpha.5**, dédiée à la fiabilisation complète des images de référence Plex : cohérence de la capture, arrêt/redémarrage sécurisé de la source, manifeste exact et restauration from scratch.
