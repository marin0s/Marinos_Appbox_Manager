# Refresh RDAD ciblé, multi-AppBox et multi-node

## Architecture

Deux composants Python séparés sont versionnés dans le package full agent :

- `agent/rdad_catalog_sync.py` réplique exclusivement les quatre catalogues centraux vers le node local avec rsync/SSH. Il ne découvre aucune AppBox et ne contacte jamais Plex ;
- `agent/rdad_refresh.py` reste l’unique moteur de détection des deltas locaux, de queues par AppBox et d’appels de refresh Plex ciblés.

`marinos-appbox-agent.py` les exécute dans la boucle RDAD dédiée, indépendante du heartbeat, de l’inventory/metrics et du worker de commandes séquentiel. À chaque tick, le moteur de sync vérifie sa propre échéance, puis le refresh s’exécute. Une sync réussie force un scan catalogue partagé dans ce même tick ; sinon la cadence normale de scan reste inchangée. Il n’existe pas de nouveau daemon ni de second timer AppBox Manager.

Chaque agent utilise exclusivement Docker local, les mounts locaux, les endpoints Plex publiés sur le loopback du node et ses propres files sous `/var/lib/marinos-appbox-agent/rdad-refresh`. Aucun hostname, IP de node, client, port Plex ou identifiant de section n’est codé dans le chemin principal.

## Réplication des catalogues

La source distante par défaut est `/mnt/media/decypharr` et la destination locale `/mnt/decypharr-poc`. Le host n’a volontairement aucune valeur par défaut : une installation neuve reste inactive tant qu’un opérateur n’a pas configuré le serveur central. Chaque bibliothèque utilise un argv sans shell équivalent à :

```text
rsync -a --delete --links --timeout=<timeout> -e <ssh borné> -- user@host:/source/<library>/ /destination/<library>/
```

Les bibliothèques autorisées sont exclusivement `radarr`, `radarr-4k`, `sonarr` et `sonarr-4k`. Les sources et destinations absolues sont validées, `.mnt` et le traversal sont refusés, et la destination racine doit déjà exister comme véritable répertoire non symlink. Seuls les quatre sous-répertoires directs peuvent être créés ou recevoir `--delete`. Les liens sont copiés comme liens : leurs cibles ne sont ni suivies, ni modifiées, ni copiées.

SSH utilise BatchMode, IdentitiesOnly, StrictHostKeyChecking et un timeout. User, host, clé et chemins sont validés avant construction de l’argv. stdout/stderr rsync sont ignorés et les logs ne contiennent jamais host, chemin, clé, sortie distante ou secret. Un exit non nul ou timeout est enregistré uniquement avec bibliothèque, résultat et code. Une bibliothèque en échec n’empêche jamais les trois suivantes.

L’état `/var/lib/marinos-appbox-agent/rdad-catalog-sync/state.json` conserve atomiquement dernier essai, dernier succès, résultat et code par bibliothèque. La cadence survit donc aux restarts. Les échecs sont eux aussi temporisés afin qu’un host indisponible ne déclenche pas quatre connexions à chaque tick agent.

La source historique `/usr/local/sbin/sync-decypharr-catalogs.sh` n’est pas présente dans le repository et ne peut donc pas être corrigée ou distribuée avec une release. Le nouveau module porte les garanties observées sur le moteur terrain : état persistant, timestamps, déduplication, path ciblé, vérification fichier/symlink, lecture réelle FUSE jusqu’à 1 Mio, détection d’activité Plex, defer et retry.

## Découverte locale

À chaque cycle, l’agent reconstruit la liste depuis `docker ps -aq` et `docker inspect`. Une cible moderne doit porter les trois labels :

```text
marinos.appbox.type=plex
marinos.appbox.id=<client_id>
marinos.appbox.node=<node_id de cet agent>
```

Un Plex arbitraire sans ces labels est ignoré. Un conteneur d’un autre node est ignoré même s’il apparaît dans un fixture ou inventaire erroné. Une nouvelle AppBox apparaît au cycle suivant. Une cible arrêtée reste identifiable mais sa file est différée.

