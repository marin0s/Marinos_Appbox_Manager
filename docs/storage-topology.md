# Topologie distribuée des Volume Mounts

## Modèle

Un `storage_mounts` décrit une ressource logique : chemin hôte Linux, chemin conteneur,
lecture seule, propagation, caractère requis et types Plex/Jellyfin. La colonne historique
`node_id` reste en base afin de ne pas casser les installations alpha antérieures, mais
elle ne limite plus la définition à ce node. La disponibilité appartient à
`node_storage_paths`, indexée par `(node_id, host_path)`.

`requires_mountpoint=1` exige un vrai point de montage (`os.path.ismount`) en plus de
l'existence du répertoire. Une définition peut donc représenter aussi bien un simple bind
local qu'une racine NFS. Les lignes historiques migrent avec `0` afin de conserver leur
contrat antérieur fondé sur l'existence du chemin. Pour une base neuve, les données seed
déclarent explicitement RDAD comme chemin existant et les deux NAS comme mountpoints ;
ce choix vient des données de définition, sans règle codée sur leurs noms. Le formulaire
des nouvelles définitions permet à l'opérateur de choisir explicitement la politique.

## Collecte et fraîcheur

Le heartbeat renvoie seulement la liste des chemins logiques activés. Il n'effectue aucun
`stat`, calcul de capacité ou accès NAS. L'agent conserve cette liste puis l'observe dans
sa boucle telemetry, déjà séparée du worker métier et du heartbeat. Le payload inventory
peut inclure `storage_paths` avec :

- `path`, `exists`, `mounted`, `collected_at` ;
- facultativement `filesystem`, `source`, `mount_type`, `total_bytes`, `free_bytes`,
  `used_bytes`.

Le Control Plane n'accepte que les chemins absolus déjà configurés, ignore les chemins
inconnus ou traversants et date lui-même la réception. Il ne supprime pas une ancienne
observation lorsqu'un payload omet le champ, afin que l'état devienne naturellement
`stale` plutôt que de paraître frais. La fraîcheur vaut 180 secondes par défaut et se
configure avec `APPBOX_STORAGE_OBSERVATION_SECONDS` (minimum 30 secondes).

Les états sont :

- `available` : observation fraîche, chemin présent et, si exigé, mountpoint réel ;
- `absent` : observation fraîche mais chemin absent ou simple répertoire alors qu'un
  mountpoint est exigé ;
- `unknown` : aucune observation valide ou agent sans capacité
  `storage_observations` ;
- `stale` : dernière réception plus ancienne que le timeout.

CRONOS produit ses observations locales dans son collecteur de métriques et les persiste
dans la même table. Cela ne rend pas CRONOS éligible au placement AppBox.

## Provisioning

Le groupe est résolu après application du profil et contre le node réellement retenu.
Le même résolveur sert au placement manuel et automatique :

- un mount `required` dans tout état autre que `available` bloque avant création du
  workspace, réservation de port ou génération Compose, avec le node, le chemin et la
  raison ;
- un mount optionnel `absent` sur une observation fraîche ne bloque pas, mais il est omis
  du snapshot `appbox_mounts` et du Compose. La raison est conservée dans la décision de
  placement et visible dans la matrice Stockage ;
- un mount optionnel `unknown` ou `stale` bloque le provisioning : l'absence n'étant pas
  confirmée, l'omettre produirait une AppBox ambiguë et le conserver risquerait de laisser
  Docker créer un faux répertoire local ;
- avant un `deploy` ou `recreate` ultérieur, tous les mounts déjà inscrits dans le
  Compose sont revalidés. Si l'un d'eux n'est plus confirmé, même optionnel à l'origine,
  l'opération est bloquée plutôt que d'exécuter un Compose devenu dangereux ;
- le placement automatique rejette le candidat dont le stockage requis est indisponible
  et peut choisir un autre AppBox-Node conforme.

Le snapshot des mounts réellement retenus reste attaché à l'AppBox. Les Reference Images,
leurs archives, caches et règles de suppression ne sont pas modifiés.

## Compatibilité et déploiement progressif

La migration crée additivement `node_storage_paths` et ajoute
`storage_mounts.requires_mountpoint` avec la valeur compatible `0`. Elle est idempotente
et ne réécrit ni ne supprime les définitions, groupes, profils, Compose ou AppBox existants. Les anciens
payloads inventory sans `storage_paths` restent acceptés. Un ancien agent sans capability
est affiché `unknown`; une AppBox existante continue à fonctionner, mais un nouveau
provisioning exigeant un mount requis attend une observation d'un agent mis à jour.

Ordre conseillé : déployer le Control Plane, mettre à jour les agents, attendre un cycle
heartbeat puis inventory, vérifier la matrice Stockage, et seulement ensuite créer une
AppBox utilisant un groupe requis.

## Recette E2E CRONOS / ORION / ARTEMIS

À exécuter uniquement après autorisation terrain :

1. Sauvegarder SQLite et les Compose concernés. Relever version/build/SHA des agents.
2. Créer une définition jetable pointant vers un mount présent sur ORION et absent sur
   ARTEMIS. Attendre un heartbeat puis un inventory de chaque node.
3. Vérifier dans Stockage `ORION = AVAILABLE`, `ARTEMIS = ABSENT`, avec âge et raison.
   Vérifier que CRONOS possède son propre état mais reste exclu du placement AppBox.
4. Rendre le mount requis : le placement manuel ARTEMIS doit répondre 409 avant toute
   AppBox, réservation ou workspace ; ORION doit être accepté.
5. En automatique, vérifier qu'ARTEMIS figure dans les rejets stockage et qu'ORION est
   retenu si les autres critères sont satisfaits.
6. Rendre le mount optionnel et absent : créer une AppBox jetable sur ARTEMIS, puis
   vérifier que son `compose.yml` et `appbox_mounts` ne contiennent pas ce chemin.
7. Arrêter seulement la collecte inventory en laissant les heartbeats : le node reste
   ONLINE tandis que le mount devient `STALE`; un required ne peut plus être provisionné.
8. Tester un répertoire ordinaire avec « vrai point de montage » activé : état ABSENT.
   Désactiver cette option sur une définition jetable : état AVAILABLE.
9. Confirmer qu'aucune Reference Image, version, archive ou cache n'a changé.

## Rollback

Restaurer le code et le package agent précédents. Les agents antérieurs ignorent la liste
`storage_paths` et le Control Plane antérieur ignore la nouvelle table/colonne SQLite.
La migration étant additive, aucune migration descendante n'est requise. Pour revenir au
comportement antérieur d'un environnement de test, restaurer la sauvegarde SQLite plutôt
que supprimer manuellement des tables. Les AppBox déjà générées conservent leur snapshot
de mounts et leur Compose ; inspecter ces fichiers avant toute recréation.
