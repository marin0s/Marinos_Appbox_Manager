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

### Packaging de l’agent

Exécuter `python scripts/package_agent.py`, puis `python scripts/package_agent.py --check`.
Le ZIP contient exclusivement les fichiers listés dans le script, en LF avec métadonnées fixes et sans compression dépendante de zlib. Le test compare l’artefact complet, et reconstruit le même ZIP depuis un checkout CRLF. `.gitattributes` impose LF aux sources Python, shell et systemd. Pour revenir au package précédent, restaurer le ZIP et ses sources depuis le même commit, jamais séparément.

`/health.reference_build_intrusive_actions` est vrai : la découverte reste en lecture seule, mais une capture peut arrêter Plex temporairement.

La version produit reste `1.6.0-alpha.5` en développement. L’agent se déclare `1.6.0-alpha.5-dev` tant que cette version n’est pas livrée, tandis que les rapports et capacités du builder Plex identifient précisément l’implémentation `1.6.0-alpha.5-phase1` et le schéma d’archive `1`. Ces champs de capacité sont optionnels afin de rester compatibles avec le Control Plane existant.

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

Le builder lit l’état Docker initial. Si Plex est actif, il demande un arrêt propre et attend l’état `exited` avant de capturer. La restauration est exécutée dans un bloc `finally` : une source initialement arrêtée reste arrêtée ; une source initialement active est redémarrée, puis l’état `running` et l’endpoint Plex `/identity` sont vérifiés. Le contrôle interroge d’abord Plex depuis l’hôte via l’adresse IP fournie par Docker ; `127.0.0.1:32400` n’est utilisé que lorsque Docker confirme le mode réseau `host`. `curl` ou `wget` dans le conteneur restent un repli secondaire et leur absence seule ne provoque pas l’échec. Une erreur de redémarrage ou de santé est remontée comme erreur explicite de restauration et n’est jamais masquée par une erreur de capture.

Chaque base canonique figée est d’abord copiée, avec ses éventuels sidecars SQLite, dans un répertoire privé inscriptible de l’agent. SQLite consolide cette copie sans modifier la source, puis produit dans l’overlay une base canonique contrôlée par `quick_check`. Les sidecars de travail sont supprimés et les fichiers WAL/SHM ne sont jamais archivés. Le rapport indique le nom, les tailles, le SHA-256, le moteur et le résultat de validation ; en cas d’échec, il distingue l’étape et le chemin source ou destination et joint des diagnostics expurgés sur les permissions, propriétaires, accès, espace disque, commandes SQLite, cycle Docker et nettoyage temporaire.

### Vérification et rollback opérateur

Les tests de Phase 1 utilisent uniquement un `/config` synthétique et des primitives Docker simulées. Pour une vérification contrôlée sur un node non-production, comparer le manifeste retourné avec le contenu du tar, vérifier l’absence des chemins exclus, puis confirmer que Plex a retrouvé son état initial. En cas d’échec de restauration signalé par le builder, ne pas relancer automatiquement une capture : vérifier l’état Docker du conteneur source et le redémarrer explicitement avant toute nouvelle tentative.