`plex-appb-34ah` possède une compatibilité legacy temporaire, isolée dans `LEGACY_CONTAINER_NAMES`. Elle peut être désactivée avec `rdad_refresh_legacy_34ah=false` après sa migration vers les labels officiels. Aucun autre Plex sans labels n’est accepté.

## Endpoint, configuration et token

Le port hôte est lu dans le binding Docker de `32400/tcp`; l’agent appelle uniquement `http://127.0.0.1:<port>`. Le mount dont la destination est `/config` fournit la racine de configuration. Le token est lu dans `Library/Application Support/Plex Media Server/Preferences.xml` et envoyé dans le header `X-Plex-Token`. Il n’est jamais placé dans l’URL, les files ou les logs.

Un port absent, un Plex inaccessible, un mount `/config` absent, des Preferences absentes/invalides ou un `PlexOnlineToken` absent rendent uniquement cette cible indisponible. Sa queue reste persistée et les autres AppBox continuent.

## Sections Plex

Le moteur interroge `GET /library/sections` et examine les éléments `Location`. Les titres et les IDs numériques ne servent jamais au mapping. Les correspondances fonctionnelles sont :

| Bibliothèque | Location attendue |
|---|---|
| `radarr` | `/data/radarr` |
| `radarr-4k` | `/data/radarr-4k` |
| `sonarr` | `/data/sonarr` |
| `sonarr-4k` | `/data/sonarr-4k` |

Une section peut contenir d’autres Locations NAS. La présence de la Location `/data` exacte suffit. Une réponse XML invalide ou une Location absente diffère la seule queue concernée.

Avant chaque refresh, `/activities` doit être lisible et vide. Pour chaque path, l’agent vérifie son existence dans le conteneur puis lit réellement jusqu’à 1 Mio d’un fichier suivi à travers les symlinks. `rdad_refresh_mode=force` conserve la vérification d’existence mais permet de bypasser le probe de lecture pour un diagnostic opérateur explicite; `readable` est le mode normal.

## Files persistantes et lifecycle des cibles

Une identité de queue est le SHA-256 de `(node_id, client_id, container_id)`. Le nom du conteneur ou du client n’est jamais utilisé directement comme chemin :

```text
/var/lib/marinos-appbox-agent/rdad-refresh/
├── catalog-scan.json
└── targets/<identity-sha256>/queue.json
```

Chaque fichier conserve l’identité complète, les fingerprints horodatés du catalogue, les entrées dédupliquées, leur dernier essai/résultat, `last_seen_at` et `orphaned_at`. Le scan calcule un fingerprint déterministe par film/série de premier niveau plutôt que de recopier l’état de chaque fichier dans chaque AppBox; ajouts, suppressions et modifications restent détectables avec un état persistant borné par le nombre de chemins ciblables. Le premier cycle d’une nouvelle cible établit un baseline sans provoquer un refresh massif. Les changements ultérieurs sont ajoutés indépendamment à chaque cible découverte.

Une cible disparue ne reçoit plus de nouvel événement. Sa queue n’est jamais supprimée : elle est marquée orpheline et reste disponible pour diagnostic. Une nouvelle instance réutilisant le même `client_id` mais possédant un autre `container_id` reçoit une nouvelle identité et ne peut pas hériter de l’ancienne queue. Il n’existe pas encore de garbage collection automatique des orphelins.

La boucle de traitement et le scan catalogue ont deux cadences distinctes. La boucle traite les files existantes toutes les `rdad_refresh_interval` secondes, tandis que le parcours récursif partagé des quatre catalogues est limité à une fois toutes les `rdad_refresh_catalog_interval` secondes. Le premier baseline ou l’apparition d’une nouvelle cible force toutefois un scan partagé immédiat; il n’existe jamais un scan par cible. Si aucune AppBox Plex locale n’est découverte, les anciennes queues sont marquées orphelines puis le cycle termine sans scanner le catalogue.

Le dernier scan achevé est persisté dans `catalog-scan.json`, donc un restart agent ne réinitialise pas artificiellement la cadence. Pour ne perdre aucun changement, le marqueur partagé n’est avancé qu’après l’écriture atomique de l’état catalogue et de la queue de toutes les cibles découvertes. Une erreur de scan ou de persistance conserve l’ancien marqueur et force une nouvelle tentative au cycle suivant. Les files déjà présentes continuent néanmoins d’être traitées pendant les cycles sans scan et après restart.

