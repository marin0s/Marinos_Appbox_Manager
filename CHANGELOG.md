# Changelog

## 1.6.0-alpha.5 — En développement

### Refresh RDAD ciblé multi-node
- Nouveau moteur Python versionné dans l’agent : découverte locale des AppBox Plex par labels officiels, endpoint et `/config` résolus depuis Docker, token jamais journalisé et sections déterminées par leurs Locations `/data` plutôt que par IDs ou titres.
- Files persistantes indépendantes adressées par identité node/client/conteneur, déduplication et timestamps, lecture FUSE réelle, defer/retry Plex busy ou indisponible, isolation complète entre AppBox et conservation explicite des queues orphelines.
- Boucle agent indépendante sans nouvelle unité systemd. Le moteur reste inactif tant que le timer/service historique `sync-decypharr-catalogs` fonctionne, puis prend le relais après bascule opérateur; compatibilité temporaire isolée pour `plex-appb-34ah`.
- Cadences séparées : les queues restent traitées toutes les 60 s tandis que le scan catalogue partagé est limité à 300 s par défaut, avec baseline immédiat pour toute nouvelle cible et zéro parcours catalogue sur un node sans AppBox Plex locale.
- Package agent régénéré avec `rdad_refresh.py`; architecture, canary, second node, rollback et interface du futur watchdog documentés dans `docs/rdad-targeted-refresh.md`.

### Lifecycle du catalogue de références
- Les déploiements historiques réellement aboutis peuvent être régularisés volontairement en `success` à partir de preuves fortes sur l’AppBox (même node et référence, runtime running/in_sync, aucune activité corrélée). Les vrais zombies restent annulables séparément, à l’unité ou après une prévisualisation bulk restrictive ; aucune action distante ni réécriture automatique au démarrage.
- La page Déploiements sépare les éléments à traiter, les terminés et l’historique obsolète dans des sections repliables. Les longs détails sont tronqués dans la carte et restent intégralement consultables.
- Une Reference Image publiée peut désormais être retirée du catalogue sans toucher à sa version courante, son archive centrale, ses caches, ses snapshots ni ses AppBox, puis republiée sans rebuild et sans changement de checksum.
- Les références retirées restent consultables mais sont refusées pour tout nouveau déploiement. Leur suppression définitive réutilise le preflight existant et reste bloquée par toute AppBox, opération, build, distribution ou purge active.
- La page Déploiements distingue les opérations actives, terminales, anciennes et incohérentes. Une clôture manuelle vers `cancelled` est proposée seulement en l’absence de job ou commande agent active ; l’historique est conservé et aucune action distante n’est lancée.
- La page dédiée aux caches de références expose les copies par node et leur identité. Une purge manuelle utilise exclusivement `reference_cache_delete`, demeure durable lorsqu’un node est offline et ne modifie jamais le catalogue central ni les AppBox.
- Le seuil d’ancienneté des déploiements sans activité est configurable par `APPBOX_DEPLOYMENT_STALE_SECONDS` (86 400 s par défaut, minimum 300 s). Documentation : `docs/reference-catalog-operations.md`.

### Topologie distribuée des Volume Mounts
- Les définitions `storage_mounts` sont désormais des ressources logiques réutilisables ; leur ancien `node_id` est conservé pour compatibilité mais ne détermine plus la disponibilité.
- Les agents observent les chemins demandés dans la boucle inventory/metrics indépendante et publient existence, état mountpoint et métadonnées optionnelles. Le heartbeat reste léger et transmet seulement la configuration des chemins à observer.
- Le Control Plane persiste les observations par `(node_id, host_path)`, expose `available`, `absent`, `unknown` ou `stale` dans une matrice par node, et utilise un timeout configurable de 180 s (`APPBOX_STORAGE_OBSERVATION_SECONDS`).
- Le provisioning résout le groupe sur le node réellement sélectionné : un montage requis non confirmé bloque avec une erreur explicite ; un montage optionnel non confirmé est omis du Compose pour éviter la création silencieuse d'un bind local vide.
- Migration SQLite additive : les définitions historiques conservent la sémantique « chemin existant » (`requires_mountpoint=0`), tandis que les nouvelles définitions peuvent exiger explicitement un véritable mountpoint. Les anciens payloads inventory/agents restent compatibles et leur stockage demeure `unknown` jusqu'à mise à jour. Un mount optionnel n'est omis que sur absence fraîchement confirmée ; `unknown/stale` bloque une décision ambiguë. Le cycle de vie des Reference Images reste inchangé.

