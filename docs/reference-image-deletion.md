# Suppression sûre des Reference Images — alpha.5

## Périmètre et dépendances

La suppression porte soit sur une ancienne version, soit sur une image entière et toutes
ses versions. Le catalogue central utilise `reference_images`,
`reference_image_versions`, `reference_builds`, `catalog_snapshots`,
`reference_image_distribution` et `node_reference_cache`. Les consommateurs inspectés
sont `appboxes`, `provisioning_profiles`, `control_plane_deployments`,
`snapshot_deployments`, les `jobs` et les `agent_commands` actifs. Les opérations et
leur progression par node sont conservées dans `reference_image_deletions` et
`reference_image_deletion_nodes`; `audit_log` conserve démarrages, refus, résultats et
erreurs.

Les fichiers centraux connus sont l’archive publiée, sa copie `deployment-cache` et le
marqueur de capture déclaré sous `APPBOX_REFERENCE_ROOT`. Une source importée externe
n’est jamais supprimée. Chaque node peut détenir une copie adressée par SHA-256 sous son
`reference_cache_dir`.

Une AppBox restaurée est autonome : `recreate` réutilise son Compose et sa configuration
existants et ne restaure pas l’archive. Sa référence catalogue est donc détachée pendant
la finalisation, sans toucher ses données, conteneurs ou fichiers. Un profil de
provisioning reste en revanche un usage futur et bloque. Les déploiements terminaux sont
conservés comme historique et leur lien catalogue est détaché; tout déploiement ou job
reprenable/en cours bloque.

## Prévisualisation et refus

Depuis **Images de référence**, une ancienne version éligible propose **Supprimer**.
L’image complète possède une action séparée. La page affiche la cible, les versions, la
taille connue, les archives, les caches et nodes, les éléments conservés, ainsi que tous
les motifs de refus. La version courante/default et une image publiée/active sont
refusées; alpha.5 ne promeut jamais implicitement une autre version.

Sont également bloqués : profil de provisioning, déploiement actif, job ou commande
active mentionnant la cible, build/capture actif, distribution `transferring`, snapshot
ou archive partagé, et toute autre suppression non terminée sur la même image. Un chemin
central relatif, traversant, hors storage, remplacé, lié ou désignant un répertoire est
refusé. Il n’existe aucun force-delete.

La confirmation est liée par SHA-256 au plan exact. Supprimer une image impose aussi de
saisir son nom exact. Les GET ne suppriment rien. L’API expose :

- `GET /api/reference-images/{image_id}/deletion`;
- `GET /api/reference-images/{image_id}/versions/{version_id}/deletion`;
- `DELETE /api/reference-images/{image_id}` avec `confirmation` et `confirmed_name`;
- `DELETE /api/reference-images/{image_id}/versions/{version_id}`;
- `GET /api/reference-deletions/{operation_id}`.

L’authentification opérateur et la protection CSRF restent celles du Control Plane; la
confirmation protège surtout d’un plan devenu obsolète.

## Machine d’état et ordre des opérations

Le preflight est recalculé dans une transaction SQLite `BEGIN IMMEDIATE`. L’opération
durable est créée en `running`, puis les versions passent à `deleting`. Des triggers
refusent alors une nouvelle publication, distribution, restauration ou association.
Il n’existe aucun verrou conservé pendant l’attente réseau; les brèves transactions sont
nécessairement sérialisées par SQLite, et les suppressions d’une même image sont
explicitement sérialisées.

Chaque cache connu reçoit une tâche persistante. Un node online et compatible reçoit
`reference_cache_delete`; les autres restent `pending`. Lorsque toutes les commandes
émises ont répondu, une mutation catalogue complète est d’abord exercée dans un
savepoint puis annulée. Ceci vérifie les FK et triggers avant de toucher les fichiers.
L’archive centrale est ensuite supprimée, puis la mutation DB est appliquée : liens
autonomes/historiques détachés, caches centraux nettoyés, snapshots sans autre
propriétaire marqués `deleted`, version ou image supprimée. Builds, snapshots, journal
d’opération et audit restent consultables.

États visibles :

- `running` : au moins une purge distante émise attend son résultat;
- `purge_pending` : catalogue et archive centraux supprimés, un node offline reste à purger;
- `partial` : une purge ou un nettoyage a échoué; les métadonnées de retry sont conservées;
- `deleted` : catalogue, archive et tous les caches connus sont confirmés absents.

`phase`, `progress`, `detail`, `error_code`, dates et résultats par node permettent de
distinguer preflight, purge nodes, validation DB, archive centrale, nettoyage DB et fin.
Un retry réutilise le même `operation_id`; fichier/cache déjà absent vaut succès. Un
double clic ne crée ni seconde opération ni seconde commande utile.

## Nodes offline et sécurité agent

Un node offline ne bloque pas la suppression centrale. Sa ligne durable conserve node,
version, chemin et checksum; l’opération reste `purge_pending`. À son prochain poll
authentifié, le Control Plane réémet seulement sa purge, puis passe à `deleted` après ACK
`cache_absent=true`. Aucun autre node et aucune file AppBox ne sont bloqués.

L’agent accepte uniquement le fichier exact
`<reference_cache_dir>/<sha256>.tar.gz`. Racine absolue non liée, checksum hexadécimal,
version bornée, chemin exact, fichier régulier et checksum réel sont obligatoires. Racine,
traversée, chemin arbitraire, symlink, répertoire ou contenu différent sont refusés. Les
erreurs permission, filesystem read-only ou I/O remontent; aucune erreur n’est masquée.

## Migration, reprise et exploitation

Au démarrage, le Control Plane ajoute les colonnes manquantes à
`reference_image_deletions`, crée `reference_image_deletion_nodes`, son index et les
triggers. Aucune commande de migration manuelle ni nouvelle configuration n’est requise.
Les opérations anciennes `cleanup_pending` restent rejouables.

Avant E2E, sauvegarder ensemble la base SQLite et `APPBOX_REFERENCE_ROOT`. Tester une
ancienne version non courante avec un cache sur ARTEMIS online et ORION offline : vérifier
ACK ARTEMIS, `purge_pending`, archive centrale absente, AppBox existante inchangée, puis
retour ORION et transition `deleted`. Tester ensuite une erreur de permission, corriger
le filesystem et relancer la même opération. Redémarrer CP pendant `running` et l’agent
après claim de la commande; confirmer qu’aucune cible différente n’est supprimée et que
les métadonnées de reprise subsistent.

Ne pas utiliser DEMETER. Sur Linux réel, valider les montages, permissions, `fsync`,
comportement des fichiers ouverts et des symlinks avec les sauvegardes disponibles. Un
rollback après suppression physique exige la restauration cohérente de la base et des
archives; le journal n’est pas une sauvegarde des données.
