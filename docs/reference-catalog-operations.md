# Catalogue, déploiements et caches de Reference Images

## Cycle de vie d’une référence

Une référence `published` est proposée pour les nouvelles AppBox. L’action **Retirer de la publication** passe uniquement `reference_images.status` à `retired` et conserve `current_version_id`, toutes les versions, archives, copies de cache, AppBox et traces historiques. Une référence retirée reste visible mais `deployment_images()`, `parse_deployment_image()` et le téléchargement d’archive la refusent pour un nouveau déploiement.

**Republier** remet la référence à `published` seulement si sa version courante existe et reste elle-même publiée. Lorsqu’une archive centrale est présente, son SHA-256 doit correspondre au checksum publié et son contenu doit satisfaire le contrat d’archive ; ces lectures ont lieu hors verrou SQLite. La source initiale peut avoir disparu. En l’absence d’archive, une source conforme au contrat de déploiement existant doit encore être disponible. Cette transition ne reconstruit rien et ne change ni version, ni chemin d’archive, ni checksum. Répéter Retirer sur une référence déjà retirée ou Republier sur une référence déjà publiée est un succès idempotent sans nouvelle mutation.

**Supprimer définitivement** est une opération distincte. Elle réutilise le plan et la machine d’état de `reference_deletion.py`. Une image doit d’abord être retirée et reste bloquée par une AppBox dépendante, un profil, un build, un déploiement actif, une distribution, une commande, un job, une purge de cache non finalisée ou une ressource partagée. Il n’existe pas de suppression forcée.

## Déploiements Control Plane

Les états `planned`, `queued`, `preparing`, `prepared`, `deploying`, `running`, `restoring` et `awaiting_claim` sont actifs. Les états `success`, `completed`, `failed` et `cancelled` sont terminaux.

La page Déploiements calcule une projection sans modifier automatiquement l’historique :

- `active` : état actif récent ou activité associée encore présente ;
- `stale` : état actif sans job/commande associé et sans mise à jour depuis le seuil configuré ;
- `inconsistent` : état actif avec `completed_at` déjà renseigné ;
- `reconcilable` : ancien workflow incomplet dont l’AppBox prouve localement la réussite ;
- `terminal` : état terminal.

Le seuil `APPBOX_DEPLOYMENT_STALE_SECONDS` vaut 86 400 secondes par défaut et ne peut pas descendre sous 300 secondes. Les nouveaux jobs et commandes de déploiement transportent le `deployment_id`, utilisé comme corrélation exacte. Pour les lignes legacy qui ne possèdent pas cet identifiant dans leurs payloads, seule une action `deploy` du même client et du même node est considérée ; elle est attribuée au deployment le plus récent qui existait à la création de l’activité. Une opération `start`, `stop`, `restart`, `recreate` ou `claim` récente sur la même AppBox, comme un deploy attaché à un deployment plus récent, ne bloque donc pas la décision portant sur un ancien deployment.

**Régulariser comme terminé** n’est proposé que pour les états historiques plausibles `preparing`, `prepared`, `deploying`, `running` et `awaiting_claim`. L’AppBox du même client doit exister sur le même node, avoir `status`, `desired_state` et `observed_state` à `running`, être `in_sync`, utiliser exactement la même version de référence et ne posséder aucune activité corrélée. L’action conserve l’historique, passe le deployment à `success`, `reconciled`, 100 %, renseigne sa fin et l’audit. Elle ne modifie pas l’AppBox et ne lance aucune action distante. Un état terminal `failed` ou `cancelled` n’est jamais promu.

**Clôturer comme annulé** reste réservé à un état actif stale ou incohérent, sans activité et sans preuve forte de réussite. La réussite démontrée a toujours priorité. La clôture conserve la ligne, passe son état à `cancelled`, renseigne `completed_at` et l’audit, sans supprimer d’AppBox, workspace ou archive et sans envoyer de commande distante.

La section repliée **Historique obsolète** contient seulement les opérations stale sans activité et sans AppBox opérationnelle. Sa prévisualisation affiche chaque identifiant, client, node, statut, âge et raison. La confirmation en masse ne transmet aucune liste faisant autorité : le serveur relit et recalcule tous les candidats dans une transaction, puis applique uniquement la même transition locale vers `cancelled`. Aucune donnée historique n’est corrigée au démarrage.

## Cache local et archive centrale

`node_reference_cache` décrit une copie distribuée sur un node. Cette copie est différente de l’archive centrale appartenant à la version. La page **Caches références** affiche node, image/version, taille, checksum, chemin déclaré et dates de contrôle.

Une purge manuelle crée une opération durable dans les tables existantes `reference_image_deletions` / `reference_image_deletion_nodes`, avec le type `cache`. Le Control Plane reprend exactement le chemin, checksum et taille persistés : aucun chemin fourni par l’opérateur n’est accepté. L’agent exécute la commande existante `reference_cache_delete`, qui applique ses contrôles de confinement et d’identité.

Un node offline laisse l’opération `pending`; un envoi est `in_progress`; la confirmation d’absence donne `success`; une erreur agent donne `failed` et peut être retentée. Le poll normal de la file par l’agent appelle la réconciliation du node avant de choisir une commande : après un redémarrage du Manager ou un retour online, cette réconciliation persistante transforme la tâche pending en commande sans worker supplémentaire. La finalisation supprime uniquement la ligne `(node_id, version_id)` si son chemin et son checksum sont toujours identiques. Une identité changée est conservée et signalée en erreur terminale/retryable. L’image, la version, `current_version_id`, l’archive centrale, les snapshots et les AppBox ne sont jamais modifiés par cette opération. `_finish_catalogue` possède en plus un garde explicite qui refuse le type `cache`.

La suppression complète d’une référence continue d’orchestrer ses propres purges de toutes les copies connues. Une purge manuelle non terminée bloque son preflight afin d’éviter deux nettoyages concurrents.
