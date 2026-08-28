# Images de référence

Une image de référence est une capture assainie d'une instance multimédia servant à créer de nouvelles AppBox déjà préparées.

## Plex

Le protocole de validation terrain et le rollback sont décrits dans [le runbook alpha.5](reference-images-alpha5-e2e.md). Une publication technique de référence ne constitue pas une validation terrain ni une livraison de production.

## Transfert et restauration alpha.5

L’agent et le Control Plane utilisent `agent/reference_contract.py` pour valider le contrat. L’upload est lu par blocs, écrit dans un fichier unique, synchronisé puis contrôlé (SHA-256, gzip jusqu’à EOF, tar et contrat Plex) avant rename atomique. Un verrou par build refuse les uploads concurrents. Après crash, ne retirer un verrou résiduel qu’après confirmation de l’absence d’upload actif. Un build ayant déjà reçu une archive différente doit être recréé, pas écrasé.

La publication conserve le manifeste de l’agent (SQLite, tailles, cycle de vie) et le complète avec les vérifications du Control Plane. La version builder vient du résultat, pas d’une constante alpha.3. Les livraisons répétées d’un résultat publié sont idempotentes.

La distribution est à la demande lors du déploiement, sans nouveau service de prédistribution : le nœud télécharge dans `/var/lib/marinos-appbox-agent/reference-cache`, vérifie puis publie un cache adressé par SHA-256. `reference_cache_dir` permet un emplacement explicitement autorisé. Le tar est entièrement prévalidé avant extraction ; liens, fichiers spéciaux, doublons, chemins Windows/absolus et traversées sont refusés. Le contenu est extrait dans une nouvelle AppBox temporaire, Preferences.xml assaini et les bases validées via une copie privée. Compose et manifeste sont préparés avant renommage de l’AppBox.

Une configuration existante non vide n’est jamais écrasée. `recreate` ne réapplique plus la référence sur les données existantes. Après un échec Docker/health, la configuration préparée est conservée pour diagnostic : pas de suppression automatique de données. Le job échoue et le cache en base n’est pas annoncé ready. Une reprise nécessite inspection opérateur ; créer un nouvel identifiant pour refaire un restore complet.

`node_reference_cache` suit `transferring → ready/failed`. Le ready exige checksum, version et santé confirmés par l’agent ; ce statut décrit la distribution, pas le claim. Le déploiement reste `awaiting_claim`, et le snapshot `restored_unclaimed`, avant le claim explicite depuis l’interface. L’association est ensuite recontrôlée après retrait du jeton et recréation. Aucun claim automatique sans jeton fourni par l’opérateur. Les anciens agents restent compatibles pour les opérations ordinaires, mais une restauration alpha.5 exige le nouveau package et sa confirmation de santé.

Le claim vérifie running, HTTP, identité générée, puis claimed, avec conservation de l’empreinte d’identité pendant les recréations. Les fichiers sont nettoyés sur succès et échec. Si la recréation de nettoyage échoue, le résultat signale une intervention obligatoire : le processus Docker pourrait encore porter le jeton ; ne pas relancer automatiquement. Les résultats entrants sont expurgés avant persistance côté Control Plane.

Les diagnostics conservent les chemins techniques nécessaires au dépannage et ne doivent pas être diffusés publiquement. Les attributs d’identité/claim/token/password/secret sont supprimés des préférences ; cela ne constitue pas un scanner universel de secrets contenus dans les plugins ou les données métier SQLite. Examiner le template choisi avant diffusion hors du périmètre de confiance.

Le fallback SQLite `schema-readable-tokenizer-unavailable` reste une validation limitée quand les tokenizers Plex ne sont pas disponibles. La validation native et la lecture effective des bibliothèques restent obligatoires pendant l’E2E. Le timeout d’attente d’un restore est de deux heures ; le claim dispose de 25 minutes côté Control Plane pour englober les contrôles et les deux recréations Docker, elles-mêmes bornées. Un job déjà interrompu ne reprend pas succès à réception d’un résultat tardif. Les validations lourdes d’upload/publication sont exécutées hors de la boucle HTTP asynchrone pour ne pas bloquer les heartbeats.

La référence doit préserver :

- bibliothèques, collections et catalogue ;
- `Metadata/` et `Media/` ;
- bases SQLite dans un état cohérent ;
- plugins, scanners et profils utiles ;
- préférences non liées à l'identité.

Elle doit retirer les identifiants et tokens du serveur source ainsi que les caches, logs, diagnostics, transcodes et fichiers temporaires.

Les chemins de médias vus dans le conteneur doivent rester identiques entre la source et la cible. Les fichiers vidéo ne sont pas intégrés à l'archive : ils restent servis par RDAD.

Depuis `689c336`, une capture complète d'un Plex running ne provoque aucun stop/restart. Le builder enregistre les états initial et final ; une source déjà arrêtée reste arrêtée.

## Contrat d’archive Plex — Phase 1

### Packaging de l’agent

