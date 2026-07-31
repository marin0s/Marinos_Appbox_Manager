# Images de référence

Une image de référence est une capture assainie d'une instance multimédia servant à créer de nouvelles AppBox déjà préparées.

## Plex

La référence doit préserver :

- bibliothèques, collections et catalogue ;
- `Metadata/` et `Media/` ;
- bases SQLite dans un état cohérent ;
- plugins, scanners et profils utiles ;
- préférences non liées à l'identité.

Elle doit retirer les identifiants et tokens du serveur source ainsi que les caches, logs, diagnostics, transcodes et fichiers temporaires.

Les chemins de médias vus dans le conteneur doivent rester identiques entre la source et la cible. Les fichiers vidéo ne sont pas intégrés à l'archive : ils restent servis par RDAD.

Pendant une capture complète, l'arrêt temporaire de Plex est autorisé lorsque nécessaire à la cohérence. Le builder doit enregistrer l'état initial et redémarrer la source dans tous les chemins de sortie.

## Contrat d’archive Plex — Phase 1

L’archive est construite depuis le montage `/config` d’un conteneur Plex arrêté. Elle contient uniquement les données applicatives suivantes sous `Library/Application Support/Plex Media Server/` :

- `Metadata/` ;
- `Media/` ;
- les bases canoniques `.db` de `Plug-in Support/Databases/` ;
- `Plug-ins/`, `Scanners/` et `Profiles/` ;
- `Resources/` lorsqu’il existe ;
- `Preferences.xml` après suppression des attributs d’identité et de compte.

`Metadata/` et `Media/` désignent ici les données internes de Plex présentes dans `/config`. Les films et épisodes montés depuis RDAD ne sont jamais parcourus ni ajoutés à l’archive.

Sont exclus les caches, logs, crash reports, codecs, diagnostics, sessions, transcodes, répertoires temporaires, PID, WAL/SHM, fichiers temporaires et sauvegardes de bases datées ou dupliquées. Les liens symboliques et liens physiques ne sont pas archivés.

### Cohérence et restauration de la source

Le builder lit l’état Docker initial. Si Plex est actif, il demande un arrêt propre et attend l’état `exited` avant de capturer. La restauration est exécutée dans un bloc `finally` : une source initialement arrêtée reste arrêtée ; une source initialement active est redémarrée, puis l’état `running` et l’endpoint Plex `/identity` sont vérifiés. Une erreur de redémarrage ou de santé est remontée comme erreur explicite de restauration et n’est jamais masquée par une erreur de capture.

Chaque base canonique est copiée vers l’overlay assaini avec le mécanisme SQLite pris en charge, contrôlée par `quick_check` lorsque possible et décrite par son nom, ses tailles, son SHA-256, son moteur et son résultat de validation. Les fichiers WAL et SHM ne sont jamais archivés.

### Vérification et rollback opérateur

Les tests de Phase 1 utilisent uniquement un `/config` synthétique et des primitives Docker simulées. Pour une vérification contrôlée sur un node non-production, comparer le manifeste retourné avec le contenu du tar, vérifier l’absence des chemins exclus, puis confirmer que Plex a retrouvé son état initial. En cas d’échec de restauration signalé par le builder, ne pas relancer automatiquement une capture : vérifier l’état Docker du conteneur source et le redémarrer explicitement avant toute nouvelle tentative.