### Fiabilité du provisioning AppBox distribué
- Hotfix lifecycle des ports et création transactionnelle : les index Plex/Tautulli excluent uniquement les AppBox définitivement `deleted`, tandis que l’allocator fusionne réservations actives et ports de toutes les AppBox non terminales (`stopped`, `error`, `missing`, `not_deployed` inclus). La migration conserve l’historique des ports et reconstruit les index sous savepoint. Toute collision SQLite devient un HTTP 409 métier ; AppBox, réservations, montages, décision, déploiement, événement et éventuel job sont commit ensemble, avec rollback du seul workspace neuf dont le marqueur client/node est exact.
- Livraison fiable des commandes AppBox compatibles selon `queued → offered → ACK → claimed` : une réponse GET perdue ne crée plus de commande orpheline, une offre non confirmée est relivrée avec un nouveau token et l'ACK est idempotent si sa réponse se perd. Lease, activité worker et deadline ne démarrent qu'après ownership confirmé ; le chemin historique reste disponible pendant la migration des agents.
- Les archives Reference Images publiées utilisent leur SHA-256 immuable persisté sans rehash synchrone. Snapshot/revalidation de catalogue seuls restent sous `db_lock`; hash legacy, compression et autres grosses I/O sont hors verrou et une suppression/republication concurrente invalide proprement la préparation.
- La préparation centrale d'une référence possède une phase UX dédiée sans démarrer Docker prématurément. Les workspaces Control Plane sont marqués, supprimés uniquement après preuve distante et vérification stricte ; un orphelin non vérifié produit HTTP 409 au lieu d'un traceback.
- Les commandes `appbox_action` des agents compatibles utilisent un bail worker de 180 s par défaut (`APPBOX_COMMAND_LEASE_SECONDS`) renouvelé par l’ownership déclaré dans le heartbeat indépendant, sans confondre liveness et progression UX. Un deadline global configurable (`APPBOX_COMMAND_MAX_RUNTIME_SECONDS`, 7200 s par défaut) empêche une commande de devenir immortelle. Une expiration termine commande et job en `failed`; les résultats tardifs sont ignorés et audités.
- Correction du blocage du premier POST progress : le endpoint ne reprend plus `db_lock` via la mise à jour du déploiement. Les reports sont envoyés par un canal best effort borné et journalisent tentative, résultat, timeout et durée sans secret. `preparing` reste neutre et Docker ne passe running qu’au stage `compose_deployment`.
- Les phases cache, checksum, validation archive, extraction, SQLite, personnalisation runtime, fichiers de configuration, Compose et attente runtime remontent une progression persistée et monotone. Les agents alpha.5 antérieurs restent acceptés sans bail et leurs phases non observables sont signalées comme telles.
- Les réservations Plex/Tautulli sont atomiques et rattachées au `selected_node_id`; un même port est permis sur deux nodes, jamais deux fois sur le même node/protocole. La synchronisation libère les réservations orphelines et suit un changement de node sans suppression distante implicite.
- Le placement automatique exige explicitement `appbox-node`, exclut `bare-metal`, maintenance, CRONOS et les rôles ambigus. Le placement manuel Bare-Metal respecte l’autorisation et la confirmation configurées.
- Les états historiques `jobs.error` migrent vers le terminal canonique `failed`. Les compteurs de jobs distants proviennent de SQLite; les AppBox generated/deleted sont distinguées de la capacité active et l’UX affiche « Configuration créée — non déployée ».
- La réconciliation marque les lignes deleted avec un état cohérent, signale dossiers/conteneurs encore présents sans les détruire et n’invente plus de port drift pour un conteneur arrêté.

### Fiabilité et progression des Reference Builds
- Fix Reference Builder worker lease expiration during long captures: les agents compatibles utilisent désormais `queued → offered → ACK → claimed`; lease et `worker_activity_at` ne commencent qu’après ACK et sont renouvelés par le heartbeat indépendant, même si la progression reste fixe pendant plusieurs heures. Les agents legacy restent acceptés sans lease artificiellement non renouvelable.
- Une expiration réelle persiste la demande d’annulation, maintient le build terminal, refuse upload/résultat tardifs et laisse l’agent interrompre l’I/O puis nettoyer son staging. L’ACK et le résultat terminal sont idempotents ; aucune version supplémentaire n’est publiée lors d’un retry HTTP.
- Un seul workflow global couvre désormais découverte, preflight, capture, validation et publication ; les commandes agent restent des sous-jobs techniques et ne peuvent plus afficher un faux succès à 100 %.
- Progression de capture remontée périodiquement depuis les octets réellement écrits, persistée de façon monotone et limitée en fréquence. États, libellés et durées de l’inspecteur reflètent les données réellement disponibles.
- Preflight du filesystem temporaire bloquant avec besoin `payload estimé + max(5 GiB, 10 %)`, marge configurable et second contrôle juste avant la capture.
- Annulation coopérative et état `cancelled` : signal par heartbeat indépendant, interruption de l’écriture/transfert, nettoyage des temporaires et terminaison cohérente du build, job et de la commande.
- Lease configurable de 180 s par défaut sur les captures longues, renouvelée par heartbeat ; une commande orpheline devient failed et un résultat tardif ne relance ni publication ni second worker.
- Migration SQLite additive des champs de lease, progression et annulation. La capture Plex live reste non intrusive et ne stoppe/redémarre jamais la source.

