# Provisioning AppBox distribué — fiabilité alpha.5

## Sources de vérité

SQLite conserve les jobs, commandes, décisions, AppBox et réservations. Le statut du
node vient du heartbeat reçu par le Control Plane. La santé de son exécuteur est
distincte : `active_commands`, `oldest_claim_age_seconds`, `worker_lease_status` et
`executor_health` décrivent le worker. Des métriques stale ne rendent jamais le node
offline et ne bloquent pas seules START/STOP/RESTART/RECREATE/CLAIM ou un placement
manuel techniquement valide.

La capacité active exclut `generated/not_deployed` et `deleted/history`. Les projections
exposent séparément running, stopped, not_deployed, missing et deleted. Une AppBox
generated est présentée comme **Configuration créée — non déployée**.

Les jobs utilisent `queued`, `running`, puis `success`, `failed` ou `cancelled`. La
migration convertit les anciennes lignes `error` en `failed`; `error` reste uniquement
un état métier historique possible d’une AppBox, pas un état actif de job.

## Bail du worker et compatibilité

Un nouvel agent annonce `appbox_command_lease` et `appbox_progress`. Au claim, le CP
crée un bail de 180 secondes par défaut, configurable par
`APPBOX_COMMAND_LEASE_SECONDS` (minimum 30). Le heartbeat léger transmet
`active_command_id` et renouvelle le bail uniquement si cette commande est encore
claimed sur ce node. Cette preuve d'ownership met à jour `worker_activity_at` sans
changer le stage, le détail ou le pourcentage. Le renouvellement ne dépasse jamais
`command_deadline_at`, calculé une fois au claim avec
`APPBOX_COMMAND_MAX_RUNTIME_SECONDS` (7200 s par défaut).

Le canal progress est distinct et best effort. Il utilise un reporter asynchrone avec
timeout court (`command_progress_timeout_seconds`, 5 s par défaut), coalescence et logs
tentative/résultat/durée. Une panne de ce canal ne bloque pas checksum/extraction et ne
renouvelle pas le bail. Les réponses 404/409/410 ou l'annulation renvoyée déclenchent
l'arrêt coopératif au prochain callback. Si le process ou le worker disparaît, le bail expire,
commande et job passent `failed` et la relance doit être décidée après inspection du
runtime. Un résultat après expiration est accepté au niveau transport mais ignoré avec
un événement `late_agent_result_ignored`. Après restart, l’agent ne reçoit que les
commandes queued : une commande terminale n’est jamais reprise. Le node garde un
`executor_health=stalled` durable après expiration; seul un nouveau poll provenant de
la boucle worker (`worker_last_poll_at`) prouve sa reprise et rétablit sa capacité.

Les agents alpha.5 déjà installés qui n’annoncent pas ces capacités continuent à
fonctionner selon le chemin historique : aucun bail ne leur est imposé et les phases
non observables sont indiquées comme télémétrie indisponible. Leur mise à jour est
requise pour détecter un worker figé avec heartbeat encore sain.

## Progression

Le workflow deploy persiste les phases validation node/stockage/manifeste, préparation neutre, cache,
checksum, validation archive, extraction, SQLite, personnalisation runtime, fichiers,
Compose, attente runtime/health, refresh, watchdog et notification. Les pourcentages
de phase et du job ne diminuent pas. `preparing` ne démarre aucune étape Docker et
`docker_deploy` ne passe running qu'au report `compose_deployment`. Un résultat final peut confirmer une phase déjà
running; une phase non applicable ou non observable est `skipped`, jamais déclarée
faussement exécutée. Le job global n’atteint 100 % qu’à son état terminal.

## Ports et placement

Une réservation active est unique par `(node_id, port, protocol)`. Le node est toujours
le `selected_node_id`; le test de sockets local ne s’applique qu’au node local, jamais à
CRONOS pour une cible distante. Plex et Tautulli sont réservés dans la même transaction
que l’AppBox. Un échec logique annule la transaction et supprime le staging central.
La synchronisation est idempotente : elle recrée une réservation manquante, suit un
changement de node et libère les lignes sans AppBox active. La suppression définitive
libère aussi les ports. Deux nodes peuvent utiliser le même numéro.

