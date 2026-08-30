# Hotfix alpha.5 — Suppression AppBox et attente distante

## Diagnostic confirmé

L'agent exécutait `shutil.rmtree(app_dir, ignore_errors=False)` sans traiter l'absence
du dossier. Un `down` réussi, y compris « No resource found to remove », pouvait donc
être suivi d'ENOENT. Sans Compose, les erreurs « No such container » de `docker rm`
étaient également fatales. Enfin, toute erreur de `docker inspect` était assimilée à
une absence, y compris une panne du daemon. Le mode embedded exigeait lui aussi un
dossier présent avant suppression.

Le worker CP était globalement séquentiel : il appelait execute_job puis attendait
la commande distante avant de choisir le job suivant. Le head-of-line blocking est
confirmé. Ce n'était pas une boucle strictement infinie : une suppression disposait
d'un timeout de 900 s, un déploiement avec référence de 7200 s. Le waiter rafraîchissait
updated_at, empêchant le watchdog d'identifier cette attente comme un job inactif.
La commande pouvait rester queued après expiration et être exécutée tardivement.

## Suppression

Une primitive de l'agent réalise les contrôles et le nettoyage. Le CP embedded charge
cette même primitive depuis le script agent livré ; aucune duplication du contrôle de
chemin et aucun nouveau membre ZIP incompatible avec les anciens validateurs managed.
Les nodes distants utilisent leur propre copie : **mettre à jour CP et agents concernés**.
Un ancien agent reste compatible avec les métadonnées supplémentaires, mais conserve
son ancien comportement de suppression tant qu'il n'a pas été mis à jour.

- Le chemin est exactement `appbox_base_dir/client_id` (base autorisée par configuration,
  `/srv/appboxes` par défaut). Identifiant invalide, chemin arbitraire, racine elle-même,
  autre client, symlink du dossier/racine et Compose non régulier sont refusés.
- Le CP transmet également le chemin de son inventaire ; l'agent refuse une divergence
  avec sa racine configurée. Ne pas contourner ce refus en élargissant la racine.
- Docker doit répondre à une requête d'inventaire. Les conteneurs sont déterminés par
  l'inventaire fourni, les noms conventionnels historiques et le label Compose du projet.
  Un label appartenant à un autre client est refusé. Sans label, un nom conventionnel
  et un mount source résolu dans le dossier de ce client sont requis. Les noms
  historiques `plex-appb-40ah` restent reconnus pour `ab40ah`/`ab-40ah`.
- Compose présent : `down --remove-orphans`. Compose absent : nettoyage Docker direct.
  Chaque conteneur existant est retiré explicitement ; seules les réponses d'absence
  précises sont tolérées. Une erreur daemon, permissions ou I/O n'est pas un succès.
- Les conteneurs doivent être absents avant le retrait des fichiers. Delete et purge
  retirent le dossier, comme dans la sémantique actuelle du produit ; archive le conserve.
  Un dossier déjà absent ou disparu entre vérification et nettoyage est accepté.
  Aucun `ignore_errors=True`. Les liens internes ne sont pas suivis par rmtree. Sous Linux,
  mountinfo permet de refuser les montages dans le dossier, y compris les bind mounts,
  avant de retirer les fichiers. Docker est revérifié après nettoyage pour détecter
  une recréation concurrente ; elle ne doit pas produire un faux succès.
- Le CP ne finalise la suppression DB qu'après confirmation `path_exists=false` et
  absence explicite des conteneurs (liste vide requise, pas un champ omis). Les étapes cleanup_files, inventory, audit et notification
  terminent normalement ; historiques et intégrité SQLite sont conservés.

Une AppBox encore enregistrée avec une erreur métier / desired_state=deleted / observed_state=missing
est donc réparable par Supprimer/Purger, sans nettoyage SQLite manuel. La primitive
agent et la finalisation DB sont idempotentes. Une seconde requête UI/API renvoie le
job de suppression déjà réussi grâce à original_client_id conservé dans ses options,
sans nouvelle commande distante. Sans entrée catalogue ni historique vérifiable portant
cette identité, la route garde un 404 : elle n'invente pas un succès sur un node inconnu.
En mode embedded mock, une suppression réelle est refusée explicitement ; aucun
appel Docker ni retrait de fichiers n'est exécuté sous couvert de simulation.

