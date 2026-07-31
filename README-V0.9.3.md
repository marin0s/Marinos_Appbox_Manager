# V0.9.3 — Heartbeat Fix

Correction du HTTP 500 lors du premier heartbeat d'un agent distant.

Cause :
- la lecture utilisait bien `agent_node_metrics` ;
- mais l'écriture du heartbeat ciblait encore l'ancienne table historique `node_metrics`.

La V0.9.3 écrit désormais exclusivement les snapshots agent dans
`agent_node_metrics`.
