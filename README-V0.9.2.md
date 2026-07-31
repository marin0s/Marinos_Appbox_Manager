# V0.9.2 — Agent UI Fix

La V0.9.1 échouait sur les bases existantes car le nom `node_metrics`
était déjà utilisé par les métriques historiques du dashboard.

Correction :

- les snapshots d’inventaire des agents utilisent désormais
  `agent_node_metrics` ;
- aucun conflit avec l’ancienne table `node_metrics` ;
- page Nodes restaurée ;
- page Agents restaurée ;
- bouton « Générer le jeton d’installation » visible pour chaque node distant ;
- édition et suppression des nodes conservées ;
- rollback automatique conservé.

Après installation, DIONYSOS et DEMETER doivent apparaître dans Agents avec
le bouton de génération, même s’ils sont `Bare-Metal` et hors ligne.
