# Architecture

AppBox Manager utilise un modèle control plane / agents.

Le Control Plane sur CRONOS conserve l'état métier, les jobs, les décisions de placement et la file de commandes. Les agents interrogent l'API, exécutent localement les opérations Docker puis renvoient leur résultat et leur inventaire.

Le dispatcher de jobs CP réserve atomiquement les jobs dans SQLite et lance au plus
un exécuteur de job par node. Une attente distante sur ORION ne monopolise pas ARTEMIS ;
les jobs d'un même node et le worker métier de chaque agent restent séquentiels.
Les commandes AppBox doivent être claimées sous 60 s par défaut
(`APPBOX_AGENT_CLAIM_TIMEOUT_SECONDS`, minimum 5 s). Le délai et le lien au job sont
portés par des métadonnées du payload existant. L'API de poll et
le waiter appliquent la même expiration, avec transition conditionnelle sous verrou.
Après restart, les jobs running sont finalisés en `failed` et leurs commandes encore
queued annulées ; les jobs queued sont conservés. Voir [suppression et reprise](appbox-deletion-hotfix.md).

Les agents annonçant `appbox_delivery_ack` reçoivent une commande AppBox selon
`queued → offered → claimed`. Le GET crée une offre courte et renvoie un token aléatoire ;
seul son SHA-256 est persisté. Une offre non ACKée avant
`APPBOX_DELIVERY_ACK_SECONDS` (15 s par défaut, minimum 5) retourne à `queued` avec un
nouveau token. L'agent appelle ensuite l'ACK avant toute I/O métier. L'ACK atomique
devient `claimed`, démarre lease et deadline, et reste idempotent avec le même token :
la perte de sa réponse HTTP ne double donc pas l'exécution. Un ancien token est refusé.

Les agents annonçant `appbox_command_lease` séparent la liveness du processus et celle
du worker métier. `worker_activity_at` reste vide tant qu'aucun ACK/ownership réel n'a
été reçu. Le heartbeat maintient le node online et, lorsque le runtime déclare
l'identifiant exact de la commande claimed qu'il possède, renouvelle son ownership sans
inventer de progression fonctionnelle. Une offre seule ne renouvelle rien. Le
renouvellement reste borné par `command_deadline_at`, calculé une seule fois à l'ACK.
Le canal progress est une télémétrie UX best effort indépendante : il ne renouvelle ni
`lease_expires_at` ni `worker_activity_at`. Une commande sans heartbeat propriétaire,
ou arrivée à son deadline global, devient terminale et son résultat tardif ne modifie
plus AppBox, job ou ports.

Un Control Plane mis à jour accepte les agents antérieurs : faute de capability ACK,
il conserve leur claim historique au GET. Cette voie ne bénéficie pas de la relivraison
d'offre et doit seulement servir à la transition. Déployer le nouveau package agent sur
chaque node avant de considérer la garantie de delivery active. Voir
[provisioning distribué alpha.5](appbox-provisioning-alpha5.md).

Le runtime observé par les agents est la source de vérité pour l'état des conteneurs. La base conserve l'état désiré et les informations métier. Le moteur de réconciliation compare les deux.

Les communications sont initiées par les agents vers le Control Plane. Aucun accès Docker distant direct depuis CRONOS n'est requis.

## Cycle de vie des références

La bibliothèque et la fiche ne possèdent aucun moteur métier propre. Le wizard crée un
`reference_build` et un job global durable, puis réutilise découverte, preflight, capture,
upload, validation et publication. Le job global est la source de vérité ; les deux
`agent_commands` discovery/build sont des sous-jobs et ne le terminent pas. La progression
de capture est fondée sur `bytes_written / estimated_payload_bytes`, projetée de façon
monotone, puis les jalons validation/publication terminent à 100 %. Pour une nouvelle
version, `reference_builds.image_id` désigne explicitement
la référence cible. La publication conserve cet `image_id`, insère une nouvelle version
et bascule `current_version_id`; l’ancienne version devient historique dans l’UX.
Une lease sur la commande longue est renouvelée par le heartbeat indépendant. Son
expiration échoue le build sans double exécution ni résultat tardif accepté. L’annulation
coopérative emprunte le même heartbeat et rend build/job/commande `cancelled` après
nettoyage agent. Le preflight disque est rejoué avant capture avec réserve configurable.
La migration ajoute uniquement des colonnes nullable/default aux tables existantes. Voir
[le cycle de vie UX](reference-lifecycle.md).