### Compatibilité du bootstrap legacy monolithique
- Bootstrap des agents alpha.4 monolithiques sans `reference_contract.py`, ainsi que des installations modulaires : agent principal obligatoire, contrats/client optionnels uniquement s'ils sont absents. Fichiers présents invalides ou illisibles refusés avant réservation/activation.
- Snapshot des seuls fichiers runtime réellement installés, sans copie de modules de la candidate ; identité déterministe calculée sur leurs octets. Lecture statique de la déclaration historique `VERSION`, sans changer le contrat des packages managed.
- Rollback vers le snapshot monolithique, retry après préflight échoué avec lock libre existant ; configuration, identité, launcher ABI, scheduler et hotfix suppression/file inchangés. Générations supportées et recette ORION documentées dans `docs/agent-upgrades.md`.

### Hotfix suppression AppBox et file multi-node
- Delete/purge idempotents lorsque dossier, Compose ou conteneurs sont déjà absents ; archive conserve les données. Vérification Docker réelle avant nettoyage, erreurs disque/daemon conservées, chemin et propriété des conteneurs contrôlés.
- Exécuteur de suppression partagé par agent et mode embedded, sans nouveau composant ZIP ; suppression locale réelle interdite en mode mock. Réparation des AppBox legacy error/deleted/missing jusqu'à l'inventaire, l'audit et la notification.
- Dispatcher CP avec une voie séquentielle par node : ORION ne bloque plus ARTEMIS. Délai de claim persistant de 60 s par défaut, configurable ; commandes expirées non distribuées et résultats tardifs non appliqués aux commandes terminales.
- Restart CP : jobs interrompus finalisés en erreur et commandes non claimées associées annulées, y compris legacy ; jobs queued conservés. Aucun changement de schéma SQLite ni du mécanisme de liveness ou de suppression des Reference Images.
- Recette et limites documentées dans `docs/appbox-deletion-hotfix.md`.

### Supervision agent sans polling permanent en idle
- Compatibilité forward des packages managed : le manifeste authentifié déclare une liste extensible mais exacte et hashée, avec racine de fichiers obligatoires, limites de taille/nombre, chemins plats et contrôles explicites protocol/launcher ABI.
- Les agents déjà déployés avec l'ancienne allowlist passent automatiquement par un package bridge reproductible, strictement compatible avec N, puis une seconde opération atomiquement chaînée installe le package complet contenant les nouveaux modules comme `rdad_refresh.py`.
- Les échecs de préparation exposent désormais un code borné distinguant téléchargement, taille, SHA, manifeste, ensemble de fichiers, checksum, chemin, protocole, ABI et préparation locale, sans contenu sensible.
- Réveil systemd sur la demande durable, timer rapide seulement pendant upgrade/reprise/notifications ; contrôle au boot puis retour idle, sans nouveau daemon.
- Migration automatique du timer 5 s des nodes managed depuis une release confirmée : path/drop-in connus, écritures atomiques, sauvegarde et reprise/restauration durables, daemon-reload et activations asynchrones vérifiées.
- Scheduling versionné épinglé indépendamment de current/controller/rescue ; lanceur et service de base fixes, agent.json, protocole/ABI 1, liste ZIP et rollback agent inchangés.
- Tests idle/actif/reboot, acquittements terminaux, courses, contention, migration interrompue, chaîne managed et secours ; procédure de recette Linux dans `docs/agent-upgrades.md`.

### Correctif après bootstrap ARTEMIS
- Un tick du lanceur dont le verrou est déjà détenu quitte silencieusement avec code 0, sans controller/rescue ; les autres erreurs et le secours restent inchangés.
- `/health` et le registre Plex annoncent la capture sans interruption (`intrusive_actions=false`) ; documentation et tests réalignés sans changement du moteur, de l'ABI, du bootstrap ou du rollback.

