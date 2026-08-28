# Changelog

## 1.6.0-alpha.5 — En développement

### Préférences runtime Plex
- Références nouvellement capturées neutres : suppression du nom, des mappings de ports et connexions personnalisées source.
- Nouvelles AppBox Plex : nom majuscule et port manuel issus du provisioning, transmis dans le manifeste et appliqués avant démarrage, y compris depuis les anciennes références.
- La personnalisation touche uniquement Preferences.xml, jamais Metadata/Media/DB ; elle ne s'applique pas au claim ni au recreate d'une instance existante.

### Images de référence Plex — Phase 1
- Définition d’un contrat d’archive Plex fondé sur une liste d’inclusion explicite, avec conservation de `Metadata`, `Media`, bases canoniques, plugins, scanners, profils, ressources et préférences assainies.
- Capture à chaud d’un Plex actif sans interruption du service ; les bases SQLite sont figées via `Plex SQLite` dans le conteneur, avec validation `quick_check` et fallback Python SQLite si nécessaire.
- Exclusion des caches, logs, diagnostics, sessions, transcodes, fichiers temporaires, PID, WAL/SHM et sauvegardes de bases datées ou dupliquées.
- Rapports de construction enrichis avec le cycle de vie du conteneur, la confirmation qu’aucun arrêt/redémarrage n’a été effectué pendant une capture live, les contenus réellement archivés, les tailles, les checksums et les validations SQLite.
- Tests synthétiques sans accès à Docker couvrant le contenu de l’archive, l’assainissement, les états initialement actif ou arrêté, les erreurs de capture et la garantie qu’un Plex actif n’est ni stoppé ni redémarré par le builder.
- Harmonisation des versions de développement de l’agent et du builder Phase 1, contrôle `/identity` depuis l’hôte avec repli conteneur, et durcissement des chemins tar hors contrat.
- Correction de l’import `uuid` manquant dans le chemin de snapshot SQLite Plex et alignement du footer, de `/health` et de l’agent embarqué sur la version de validation `1.6.0-alpha.5-dev`.
- Capture SQLite Plex renforcée par une copie privée et inscriptible de la base figée et de ses sidecars, avec erreurs par étape et diagnostics de chemins, permissions, espace disque, moteur, sous-processus, cycle Docker et répertoires temporaires.
- Capture live finalisée : lorsqu’un Plex est `running`, le builder transmet désormais le conteneur au moteur de snapshot, utilise `Plex SQLite` sans `docker stop/start` et conserve le service disponible pendant toute la création de l’archive.

### Corrections de finalisation
- `/health` et les capacités agent déclarent les captures Plex comme non intrusives : un Plex actif reste en ligne pendant la construction de la Reference Image.
- Packaging agent déterministe : contenu LF, ordre, permissions et horodatages fixes ; vérification complète de l’artefact et des checkouts LF/CRLF.
- Contrat d’archive partagé entre Control Plane et agent ; refus des chemins absolus, traversées, liens, fichiers spéciaux, doublons et gzip tronqués. Module livré dans le ZIP et installé avec l’agent.
- Upload en streaming, SHA-256 recalculé, validation avant publication atomique, verrou d’upload et refus de remplacement par un contenu différent. Les résultats répétés ne republient pas une version.
- Conservation du manifeste détaillé et de la version réelle du builder, au lieu de la constante alpha.3.
- Téléchargement vers un cache adressé par SHA-256 ; restauration dans un staging isolé avec validation SQLite, refus d’écraser une AppBox existante et mise à jour de `node_reference_cache` uniquement après confirmation distante.
- Contrôle de l’état Docker et de l’identité HTTP Plex avant succès ; distinction restauration/attente du claim/association confirmée, sans marquer un échec comme running.
- Claim distant : délais explicites, nettoyage des fichiers et recréation sans jeton même après échec, vérification de l’identité et de l’association après nettoyage, erreurs expurgées.
- Assainissement des attributs de claim et secrets dans Preferences.xml tout en conservant les préférences applicatives ; tests de corruption SQLite et de nettoyage.
- Nommage Plex corrigé pour les identifiants `ab-…`, refus des nouveaux identifiants à séparateurs dupliqués ; reconnaissance des anciens noms conservée dans l’inventaire.
- Tests portables : fermeture explicite des connexions SQLite, lecture UTF-8 et synchronisation de répertoire conditionnelle sous Windows. Retrait du suivi des anciens bytecodes Python.

### Documentation et dépôt
- Ajout des fondations du dépôt : `.gitignore`, guide de contribution, contexte agents, architecture, roadmap et templates GitHub.
- Confirmation de `1.6.0-alpha.4` comme version de production et de `1.6.0-alpha.5` comme prochaine livraison.

### Prévu pour cette version
- Fiabilisation complète des images de référence Plex.
- Conservation explicite des bibliothèques, `Metadata`, `Media` et bases cohérentes.
- Capture Plex active sans interruption, avec snapshot SQLite cohérent et conservation de l’état initial du conteneur.
- Validation d'un déploiement from scratch, du claim distant et du nommage sans double tiret.

## 1.6.0-alpha.4 — Base de production importée

- Version de production déclarée dans le dépôt ; import initial du 31 juillet 2026.
- Socle de départ de la release alpha.5 : Control Plane, agents et Reference Images.
- Les imports Git ne permettent pas de reconstituer un delta exhaustif avec alpha.3 ; aucune validation terrain supplémentaire n’est déduite de cet import.

## 1.6.0-alpha.3

- Plex SQLite native hot backup and validation.
- Retry endpoint for failed Reference Builds.
- Correct failed/building status rendering in Reference Images UI.

## 1.6.0-alpha.2

- Capture Plex active rendue cohérente avec sauvegarde SQLite à chaud.
- Suppression du staging complet afin d'éviter de doubler les 35 Go de configuration sur OURANOS.
- Archive construite depuis la source avec overlay assaini.
- Validation SQLite `quick_check` et suppression WAL/SHM.
- 21 tests unitaires.

## 1.5.2 — Reference Images UX — 2026-07-30

### Modifié
- Refonte de la page Images de référence autour de trois parcours clairs : Bibliothèque, Depuis un serveur et Depuis un fichier.
- Suppression du score de compatibilité dans l’interface ; les blocages sont désormais affichés comme erreurs actionnables.
- Simplification du menu Ressources : Images de référence, Déploiements, Agents et Stockage.
- Renommage de Stockage & Références en Stockage.
- Recentrage de Stockage sur les Volume Mounts et groupes de montages.

### Sécurité
- La découverte Plex reste en lecture seule.
- L’import depuis un fichier reste désactivé tant que la validation automatique n’est pas disponible.

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

## 1.4.0 — Sprint 4 Phase 2

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