Une réussite retire seulement l’entrée de cette cible. Token absent, Plex busy/injoignable, XML invalide, section absente, média absent/illisible, arrêt ou disparition du conteneur conservent l’entrée pour un cycle ultérieur. Une erreur sur A n’empêche jamais l’ajout ou le traitement de B.

## Observabilité et watchdog

Les événements sont des JSON sur stdout avec `component=rdad_refresh`, `event`, `node`, `client_id`, `container`, `library`, `section`, `path` et `result` selon le cas. Les événements principaux sont `refresh_target_discovered`, `refresh_target_unavailable`, `queue_add`, `queue_defer`, `refresh_success`, `refresh_failed`, `target_failed` et `cycle_skipped`. Les champs sensibles sont exclus et les textes d’erreur sont bornés/redactés.

La TODO « finaliser refresh ciblé et watchdog » ne doit pas créer un autre déclencheur Plex. Un futur watchdog doit seulement observer les logs structurés et les `queue.json` (ancienneté, dernier résultat, orphan) puis alerter. Le moteur présent reste l’unique producteur/consommateur des files et l’unique appelant de l’API refresh.

## Distribution et coexistence legacy

`rdad_refresh.py` appartient à l’allowlist immuable du package. Une mise à jour managed distribue donc ensemble l’agent, ce module et leur manifeste; le rollback revient à l’ensemble cohérent précédent. L’installeur legacy copie également le module et le bootstrap le snapshotte s’il existe.

Le réglage `rdad_refresh_enabled` accepte `auto` (défaut), `true` ou `false`. Même avec `true`, le moteur refuse de travailler si `sync-decypharr-catalogs.timer` ou `sync-decypharr-catalogs.service` est actif. Cette barrière interdit deux moteurs concurrents. Options complémentaires :

```json
{
  "rdad_refresh_enabled": "auto",
  "rdad_refresh_interval": 60,
  "rdad_refresh_catalog_interval": 300,
  "rdad_refresh_mode": "readable",
  "rdad_refresh_catalog_root": "/mnt/decypharr-poc",
  "rdad_refresh_state_dir": "/var/lib/marinos-appbox-agent/rdad-refresh",
  "rdad_refresh_legacy_34ah": true
}
```

La réplication possède sa configuration indépendante :

```json
{
  "rdad_catalog_sync_enabled": "auto",
  "rdad_catalog_sync_interval": 300,
  "rdad_catalog_sync_host": "catalog.internal.example",
  "rdad_catalog_sync_user": "root",
  "rdad_catalog_sync_identity_file": "/root/.ssh/id_ed25519_decypharr_sync",
  "rdad_catalog_sync_source_root": "/mnt/media/decypharr",
  "rdad_catalog_sync_destination_root": "/mnt/decypharr-poc",
  "rdad_catalog_sync_timeout": 180
}
```

`auto` exige une configuration valide, notamment un host explicite et une clé existante. `false` désactive seulement la réplication ; le refresh peut continuer à traiter une queue existante. Le même fichier agent peut être utilisé sur tous les nodes, avec uniquement ces valeurs de transport adaptées localement.

Sans `rdad_refresh_catalog_root`, la racine est déduite de `rdad_path` : le parent lorsque celui-ci se termine par `.mnt`, sinon le chemin lui-même.

## Canary ARTEMIS

Ces commandes constituent une procédure terrain à exécuter ultérieurement par un opérateur; elles n’ont pas été exécutées pendant le développement.

