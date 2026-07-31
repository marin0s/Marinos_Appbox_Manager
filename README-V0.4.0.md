# Marinos AppBox Manager V0.4.0 — Fondation mononode

## Périmètre

Cette version consolide ARTEMIS avant la séparation control-plane/agent.

Nodes retenus pour la trajectoire :
- ARTEMIS : node initial
- DEMETER : futur second node
- HADES : hors périmètre AppBox

## Nouveautés

- SQLite devient la source de vérité.
- Import automatique de `appboxes.json` et `jobs.json`.
- File globale persistante avec un worker unique.
- Déploiement, démarrage, arrêt et recréation passent dans la file.
- Inventaire du node local ARTEMIS.
- Dashboard de supervision du node.
- Monitoring CPU, RAM, disque, I/O et réseau.
- Comptage des conteneurs Docker.
- Historique des jobs et événements dans SQLite.
- API :
  - `/api/queue`
  - `/api/nodes/artemis/status`
  - `/api/nodes/artemis/metrics`
- Les fichiers JSON existants sont conservés comme sauvegarde de migration.

## Important

La première mesure de débit I/O/réseau apparaît après environ 10 secondes.

## Installation

```bash
cd /root
unzip marinos-appbox-manager-v0.4.0-artemis.zip
cd appbox-manager-poc-v0.4.0
chmod 755 upgrade-v0.4.0-artemis.sh
./upgrade-v0.4.0-artemis.sh
```

## Contrôles

```bash
curl -s http://127.0.0.1:8090/health | jq
curl -s http://127.0.0.1:8090/api/nodes/artemis/status | jq
curl -s http://127.0.0.1:8090/api/queue | jq
```

## Suite prévue

- V0.4.1 : étapes détaillées de workflow.
- V0.4.2 : rollback et déploiement intelligent.
- V0.4.3 : notifications Discord.
- V0.4.4 : refresh ciblé local.
- V0.4.5 : watchdog et monitoring avancé.
- V0.5 : séparation control-plane/agent.
- V0.6 : multinode manuel ARTEMIS + DEMETER.