Exécuter `python scripts/package_agent.py`, puis `python scripts/package_agent.py --check`.
Le ZIP contient exclusivement les fichiers listés dans le script, en LF avec métadonnées fixes et sans compression dépendante de zlib. Le test compare l’artefact complet, et reconstruit le même ZIP depuis un checkout CRLF. `.gitattributes` impose LF aux sources Python, shell et systemd. Pour revenir au package précédent, restaurer le ZIP et ses sources depuis le même commit, jamais séparément.

`/health.reference_build_intrusive_actions=false` et le registre Plex annonce `intrusive_actions_enabled=0`, tout en restant activé (`enabled=1`). La capability agent `reference_builder_intrusive_actions` est également fausse : une source active est capturée sans stop/restart. Le registre existant est réaligné au démarrage du CP, sans migration manuelle ni modification du moteur de capture.

La version produit reste `1.6.0-alpha.5` en développement. L’agent se déclare `1.6.0-alpha.5-dev` tant que cette version n’est pas livrée, tandis que les rapports et capacités du builder Plex identifient précisément l’implémentation `1.6.0-alpha.5-phase1` et le schéma d’archive `1`. Ces champs de capacité sont optionnels afin de rester compatibles avec le Control Plane existant.

L’archive est construite depuis le montage `/config`, avec un snapshot cohérent des bases SQLite. Elle contient uniquement les données applicatives suivantes sous `Library/Application Support/Plex Media Server/` :

- `Metadata/` ;
- `Media/` ;
- les bases canoniques `.db` de `Plug-in Support/Databases/` ;
- `Plug-ins/`, `Scanners/` et `Profiles/` ;
- `Resources/` lorsqu’il existe ;
- `Preferences.xml` après suppression des attributs d’identité et de compte.

`Metadata/` et `Media/` désignent ici les données internes de Plex présentes dans `/config`. Les films et épisodes montés depuis RDAD ne sont jamais parcourus ni ajoutés à l’archive.

Sont exclus les caches, logs, crash reports, codecs, diagnostics, sessions, transcodes, répertoires temporaires, PID, WAL/SHM, fichiers temporaires et sauvegardes de bases datées ou dupliquées. Les liens symboliques et liens physiques ne sont pas archivés.

### Cohérence et restauration de la source

Le builder lit l’état Docker initial. Si Plex est running, les bases sont figées via Plex SQLite dans le conteneur actif ; aucun arrêt ni redémarrage n'est demandé, même en cas d'erreur. Pour une source arrêtée/created, le snapshot utilise Python SQLite sans démarrer le conteneur. Le bloc `finally` relève l'état final, sans action sur le cycle de vie Docker.

Chaque base canonique figée est d’abord copiée, avec ses éventuels sidecars SQLite, dans un répertoire privé inscriptible de l’agent. SQLite consolide cette copie sans modifier la source, puis produit dans l’overlay une base canonique contrôlée par `quick_check`. Les sidecars de travail sont supprimés et les fichiers WAL/SHM ne sont jamais archivés. Le rapport indique le nom, les tailles, le SHA-256, le moteur et le résultat de validation ; en cas d’échec, il distingue l’étape et le chemin source ou destination et joint des diagnostics expurgés sur les permissions, propriétaires, accès, espace disque, commandes SQLite, cycle Docker et nettoyage temporaire.

### Vérification et rollback opérateur

Les tests de Phase 1 utilisent uniquement un `/config` synthétique et des primitives Docker simulées. Pour une vérification autorisée sur un node non-production, comparer le manifeste et le tar, vérifier les exclusions, la continuité de Plex et l'absence de stop/restart dans le rapport. En cas d'échec, conserver les diagnostics et vérifier l'état de la source avant toute nouvelle tentative. Le rollback de cette correction de reporting restaure le CP précédent ; il ne modifie pas le moteur de capture.

## Personnalisation des nouvelles AppBox

Le template est neutre : FriendlyName, ManualPortMappingMode/Port, LastAutomaticMappedPort
et customConnections sont supprimés à la capture. Les anciennes archives publiées restent
acceptées : leur copie restaurée est neutralisée puis personnalisée avant démarrage.
Le manifeste inclut le nom client en majuscules et le port Plex alloué ; la cible applique
FriendlyName, ManualPortMappingMode=1 et ManualPortMappingPort. Aucune donnée catalogue
n'est modifiée. Les agents doivent annoncer plex_runtime_preferences pour un nouveau
provisioning Plex. Le claim/restart/recreate ne réinitialise pas une identité existante.
Vérification : créer un nouvel ID sur un nœud autorisé, comparer les trois attributs avec
le provisioning, puis vérifier claim et lecture. Rollback : restaurer CP et package agent
du même commit précédent ; aucune modification rétroactive des AppBox ou archives.
L'E2E ARTEMIS rapporté par l'opérateur depuis 689c336 valide la chaîne capture live,
publication, transfert/cache, restore, identité, claim, catalogue et lectures ATHENA/RDAD.
Il a révélé les préférences héritées ; cette correction nécessite sa propre validation terrain.