1. Sauvegarder `agent.json`, l’état du timer legacy, ses files/timestamps historiques et `/var/lib/marinos-appbox-agent/rdad-refresh` s’il existe. Le format du script terrain n’étant pas versionné dans ce repository, ses files ne sont pas importées aveuglément dans le nouveau format.
2. Déployer la release agent validée et configurer la sync native sans arrêter le moteur legacy. Les deux composants doivent indiquer `cycle_skipped` avec `legacy_timer_active` : le timer historique continue alors d’assurer sync et refresh sans concurrence.
3. Vérifier les labels, bindings `32400/tcp` et mount `/config` de `plex-appb-34ah`, JDMRY et P0E2E01. Ne jamais afficher Preferences.xml ou le token.
4. Laisser le moteur historique vider ses files. Si une entrée reste différée, conserver le moteur legacy et résoudre sa cause avant la bascule; ne jamais déclarer cette entrée migrée. Une fois les files vides, arrêter et désactiver le timer legacy, attendre la fin éventuelle du service, puis vérifier les deux unités inactives : `systemctl disable --now sync-decypharr-catalogs.timer` et `systemctl is-active sync-decypharr-catalogs.service`.
5. Redémarrer l’agent ou attendre le cycle suivant. Vérifier quatre `catalog_sync_success`, puis la découverte de 34ah et JDMRY; P0E2E01 doit produire `token_missing` sans erreur globale. Comparer un échantillon de liens CRONOS/local avec `readlink` sans suivre leur cible.
6. Provoquer séparément un vrai changement Radarr puis Sonarr. Vérifier l’ajout dans chaque queue, la section résolue par Location, la lecture FUSE et `refresh_success` pour les cibles claimées.
7. Pendant une activité Plex, vérifier `plex_busy`, la conservation de la queue puis sa réussite au cycle suivant.
8. Créer/supprimer une AppBox de test et vérifier apparition, arrêt des nouveaux ajouts, marquage orphan et absence de réattribution.

## Second node et généralisation

Après un canary stable, répéter sur ORION ou un autre node compatible : sauvegarde, upgrade agent, vérification du skip si un timer legacy existe, arrêt contrôlé de celui-ci, validation de deux cycles et d’un événement Radarr/Sonarr. Confirmer qu’aucun endpoint d’ARTEMIS n’est contacté et que seules les queues locales apparaissent. Généraliser node par node seulement après comparaison des logs et de l’ancien moteur.

## Rollback

1. Passer d’abord `rdad_catalog_sync_enabled` et `rdad_refresh_enabled` à `false` dans `agent.json`, redémarrer l’agent et vérifier les deux `cycle_skipped/result=disabled`. Cette étape ferme la fenêtre où une sync ou un refresh déjà commencé pourrait chevaucher le moteur legacy.
2. Réactiver ensuite le moteur historique : `systemctl enable --now sync-decypharr-catalogs.timer` et vérifier son état.
3. Utiliser le rollback agent managed normal si le code doit revenir à la release précédente. Conserver la configuration/identité d’agent.
4. Conserver les `queue.json`; ne pas les injecter dans le script legacy et ne pas les supprimer pendant l’analyse.
5. Pour réessayer, laisser `rdad_refresh_enabled=false`, vider le moteur legacy, désactiver timer et service, puis seulement remettre `auto` et redémarrer l’agent.

## Dépannage

- `legacy_timer_active` : coexistence protégée, arrêter l’ancien timer seulement pendant une migration approuvée.
- `configuration_invalid` / `identity_unavailable` : corriger host/user/racines ou la clé locale sans jamais afficher son contenu.
- `destination_root_unavailable` : monter/créer explicitement la racine attendue ; l’agent refuse de la créer pour éviter un rsync sur le filesystem racine après perte de mount.
- `rsync_failed` / `timeout` : vérifier réseau, known_hosts, source distante et permissions. Les autres bibliothèques continuent et un nouvel essai aura lieu à l’échéance persistée.
- `token_missing` / `preferences_missing` : réclamer Plex ou réparer son mount `/config`; ne jamais copier un token dans la configuration agent.
- `plex_endpoint_unavailable` : vérifier le binding Docker `32400/tcp`.
- `plex_sections_invalid` / `section_not_found` : vérifier `/library/sections` et les Locations `/data`, sans supposer les IDs.
- `plex_busy` / `activities_unavailable` : attendre; la queue doit rester intacte.
- `media_absent` / `media_unreadable` : vérifier mount `/data`, propagation `rshared`, FUSE et lecture réelle dans le conteneur.
- `orphaned_at` : cible disparue; conserver la queue jusqu’à décision opérateur.