Le placement automatique exige le tag configuré `appbox-node`, refuse le tag exclu,
`bare-metal`, maintenance, CRONOS, heartbeat/capacité absents, métriques de capacité
stale et rôle ambigu. Le placement manuel Bare-Metal reste possible seulement si
`allow_manual_bare_metal=1`, avec confirmation si
`require_confirmation_bare_metal=1`.

## Réconciliation non destructive

Une ligne deleted est projetée en desired=deleted. Un dossier central ou conteneur
encore présent produit `cleanup_required`; rien n’est supprimé implicitement. Une
AppBox active sans conteneur devient missing/partial. Un conteneur arrêté attendu ne
publie naturellement aucun port : ce cas ne produit pas `port_drift`. Les réservations
orphelines sont libérées sans campagne de nettoyage distante.

## Recette E2E ARTEMIS / JDMRY (à exécuter seulement après autorisation)

1. Sauvegarder SQLite et relever pour `ab34ah` : état, StartedAt et RestartCount de
   `plex-appb-34ah`. Relever l’agent version/build/SHA et le timeout de bail.
2. Vérifier ARTEMIS online, metrics séparées, tags `appbox-node`/`media`, hors
   maintenance et sans commande claimed antérieure.
3. Créer JDMRY sans déployer. Vérifier `generated`, `not_deployed`, aucun job/commande,
   et les réservations Plex/Tautulli sur ARTEMIS, pas CRONOS.
4. Déployer. Observer dans le job les phases cache, checksum, archive, extraction,
   SQLite, runtime, configuration, Compose et santé. Vérifier une progression monotone,
   `worker_activity_at` et le renouvellement du bail par heartbeat pendant les phases longues,
   même si le canal progress est momentanément indisponible.
5. Sur une AppBox jetable, arrêter réellement l'agent et attendre l'expiration du bail :
   commande/job deviennent `failed` et le node devient OFFLINE au timeout heartbeat.
   Dans un second essai jetable, réduire `APPBOX_COMMAND_MAX_RUNTIME_SECONDS` en gardant
   heartbeat et ownership actifs : au deadline, commande/job deviennent `failed`, le node
   reste ONLINE mais son exécuteur est stalled jusqu’à reprise du poll. Ne pas faire ces
   essais sur `ab34ah`.
6. Envoyer/simuler le résultat tardif de cette commande : commande/job restent failed,
   AppBox et ports inchangés, événement explicite présent. Redémarrer l’agent et vérifier
   que cette commande n’est pas redistribuée.
7. Vérifier collision refusée sur ARTEMIS et même port autorisé sur un autre node de
   test. Supprimer définitivement l’AppBox jetable et vérifier les ports released.
8. Vérifier API/UI : vrais running_jobs/queued_jobs, compteurs active/running/stopped/
   not_deployed/deleted, Bare-Metal exclu en automatique et confirmation manuelle.
9. Rejouer la réconciliation : stopped sans port drift; deleted avec artefact présent
   signalé sans suppression. Confirmer que `ab34ah` a toujours le même StartedAt et
   RestartCount.

## Rollback et limites

Rollback applicatif : arrêter le Control Plane, restaurer ensemble son code et la
sauvegarde SQLite antérieure, puis redémarrer. Pour l’agent managed, utiliser le rollback
de release documenté dans `agent-upgrades.md`; ne pas remplacer agent.json. Un rollback
vers un ancien agent retire la protection de bail AppBox et la télémétrie détaillée.

Les tests locaux simulent Docker, le réseau et Linux. Restent à valider sur ARTEMIS :
durées et débit réels d’un gros cache, cadence des callbacks SQLite/tar, comportement
Compose/systemd lors d’une coupure réseau, permissions du staging et fidélité des
compteurs après inventaire réel. Aucune action de génération, réconciliation ou restart
agent ne cible un conteneur Plex existant; seule la commande Compose du projet client
nouvellement demandé est exécutée.