### Layout du détail node
- Espacement vertical des rangées métriques et Système/Agent via la grille existante, sans modification des breakpoints ni du layout local.
- Critères E2E du lot 2 documentés, dont extinction physique du node et rafraîchissement UI sans restart CP ; aucun changement de liveness.

### Cycle de vie UX des références
- Bibliothèque simplifiée : nom, application, disponibilité, version active, taille, mise à jour et nombre de versions ; seules les actions Gérer et Déployer restent sur les cards.
- Fiche par référence avec métadonnées, source initiale, usages, distributions, historique et labels ACTIVE/HISTORIQUE/EN CONSTRUCTION/EN SUPPRESSION/ERREUR. Les identifiants techniques sont repliés.
- Wizard unique en cinq étapes pour une nouvelle référence ou une nouvelle version. Plex depuis node/serveur ou AppBox utilise le builder existant ; Jellyfin et l’upload navigateur sont annoncés indisponibles sans faux workflow.
- `reference_builds.image_id` cible explicitement une référence existante : publication d’une nouvelle version sous le même image_id, bascule de current_version_id et AppBox existantes inchangées.
- Suppressions déplacées dans l’historique et la zone de danger ; blockers assortis d’actions vers nouvelle version, profil, job, déploiement ou distributions. Recette : `docs/reference-lifecycle.md`.

### Suppression des images de référence
- Suppression UI/API d’une ancienne version ou d’une image inactive, avec plan détaillé, confirmation liée à l’état et saisie du nom pour l’image ; aucune suppression forcée ou promotion implicite.
- Refus des versions active/default, images publiées, provisioning, opérations/déploiements/jobs/builds/distributions actifs et ressources partagées ; AppBox restaurées autonomes préservées avec détachement du lien catalogue.
- Machine d’état persistante, verrouillage logique contre les écrivains concurrents, validation DB avant unlink, nettoyage central idempotent et audit des succès, refus et erreurs.
- Purge agent strictement confinée au cache SHA-256. Nodes offline en `purge_pending`, erreurs en `partial`, reprise au poll et retry avec le même operation_id. Documentation : `docs/reference-image-deletion.md`.

### Upgrades distants des agents
- Déclenchement manuel dans Agents/Nodes ; versions installée/disponible, build, SHA-256, taille et phases observables, distinctes de la liveness.
- Package officiel déterministe avec manifeste ; copies immuables par SHA-256 et téléchargement authentifié par node. Validation stricte avant préparation puis activation ; aucun script d'installation du ZIP exécuté.
- Lanceur systemd minimal indépendant ; helper/client/contrat versionnés, contrôleur sortant conservé pendant activation et rollback, puis relais à la nouvelle release. Unité agent versionnée remplacée atomiquement avec daemon-reload et restauration en cas d’échec. Bascule atomique de current/previous. Confirmation du nouveau processus par heartbeat/version/build/PID et service actif ; rollback automatique vérifié, état et journal de phases durables.
- Bootstrap opérateur unique depuis l'installation legacy, sans réenrôlement ni modification de agent.json. Conservation des fichiers legacy et de la release précédente.
- Verrou d'exclusion avec les commandes/jobs AppBox et Reference Images ; trois boucles agent inchangées. Expiration de la préparation, délais de confirmation/rollback et procédure de reprise documentés.
- Tests locaux avec systemd/symlinks simulés sous Windows ; validation Linux/ARTEMIS et durcissement TLS/authentification opérateur/CSRF restent distincts de cette livraison.

### Disponibilité des nœuds
- Liveness centralisée : heartbeat récent online, expiré offline, absent/invalide unknown ; maintenance prioritaire, timeout configurable de 180 secondes par défaut.
- UI/API et contrôles de placement, provisioning, exécution et remise des commandes utilisent les états dérivés ; CRONOS reste exclu des AppBox.
- Agent : exécution métier séquentielle, heartbeat léger et collecte métriques/inventaire dans trois boucles indépendantes.
- Endpoint metrics séparé ; un heartbeat léger ne réécrit pas les échantillons, les anciens échantillons ne remplacent pas les nouveaux. Métriques expirées signalées séparément : exclusion possible du placement automatique uniquement ; placement manuel et actions existantes restent disponibles avec heartbeat/capacités valides.
- Rafraîchissement des badges de disponibilité et des choix de placement ; suppression du réglage manuel ONLINE.

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
