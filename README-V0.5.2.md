# Marinos AppBox Manager V0.5.2 — Phase 2 Business Model

## Objectif

SQLite devient l’inventaire métier central du Control Plane.

## Nouvelles tables

- `containers`
- `networks`
- `volumes`
- `templates`
- `port_reservations`
- `settings_store`
- `notifications_queue`

Les tables existantes restent conservées :

- `nodes`
- `appboxes`
- `jobs`
- `job_steps`
- `events`
- `node_metrics`

## Inventaire Docker

Une synchronisation inspecte le daemon Docker local et enregistre :

- image et image ID ;
- état et healthcheck ;
- ports ;
- labels ;
- montages ;
- réseaux ;
- rattachement à l’AppBox ;
- volumes et réseaux Docker.

## Réservations de ports

Les ports Plex et Tautulli existants sont importés dans une table persistante. La réservation ne dépend plus uniquement de `ss` ou de l’état courant des conteneurs.

## Interface

Nouvel onglet **Inventaire** avec :

- conteneurs ;
- réseaux ;
- volumes ;
- ports réservés ;
- modèles Plex/Jellyfin ;
- synchronisation manuelle Docker.

## Installation

```bash
cd /root
unzip marinos-appbox-manager-v0.5.2-artemis.zip
cd appbox-manager-poc-v0.5.2
chmod 755 upgrade-v0.5.2-artemis.sh
./upgrade-v0.5.2-artemis.sh
```
