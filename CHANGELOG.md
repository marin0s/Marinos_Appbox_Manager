# Changelog

## 1.6.0-alpha.5 — En développement

### Documentation et dépôt
- Ajout des fondations du dépôt : `.gitignore`, guide de contribution, contexte agents, architecture, roadmap et templates GitHub.
- Confirmation de `1.6.0-alpha.4` comme version de production et de `1.6.0-alpha.5` comme prochaine livraison.

### Prévu pour cette version
- Fiabilisation complète des images de référence Plex.
- Conservation explicite des bibliothèques, `Metadata`, `Media` et bases cohérentes.
- Arrêt temporaire sécurisé du Plex source lorsque nécessaire, avec redémarrage garanti.
- Validation d'un déploiement from scratch, du claim distant et du nommage sans double tiret.

## 1.6.0-alpha.3

- Plex SQLite native hot backup and validation.
- Retry endpoint for failed Reference Builds.
- Correct failed/building status rendering in Reference Images UI.

# 1.5.2 — Reference Images UX — 2026-07-30

### Modifié
- Refonte de la page Images de référence autour de trois parcours clairs : Bibliothèque, Depuis un serveur et Depuis un fichier.
- Suppression du score de compatibilité dans l’interface ; les blocages sont désormais affichés comme erreurs actionnables.
- Simplification du menu Ressources : Images de référence, Déploiements, Agents et Stockage.
- Renommage de Stockage & Références en Stockage.
- Recentrage de Stockage sur les Volume Mounts et groupes de montages.

### Sécurité
- La découverte Plex reste en lecture seule.
- L’import depuis un fichier reste désactivé tant que la validation automatique n’est pas disponible.

# 1.4.0 — Sprint 4 Phase 2

- Suppression standard transactionnelle avec vérification distante.
- Commit BDD uniquement après absence confirmée des conteneurs et du dossier AppBox.
- Audit et job conservés après suppression.
- Correctif d’archivage et packaging agent systemd.
- Exclusion de `control-plane-runtime/` du script de mise à niveau.


## 1.2.3 — 2026-07-30

- Suppression de la carte CRONOS du menu latéral.
- Suppression du badge Mock dans la navbar.
- Footer et build UI synchronisés avec la version réelle.
- Numéros du changelog normalisés.
- Refresh automatique après un déploiement réussi.
- Confirmation de suppression intégrée à l’application.
- Détails techniques des workflows repliables.
- Carte Provisioning corrigée et responsive.
- Version Jellyfin affichée correctement.

## 1.2.2 — 2026-07-29

- Rechargement automatique après `Start`, `Stop`, `Restart` et `Recreate` réussis.
- Message d'attente pendant la remontée de l'inventaire et de la réconciliation.
- Verrou frontend empêchant plusieurs rechargements pour un même job.
- Cache-busting du JavaScript principal.
- Backend, agent, compose, base et runtimes inchangés.

## 1.2.1 — 2026-07-29

- Cycle de vie distant rétabli depuis la Phase 1 stable.
- Correction de `jobs.node_id`.
- Conservation stricte du runtime CRONOS.

## 1.4.1 — Transaction Engine — 2026-07-30

### Corrigé
- Protection globale du worker contre les exceptions non gérées.
- Suppression SQLite atomique, idempotente et contrôlée par les clés étrangères.
- Fin des jobs de suppression bloqués après `cleanup_files`.
- Audit d'échec garanti pour les workflows interrompus.

### Ajouté
- Récupération des jobs `running` au démarrage du Control Plane.
- Watchdog configurable des workflows bloqués.
- Tests de transaction, rollback et idempotence.
- Script de mise à niveau CRONOS avec sauvegarde et validations SQLite.

## 1.4.2 — Deletion Integrity Hotfix — 2026-07-30

### Corrigé
- Détachement transactionnel de `placement_decisions.client_id` avant suppression d'une AppBox.
- Détachement transactionnel de `control_plane_deployments.client_id` avant suppression d'une AppBox.
- Correction de l'erreur `FOREIGN KEY constraint failed` reproduite sur `TEST141`.

### Tests
- Ajout d'un scénario complet de non-régression avec historiques, inventaires, ports, jobs et notifications.
- Vérification de l'idempotence de la finalisation.
- Vérification du rollback complet lorsqu'une contrainte étrangère inconnue subsiste.

### Correctif installateur 1.4.2-r2
- Les tests et la compilation Python sont désormais exécutés dans l'image Docker de l'application.
- CRONOS n'a plus besoin de recevoir `psutil`, FastAPI ou les autres dépendances Python sur l'hôte.
- Le processus d'upgrade reste autonome depuis `/root`.

## 1.6.0-alpha.2

- Capture Plex active rendue cohérente avec sauvegarde SQLite à chaud.
- Suppression du staging complet afin d'éviter de doubler les 35 Go de configuration sur OURANOS.
- Archive construite depuis la source avec overlay assaini.
- Validation SQLite `quick_check` et suppression WAL/SHM.
- 21 tests unitaires.