## File et délais

Le dispatcher réserve un job sous verrou SQLite puis lance un thread par node actif.
Il ne fait plus lui-même l'attente distante. Il ne lance jamais un deuxième job sur
un node dont le thread précédent est encore vivant, même si le watchdog a finalisé
son état. Les autres nodes avancent indépendamment. Le modèle d'exploitation reste
un seul processus Control Plane sur sa base SQLite, comme avant ce correctif.

`APPBOX_AGENT_CLAIM_TIMEOUT_SECONDS=60` règle le délai de prise en charge (minimum 5 s).
Le CP fixe `_claim_deadline` lors de l'enqueue et `_job_id` pour les commandes de jobs,
dans le JSON existant. L'agent ignore ces champs. Modifier la configuration ne réécrit
pas le délai d'une commande déjà créée. Pour les commandes legacy sans deadline,
created_at + timeout configuré s'applique ; une date/payload invalide est refusé.

Le waiter et le poll utilisent ce même calcul. Une transition conditionnelle
`queued → failed` sous verrou empêche l'expiration d'une commande déjà claimée par
une autre transaction. Les commandes expirées ne sont pas retournées à un agent.
Le job concerné devient failed/100 %, étapes restantes skipped, message explicite :
« Commande distante non prise en charge par le node dans le délai imparti. »

Le délai total existant reste borné : 900 s pour les jobs usuels, 7200 s pour un deploy
avec archive de référence, 1500 s pour le claim Plex interactif. L'arrêt du CP interrompt
l'attente. Un timeout d'exécution n'est **pas** une annulation distante d'une commande
déjà claimée : elle peut encore terminer sur le node. Le résultat tardif ne transforme
pas la commande terminale ni le job en succès. Vérifier le runtime avant relance ;
l'agent garde son worker métier séquentiel.

Le contrôle d'exécution/liveness avant enqueue et avant claim reste celui du lot 2 :
heartbeat récent, capacité, restrictions de maintenance et upgrade. Pas de nouveau
mécanisme de liveness ; les métriques stale seules ne bloquent pas la suppression.
Les règles existantes autorisent stop/delete en maintenance avec agent joignable.
CRONOS reste interdit aux AppBox.

## Reprise et migration

Aucune migration de schéma SQLite, aucun réenrôlement, aucune modification de agent.json.
Sauvegarder néanmoins la base avant livraison du CP. Au démarrage :

1. Les anciens jobs running sont finalisés en `failed`.
2. Leurs commandes encore queued sont annulées avant distribution. Les anciennes
   commandes sans _job_id sont rattachées par node/client/action aux jobs interrompus.
3. Les commandes queued expirées ou détachées d'un job actif sont finalisées en échec.
4. Les jobs queued avant le hotfix sont conservés et exécutés par les voies par node.

Les suppressions interrompues peuvent être relancées depuis l'UI après vérification
du runtime. Ne pas réécrire les états à la main dans SQLite. Une commande déjà claimed
peut encore s'exécuter : le redémarrage du CP ne tue pas son processus distant.

Le ZIP conserve la liste stricte des membres, protocole 1 et launcher ABI 1 ; seul le
script métier de l'agent change. Utiliser l'upgrade managed manuel existant, sans
bootstrap. La supervision adaptative, le claim Plex, le moteur de capture et la
suppression des Reference Images ne sont pas modifiés.

## Recette ARTEMIS / ORION, à exécuter uniquement après autorisation

Aucun accès serveur ni déploiement n'a été réalisé pendant le développement.
Ne pas utiliser DEMETER. La suppression de fichiers n'a pas de rollback de données :
sauvegarder tout contenu à conserver ou utiliser archive.

