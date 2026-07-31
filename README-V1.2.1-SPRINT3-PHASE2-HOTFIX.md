# AppBox Manager 1.2.1 — Sprint 3 Phase 2 Hotfix

Correctif construit depuis la Phase 1 stable.

## Fonctions
- actions distantes Start, Stop, Restart et Recreate ;
- `jobs.node_id` enregistré avec le node cible ;
- exécution par l’agent du node cible ;
- boutons distincts Déployer / Démarrer / Arrêter / Redémarrer / Recréer ;
- archive agent régénérée et vérifiée pendant l’upgrade.

## Sécurité de mise à niveau
Le script ne remplace jamais :
- `docker-compose.yml` ;
- `.env` ;
- `data/` et la base SQLite ;
- `generated/` ou les runtimes existants.

Le script refuse la validation finale si `appbox-manager-artemis` apparaît sur CRONOS.