La résolution d'une archive publiée ne relit pas ses dizaines de Go : le checksum
persisté au moment de la publication est l'identité immuable. Les seules transactions
sous `db_lock` prennent puis revalident un snapshot de métadonnées. Hash legacy,
compression, copie et inspections de fichiers s'exécutent hors verrou ; une transition
concurrente vers suppression ou une republication fait échouer la revalidation plutôt
que de livrer un résultat devenu obsolète. La dernière vérification d'état et l'insertion
de la commande partagent ensuite une transaction : la commande queued bloque la
suppression, ou une suppression déjà verrouillée bloque l'enqueue.

## Suppression des Reference Images

Le Control Plane pilote une opération persistante par image/version. Il verrouille
logiquement les versions, émet des commandes de purge confinées aux caches adressés par
SHA-256, puis supprime archive centrale et catalogue après validation transactionnelle.
Les caches de nodes offline restent des tâches `purge_pending` et sont réconciliés au
poll suivant, sans accès entrant ni blocage global. Les AppBox déjà restaurées sont
autonomes et leur lien catalogue est détaché; provisioning et opérations actifs restent
des blockers. Voir [le contrat de suppression](reference-image-deletion.md).

## Disponibilité et boucles agent (alpha.5, lot 2)

La disponibilité distante est dérivée exclusivement de `node_agents.last_heartbeat`,
horodaté à réception par le Control Plane. `nodes.status`, `node_agents.status` et
`nodes.last_seen` historiques ne peuvent rendre un agent disponible. Le même calcul
alimente Nodes/Agents/détail, API, placement manuel/automatique, provisioning, exécution
des jobs et remise d'une commande de déploiement. Une maintenance par flag ou tag
prime sur le badge ; `agent_online` indique séparément si l'agent répond encore.
Sans heartbeat, avec une date invalide/sans fuseau/future : unknown. Âge <= timeout :
online ; âge > timeout : offline. CRONOS local est identifié par le processus répondant,
pas par SQLite : il n'est pas un agent Docker et n'est jamais éligible aux AppBox.

`APPBOX_AGENT_ONLINE_SECONDS` vaut 180 par défaut (minimum 30), exposé dans Compose.
Le serveur recommande une cadence <= timeout/3, au plus 60 secondes. L'agent respecte
cette recommandation, plafonne son intervalle configuré à 60 secondes et conserve une
cadence plus courte si configurée. Déployer d'abord le Control Plane, puis les agents.
Aucune migration de schéma ou modification des configurations/secrets existants.

Trois boucles : worker métier séquentiel ; heartbeat léger ; collecte métriques et
inventaire. Aucun appel Docker/collecte n'est exécuté par le heartbeat. Les demandes
manuelles d'inventaire et les fins d'opérations réveillent la seule boucle de collecte ;
le résultat de commande signifie alors collecte planifiée, pas inventaire déjà actualisé.
Les requêtes HTTP restent bornées ; aucune exemption au timeout pendant une capture.

Le nouvel endpoint authentifié `/api/agent/v1/{node_id}/metrics` conserve la date du début
de collecte. Un heartbeat sans métriques ne change ni les valeurs ni leur horodatage.
Les samples anciens/dupliqués sont ignorés ; les dates invalides/futures sont refusées.
La fraîcheur des métriques est distincte de la liveness (même seuil pour ce lot).
Les valeurs anciennes restent consultables avec `metrics_fresh`, `metrics_stale` et
`metrics_age_seconds`, indépendamment de `status` et `heartbeat_age_seconds`.
`execution_capable` décrit la capacité de l’exécuteur et devient faux si un bail worker
est déjà expiré ; le statut de l’agent reste online tant que son heartbeat est frais.
Le placement automatique exclut les métriques expirées avec un motif explicite
« metrics stale : capacité non fiable », sans modifier le statut online.
Le placement manuel/deploy reste autorisé avec un avertissement `provisioning_warning`.
START/STOP/RESTART/RECREATE/CLAIM ne dépendent ni de l'âge des métriques ni d'un ancien
état RDAD. Ils gardent les contrôles heartbeat, capacité d'exécution, maintenance et
état métier. La maintenance bloque les démarrages/recréations/claim ; l'arrêt reste
possible sur un agent joignable pour permettre sa mise en maintenance, comme auparavant.
`automatic_placement_allowed` et son motif sont distincts de `provisioning_allowed`
(placement manuel). Les contraintes techniques restent vérifiées par l'exécuteur cible. Les horloges agent/CP doivent être synchronisées.
Les anciens heartbeats avec métriques restent acceptés pour une transition CP d'abord.
Un nouvel agent face à l'ancien CP ne dispose pas du endpoint metrics : mettre à jour
les deux composants avant validation. Aucun changement au moteur de capture ni au claim.