1. Livrer le CP avec ce package après sauvegarde DB, redémarrer le CP, puis mettre à
   jour les agents ARTEMIS et ORION via le workflow managed. Vérifier build/SHA installé,
   heartbeat, agent.json inchangé et absence d'effet sur une AppBox témoin.
2. Observer les anciens jobs : IMAGE-JELLY concerne **ORION**. Son job running doit
   devenir failed après restart, sa commande non claimée failed ; les jobs queued
   ARTEMIS doivent avancer. Aucun UPDATE SQLite manuel.
3. Après vérification que AB40AH et AB37AH sont bien les entrées à retirer sur ARTEMIS,
   relancer Supprimer/Purger (confirmation SUPPRIMER si requise). Vérifier les messages
   « déjà absent », puis success des étapes docker_remove/cleanup_files/inventory/audit/
   notification, sortie du catalogue et libération des réservations de ports.
4. Sur une AppBox jetable, tester conteneur/dossier présents, conteneur seul, dossier
   seul et les deux absents. Vérifier après purge : aucun conteneur associé (noms et
   label Compose), aucun dossier, aucune référence active en inventaire. Relever df/du
   avant/après si des données existaient. La baisse réelle de l'espace utilisé doit
   être contrôlée sur le filesystem ; d'autres processus peuvent conserver des fichiers
   ouverts, et ce correctif ne fait aucun Docker prune global.
5. Sur ORION en recette : maintenir le heartbeat mais suspendre volontairement la
   consommation des commandes. Enqueue une suppression de test puis deux jobs ARTEMIS.
   ARTEMIS doit avancer avant le timeout ORION, séquentiellement. ORION doit finir en
   error sous le délai de claim + polling/latence locale, sans faux succès. Réactiver
   le consommateur : la commande expirée ne doit pas être reçue.
6. Arrêter complètement l'agent ORION puis attendre le timeout de liveness : une
   nouvelle opération doit être refusée avant enqueue. Après redémarrage et heartbeat,
   la disponibilité doit revenir selon les règles existantes.
7. Sur recette jetable uniquement : erreurs permissions, filesystem read-only, daemon
   Docker arrêté et symlinks vers un autre client. Vérifier failure, conservation du
   catalogue si les vérifications ne passent pas, absence de suppression hors racine.
8. Tester un restart CP pendant une attente queued et un résultat tardif après timeout.
   Les jobs doivent rester dans des états terminaux explicites, sans succès artificiel.
9. Rejouer deploy/start/stop/restart/recreate/claim Plex et un upgrade managed ; vérifier
   heartbeat pendant commande longue. Ne pas exécuter de test de suppression de Reference
   Image dans ce lot.

## Limites et rollback logiciel

Les tests locaux remplacent Docker et les transports réseau ; ils ne prouvent pas les
permissions Linux, le comportement effectif de Compose, les mounts ni les octets rendus
au filesystem. Vérifier les éventuels montages imbriqués et données partagées avant purge.
Un conteneur legacy sans nom/label/mount permettant d'attester son appartenance est refusé
plutôt que supprimé à tort. Un montage imbriqué nécessite une intervention opérateur
explicite ; le purge ne le démonte pas et ne nettoie pas ses données partagées.
Une configuration dont la racine AppBox passe par un symlink est désormais refusée pour
suppression et doit être corrigée vers un chemin explicite autorisé.

Le timeout de claim peut devoir être augmenté si l'agent traite légitimement une longue
commande extérieure à la file des jobs CP. Ce réglage ne change pas le timeout heartbeat.
Les erreurs DB ou un processus CP arrêté ne garantissent pas une progression UI immédiate ;
la reprise durable au démarrage reste nécessaire.

En cas de régression, suspendre les nouvelles opérations, conserver les journaux et
revenir au CP/package précédent selon la procédure opérateur. Le schéma SQLite n'a pas
changé ; ne pas ressusciter des commandes expirées ni restaurer un ancien état DB comme
s'il correspondait encore au disque. Le rollback logiciel ne restaure aucune donnée
déjà purgée ; seules les sauvegardes de données peuvent le faire.
