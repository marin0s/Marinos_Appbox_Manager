# Marinos AppBox Manager V1.1.0 — Sprint 2

Moteur de réconciliation distribué : Desired State / Observed State, détection Missing, Drift, arrêts manuels, écarts de ports et conteneurs orphelins.

Après mise à jour du Control Plane, réinstaller les agents depuis **Nodes > Installer l’agent**.

API :
- `GET /api/reconciliation`
- `POST /api/reconciliation/{node_id}/run`
- chaque inventaire agent déclenche automatiquement une réconciliation du node.