### Vérification et rollback

Tests synthétiques : `python -m pytest -q tests/test_node_liveness.py tests/test_agent_loops.py`.
Sur un nœud explicitement autorisé : observer une capture > 180 s avec heartbeat continu,
arrêter seulement le service agent puis vérifier offline après le délai et refus de
placement manuel/automatique ; redémarrer et constater online. Activer maintenance,
vérifier sa priorité malgré les heartbeats, puis désactiver. Vérifier qu'une collecte
bloquée rend les métriques expirées mais ne coupe pas le heartbeat. Vérifier que START,
RESTART, RECREATE et le placement manuel restent autorisés, avec avertissement ; seul
le placement automatique exclut le nœud pour capacité non fiable. Aucun arrêt de Plex
nécessaire pour ces contrôles de liveness ; aucune intervention sur DEMETER.

Sauvegarder CP et les deux fichiers Python agent avant installation. Pour mettre à jour
un agent existant, installer ensemble le script et reference_contract.py puis redémarrer
son service, sans réexécuter l'enrôlement et sans remplacer agent.json. Rollback : restaurer
CP et ces deux fichiers depuis le même commit précédent, redémarrer le seul service agent.
Les historiques/archives/AppBox ne sont pas modifiés. L'ancien agent bloque à nouveau son
heartbeat pendant une commande longue : cette limite réapparaît en cas de rollback.


## Upgrades agent — alpha.5

Le Control Plane possède le package officiel et les tables additives `agent_upgrades`
et `agent_upgrade_runtime`. Il réserve le node, fige une copie par SHA-256 et émet une
commande `agent_upgrade`. L'exécution métier reste séquentielle ; heartbeat et télémétrie
restent indépendants. Le heartbeat identifie le processus par nonce, PID, version,
build et checksum de release, capturés une seule fois au démarrage.

L'agent prépare sans changer son exécutable. Un watcher systemd réveille un petit lanceur fixe
hors du service agent, qui invoque le helper de la release controller. Ce helper revalide l'archive, journalise previous/candidate et
les délais, arrête seulement le service agent, remplace atomiquement `current` puis
redémarre. Il exige un nouveau heartbeat correspondant au processus MainPID actif.
L'absence de confirmation déclenche le retour à previous et la vérification de l'ancien
agent. Le journal local et sa file de notifications survivent aux redémarrages.

Le statut upgrade ne remplace jamais la liveness. Une opération expirée ne masque pas
un heartbeat expiré. Le verrou reste conservateur si la phase d'activation n'a pas de
résultat terminal ; ne jamais le supprimer aveuglément pour débloquer le placement.
CRONOS reste exclusivement Control Plane. Aucun changement au moteur de capture Plex,
aux données AppBox, au claim ou à la règle métriques stale du lot 2.

Les modules helper/client/contrat sont intégralement versionnés. N supervise N+1 et
conserve le rollback ; le contrôleur ne passe à N+1 qu'après heartbeat et probe de son
helper. N+1 supervise ensuite N+2. Le lanceur peut appeler le contrôleur rescue si le
dispatch échoue. Une enveloppe de récupération ABI 1 et les anciennes releases sont
conservées. L'unité agent versionnée est installée atomiquement, rechargée par systemd
et restaurée avec previous en cas d'échec. Seuls le lanceur minimal et l'unité de base de
son service restent fixes, afin que la candidate ne puisse supprimer la voie de secours.
Le timer et le scheduling sont migrés depuis chaque release confirmée avec sauvegarde,
daemon-reload et reprise durable. Un drop-in ExecStopPost épinglé à cette release reste
indépendant d'un controller/rescue plus ancien ou d'une candidate cassée. Le watcher
PathChanged surveille la demande durable ; le timer rapide n'est actif que pendant le
travail restant, y compris rollback, notifications non acquittées et migration d'unités.
Enabled mais arrêté en idle, il assure aussi un contrôle au boot. Aucun daemon ajouté,
ni changement de l'ABI 1 ou de la liste stricte des fichiers ZIP.

La procédure manuelle de remplacement des deux fichiers du lot 2 ci-dessus ne doit
plus être utilisée sur un agent managed : suivre [bootstrap, upgrade et rollback](agent-upgrades.md).
