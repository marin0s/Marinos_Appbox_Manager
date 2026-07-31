# AppBox Manager 1.2.2 — Sprint 3 UI Refresh

Version finale de validation du Sprint 3.

## Correctif

Après une action distante réussie sur une AppBox (`Start`, `Stop`, `Restart` ou `Recreate`), l'interface :

1. affiche la réussite de l'action ;
2. indique que l'inventaire et la réconciliation sont en cours d'actualisation ;
3. recharge automatiquement la page après 1,8 seconde.

Le rechargement est protégé contre les déclenchements multiples et le cache du JavaScript principal est invalidé par une nouvelle version d'asset.

## Périmètre inchangé

Cette mise à niveau ne modifie pas :

- l'agent installé sur ARTEMIS ;
- le moteur d'exécution distant ;
- `docker-compose.yml` et `.env` sur CRONOS ;
- la base SQLite ;
- les AppBox ou leurs répertoires persistants.

## Historique de modification

- **1.2.2** : actualisation automatique de l'interface après les actions de cycle de vie.
- **1.2.1** : hotfix du cycle de vie distant et restauration de l'architecture CRONOS → agents.
- **1.2.0 Phase 1** : moteur de déploiement distant sécurisé.
