# Agents : upgrades sûrs — 1.6.0-alpha.5

## Périmètre et prérequis

Déclenchement manuel uniquement. Aucun upgrade global automatique, aucune commande
shell reçue du navigateur, aucune URL libre. Le bouton « tous les agents » est désactivé.
Le Control Plane doit être mis à jour avant les agents. Ne rien tester sur DEMETER.
Les procédures ci-dessous sont à exécuter uniquement après autorisation explicite ;
elles n'ont pas été exécutées sur infrastructure pendant le développement.

Le CP crée automatiquement deux tables SQLite additives, `agent_upgrades` et
`agent_upgrade_runtime`. Sauvegarder sa base avant déploiement. Aucun changement des
tables de tokens, aucun réenrôlement. Conserver le volume DATA_DIR et son sous-répertoire
`agent-upgrades/artifacts`, qui contient les ZIP immuables `<sha256>.zip`.

## Contrat du package

`python scripts/package_agent.py` génère `agent/appbox-agent-latest.zip` ; `--check`
vérifie sa reproductibilité. Ordre, LF, permissions, dates ZIP et manifeste sont fixes.
`agent-manifest.json` contient protocole, version, SHA-256 de chaque fichier et build_id.
Le SHA-256 de l'archive complète l'identifie indépendamment de sa version affichée.
Deux builds différents portant la même version `-dev` sont distingués. Les versions
numériques et alpha/beta/rc/dev sont ordonnées explicitement ; un downgrade n'est pas proposé.

Le CP compare l'archive aux sources distribuées avant de l'offrir. Il en fige une copie
par checksum au démarrage de l'opération. L'agent et le helper vérifient chacun le SHA,
la liste exacte des fichiers, leur type régulier, les doublons, le manifeste, les hashes
et la syntaxe Python. Taille archive et contenu décompressé limités à 8 Mio. Ni traversal,
ni liens, ni fichiers spéciaux, ni script supplémentaire. Aucun `extractall`, aucune
exécution de `install-agent.sh` ou d'un hook fourni par le ZIP lors de l'upgrade.
Seul le point d'entrée agent fixe est démarré par systemd après validation.

La logique d'upgrade est désormais versionnée : `upgrade_helper.py`, `upgrade_client.py`
et `upgrade_contract.py` évoluent avec chaque release. La comparaison octet pour octet
avec trois modules installés hors releases a été supprimée. Seuls le petit
`upgrade_launcher.py` et son service/timer restent fixes après bootstrap.

### Dispatch et récupération indépendants

Le lanceur ne connaît ni ZIP, ni CP, ni versions, ni unités agent. Il verrouille le
superviseur, exécute `controller/upgrade_helper.py tick` avec un timeout, et, en cas
d'échec, appelle `rescue/upgrade_helper.py recover`. Les chemins doivent être des releases
locales ; aucune URL ni commande arbitraire. stdout/stderr des enfants sont masqués.
Sous Linux, tout le groupe de processus défaillant est tué avant la récupération.

Pour N → N+1, le contrôleur N reste actif pendant toute la transaction, même après la
bascule de current vers N+1. Il sauvegarde l'unité précédente et un état de récupération
avant tout arrêt. Le nouvel agent doit confirmer son identité complète ; le contrôleur N
exécute aussi le point d'entrée fixe `probe` du helper N+1 en processus isolé (sans privilèges
root sous Linux, sans accès requis à agent.json). Une erreur d'import, une exception ou
un timeout de ce helper déclenche le rollback par N, jamais par la candidate cassée.
Après confirmation et acquittement CP, controller pointe sur N+1. N+1 prépare et supervise
alors N+2 avec ses propres modules Python ; aucune intervention SSH entre releases normales.

`recovery.json` contient une enveloppe ABI 1 et le snapshot de récupération appartenant
au contrôleur sortant. Elle reste lisible par le contrôleur de secours même si state.json
est endommagé. Les releases suivantes doivent préserver cette enveloppe et les points
d'entrée `tick`, `recover`, `probe` ; leur logique interne peut changer. Le manifeste
annonce `launcher_abi=1`. Une rupture de cet ABI ou de l'enveloppe de privilèges du lanceur
est un chantier exceptionnel de migration, pas une obligation pour modifier le helper,
le client, le contrat ZIP ou la politique d'upgrade.

Après un succès déjà confirmé, un défaut tardif du dispatcher n'arrête pas les jobs métier :
il revient au contrôleur de secours pour les opérations futures. Il ne prétend pas annuler
une mise à jour déjà confirmée. Cela évite un rollback tardif destructeur pendant un provisioning.

### Unités systemd

- `managed-agent.service` appartient à chaque release. Il remplace atomiquement
  `/etc/systemd/system/marinos-appbox-agent.service` pendant l'activation, avec
  `systemctl daemon-reload` puis démarrage. La sauvegarde exacte de l'ancienne unité est
  journalisée ; tout échec d'activation/confirmation restaure ces octets, refait daemon-reload
  et revient à previous. Un reload est répété après reprise même si les octets correspondent
  déjà, pour couvrir une coupure entre remplacement et reload.
- Les directives admises sont validées par le contrat versionné : cadence de restart,
  timeouts, limites CPU/mémoire/tâches, description et dépendances peuvent évoluer.
  L'entrée Python/current, le type simple, le redémarrage automatique et les protections
  essentielles restent obligatoires. Pas d'ExecStartPre/ExecStopPost, de shell ou
  d'EnvironmentFile fourni par le ZIP. Une extension du contrat peut être livrée dans
  une release intermédiaire compatible avant d'utiliser de nouvelles directives.
- `marinos-appbox-updater.service` et `.timer` restent fixes : ils invoquent uniquement le
  lanceur, conservent ses répertoires writable et bornent les enfants à 150 s chacun (330 s
  pour le oneshot, secours compris). Le timer continue même si l'agent ou le helper candidat
  est cassé. La candidate ne peut donc pas désactiver la voie de secours en remplaçant ces
  unités. Les copies présentes dans le ZIP servent au bootstrap, pas aux upgrades suivants.
- Les drop-ins locaux sont conservés. Un ExecStart personnalisé doit être résolu pendant
  le bootstrap initial, puisqu'il masquerait l'unité versionnée. Les overrides ajoutés
  ultérieurement par un opérateur restent de sa responsabilité et sont à auditer en E2E.

## Chemins et supervision

| Chemin | Rôle |
| --- | --- |
| `/etc/marinos-appbox-agent/agent.json` | Configuration, token, identité existants : lecture seule pour ce mécanisme |
| `/usr/local/sbin/marinos-appbox-agent.py` | Installation legacy conservée |
| `/opt/marinos-appbox-agent/releases/<sha>/` | Releases immuables validées, receipt et manifeste |
| `/opt/marinos-appbox-agent/releases/legacy-<hash>/` | Copie de sauvegarde legacy et modules présents |
| `/opt/marinos-appbox-agent/current` | Symlink remplacé par `os.replace` sur le même filesystem |
| `/opt/marinos-appbox-agent/previous` | Release précédente, conservée pour rollback |
| `/opt/marinos-appbox-agent/controller` | Release dont le helper supervise la transaction |
| `/opt/marinos-appbox-agent/rescue` | Dernier contrôleur utilisable pour récupération |
| `/opt/marinos-appbox-agent/upgrade_launcher.py` | Seul fichier Python fixe ; aucun module d’upgrade importé |
| `/var/lib/marinos-appbox-updater/recovery.json` | Enveloppe de secours indépendante du journal courant |
| `/var/lib/marinos-appbox-agent/upgrades/` | Demande durable, téléchargement et candidate préparée |
| `/var/lib/marinos-appbox-updater/state.json` | Previous/candidate, délais, phase, confirmation et notifications non acquittées |
| `/var/lib/marinos-appbox-updater/bootstrap.json` | Reprise du bootstrap avec le même artefact et node |
| `/etc/systemd/system/marinos-appbox-agent.service` | Copie atomique de managed-agent.service de la release active |
| `/var/lib/marinos-appbox-updater/legacy-agent.service` | Unité historique sauvegardée au bootstrap |

Le timer `marinos-appbox-updater.timer` démarre 15 s après boot puis 5 s après chaque
exécution du service oneshot. Le lanceur détient flock et le contrôleur est root, sans PartOf/Requires
sur l'agent. Le service agent reste root avec ses restrictions de filesystem existantes.
Seul le helper peut écrire dans `/opt` depuis son service ; le service agent prépare
dans son répertoire writable habituel. Le helper ne stoppe aucun conteneur Docker.

## Phases et délais

`queued → downloading → verifying → prepared → installing → restarting → awaiting_heartbeat → success`

Échec avant activation : `upgrade_failed`. Échec après activation :
`rolling_back → rolled_back`, ou `rollback_failed` si le retour de l'ancien agent
ne peut pas être confirmé. `rolled_back` est présenté comme `upgrade_failed` dans
le statut synthétique, avec la phase détaillée conservée.

Préparation : délai global CP de 900 s. Téléchargement : 120 s, avec timeout socket
de 30 s (un read en cours peut dépasser le délai global d'au plus ce timeout).
Confirmation : 300 s maximum après prise en charge, bornée par le délai CP.
Rollback : nouveau délai de 300 s. Un appel systemctl est borné à 45 s ; le oneshot
à 330 s. Les délais sont persistés en temps UTC, non réinitialisés à chaque reboot.
Synchroniser les horloges CP/node. La granularité du timer et les appels en cours
s'ajoutent au moment où une expiration est observée.

Avant `prepared`, une opération expirée est marquée en échec sans activation possible.
À partir de `prepared`, le helper décide du résultat ; un CP qui n'a plus de réponse
affiche `upgrade_failed / supervisor_confirmation_overdue` après 900 s mais garde le
verrou d'exécution jusqu'au résultat terminal. Cela évite de lancer une AppBox pendant
une activation dont le résultat est encore inconnu. Ne pas effacer la ligne pour forcer
la reprise. Inspecter/reprendre le helper.

La confirmation exige systemd actif, MainPID positif, heartbeat reçu après le précédent,
version/build/checksum attendus, nouveau nonce et PID du heartbeat correspondant à
MainPID. Le rollback exige le retour de previous ; pour un ancien agent legacy sans
nonce/PID, la preuve disponible est service actif + heartbeat ultérieur de sa version.
Les phases locales sont réémises dans l'ordre après une panne réseau ; le rollback
local n'attend pas le retour du CP pour restaurer previous.

Les commandes/jobs queued ou running/claimed bloquent un upgrade. Une fois réservé,
le node refuse les nouvelles commandes/jobs métier, le placement et le provisioning.
Les trois boucles de l'agent restent séparées. Le statut ONLINE/OFFLINE et les métriques
restent ceux du lot 2 ; `restart_expected` indique une courte interruption attendue
sans transformer un agent réellement offline en online.

## API et UI

- `POST /nodes/{node_id}/upgrade-agent` : déclenchement manuel, aucun package/URL transmis par le navigateur.
- `GET /api/nodes/{node_id}/agent-upgrade` : `status` d'upgrade, `node_status` de liveness,
  âge heartbeat, versions, build installé, SHA/taille disponibles, phase, erreur, besoin de bootstrap.
- `POST /api/agent/v1/{node_id}/upgrades/bootstrap` : réservation initiale avec le SHA officiel obligatoire.
- `GET /api/agent/v1/{node_id}/upgrades/{operation_id}` : opération et dernier runtime annoncé.
- `GET /api/agent/v1/{node_id}/upgrades/{operation_id}/archive` : copie immuable authentifiée.
- `POST /api/agent/v1/{node_id}/upgrades/{operation_id}/events` : transitions contrôlées et preuve de confirmation.

Les endpoints agent réutilisent le bearer existant et vérifient le node propriétaire.
Les pages Agents/Nodes rafraîchissent l'avancement toutes les 5 s et affichent les refus
API. Les statuts synthétiques sont up_to_date/update_available/upgrading/upgrade_failed/unknown.

## Bootstrap initial sur ARTEMIS

L'agent legacy ne connaît pas `agent_upgrade` et son service a `/opt` en lecture seule.
Il ne peut donc pas installer lui-même ce système sans lui ajouter une exécution root
arbitraire. Une opération administrateur unique est nécessaire, pas une réinstallation
ni un nouvel enrôlement. Les upgrades ultérieurs passent par le bouton du CP.

1. Déployer le CP avec son package reconstruit. Sur ARTEMIS, obtenir une copie **de
   confiance** du checkout contenant le helper et du ZIP officiel, par le canal opérateur
   habituel. Ne jamais extraire/exécuter un helper reçu d'une URL arbitraire.
2. Confirmer le node ARTEMIS online, hors maintenance, sans job/commande incompatible.
   Depuis l'API de statut, relever le SHA-256 officiel (64 caractères hexadécimaux).
3. En root sur ARTEMIS, en remplaçant seulement les chemins opérateur et le SHA :

```bash
# SOURCE est un checkout vérifié de cette livraison, déjà transféré sur ARTEMIS.
SOURCE=/root/marinos-appbox-manager-reviewed
ARCHIVE=/root/appbox-agent-latest.zip
SHA=REMPLACER_PAR_LE_SHA256_OFFICIEL
test -r /etc/marinos-appbox-agent/agent.json
systemctl is-active marinos-appbox-agent.service
printf '%s  %s\n' "$SHA" "$ARCHIVE" | sha256sum --check -
python3 "$SOURCE/agent/upgrade_helper.py" bootstrap --archive "$ARCHIVE" --sha256 "$SHA"
systemctl is-enabled marinos-appbox-updater.timer
systemctl status marinos-appbox-updater.timer --no-pager
```

Le helper lit le token existant sans l'afficher, réserve l'opération auprès du CP,
copie les fichiers legacy, installe le lanceur/timer, prépare les contrôleurs dans releases et adapte l’unité, puis laisse le
superviseur activer/valider la première release. Il ne réécrit jamais agent.json.
Avant le bootstrap, sauvegarder ce fichier et les unités/drop-ins existants dans un
emplacement root protégé ; ne pas copier leur contenu dans les logs ou tickets.

Après succès : vérifier `readlink -f /opt/marinos-appbox-agent/current`, la phase success,
la version/build et `remote_upgrade=true` dans le heartbeat. Comparer localement le hash
de agent.json avant/après. Vérifier que les AppBox sont restées intactes.
Si le bootstrap est interrompu avant handoff, relancer exactement la même commande :
bootstrap.json permet de reprendre avec le même node et artefact. Si une activation existe déjà, inspecter le journal plutôt que recommencer le bootstrap.
Après un échec initial ou un rollback confirmé vers legacy, la même commande peut
réserver une nouvelle tentative (éventuellement avec un nouvel artefact officiel),
à condition que le helper ait acquitté son résultat terminal. Cela ne nécessite pas
que l'ancien agent comprenne `agent_upgrade`. Un agent déjà managed utilise le bouton CP.

## Rollback et récupération

Rollback automatique : journaliser rolling_back, arrêter uniquement le service agent,
basculer current vers previous, démarrer le service, attendre le heartbeat de previous.
Le helper conserve toutes les releases ; aucun nettoyage automatique dans alpha.5.
Après rolled_back acquitté, le timer nettoie la demande précédente au tick suivant ;
attendre quelques secondes puis une nouvelle tentative manuelle peut être lancée.

Pour un diagnostic autorisé : lire state.json (aucun token), vérifier timer/service,
connectivité et horloge, puis attendre/reprendre `python3
/opt/marinos-appbox-agent/upgrade_launcher.py`. Ne pas supprimer le journal :
il contient les délais et la cible de rollback. Une panne réseau peut laisser l'ancien
agent restauré mais non confirmé : `rollback_failed` ne signifie pas forcément que son
processus est arrêté. Vérifier systemd et le heartbeat avant toute nouvelle tentative.

En dernier recours opérateur, arrêter le timer **et** son oneshot, puis le service agent,
et restaurer current vers le chemin previous vérifié, via symlink temporaire + rename
sur le même filesystem. Restaurer aussi atomiquement l'unité précédente sauvegardée en
base64 dans `state.json.previous_unit`, puis exécuter `systemctl daemon-reload` avant de
redémarrer seulement l'agent. Conserver le journal pour résoudre
le résultat CP ; ne jamais écrire un succès artificiel dans la base. Un retour complet
au legacy restaure atomiquement l’unité sauvegardée dans `legacy-agent.service`, puis
effectue daemon-reload et restart agent, après vérification des fichiers legacy. Ne pas supprimer les autres drop-ins,
les releases ni la configuration. Le rollback du CP vers une version antérieure requiert
également l'ancien agent ou une validation explicite de compatibilité du protocole.

## Validation N → N+1 sur infrastructure, ultérieure et autorisée

1. Bootstrap et confirmer N sur ARTEMIS ; sauvegarder config, versions, hashes et état
   des AppBox. Aucun test sur DEMETER. Vérifier que le helper/timer est sain.
2. Préparer dans un environnement de recette une livraison N+1 ne changeant que la
   version agent, le helper et les modules client/contrat. Faire évoluer aussi RestartSec
   dans managed-agent.service. Reconstruire
   le package et `--check`, déployer ce CP/package de recette.
3. Vérifier update_available, version et SHA, puis cliquer une seule fois sur Mettre à
   jour l'agent. Observer chaque phase, nouveau current, nouveau MainPID/nonce, heartbeat
   et success ; vérifier agent.json inchangé et release N toujours présente. Publier ensuite
   N+2 et vérifier que le helper de N+1 prend en charge cette seconde opération, sans SSH.
4. Tester un refus pendant une capture/commande longue autorisée, avec heartbeats continus.
   Vérifier aussi que de nouvelles actions ne partent pas pendant upgrading.
5. Sur un node de recette sacrifiable seulement : simuler une candidate qui ne confirme
   pas ou annonce la mauvaise version. Observer rollback sous les délais, retour du
   heartbeat N et rolled_back ; republier un package valide et retenter manuellement.
6. Tester reboot aux phases préparation/restarting/awaiting_heartbeat/rolling_back et
   coupure CP. Vérifier les notifications rejouées, le retour de previous et la liveness
   réelle. Ne pas confondre une confirmation impossible avec un rollback confirmé.

Les tests Python locaux couvrent le contrat, le CP, l'agent préparateur, les états du
helper, les refus et la reprise. Sous Windows, systemd et les symlinks sont simulés
(création de symlink non autorisée) ; ces tests ne prouvent pas l'intégration Linux.

## Limites de sécurité séparées

- TLS : utiliser HTTPS et la validation CA normale. HTTP reste accepté pour compatibilité
  réseau existante ; un SHA reçu sur le même canal non protégé n'est pas une signature.
- Authentification opérateur : réutilisation du modèle existant du CP ; pas de nouvel
  écran de connexion/RBAC. Restreindre impérativement l'accès opérateur par réseau/proxy.
- CSRF : pas de refonte ni de protection CSRF globale ajoutée ; endpoint manuel sensible
  à protéger dans le chantier sécurité dédié.
- Canal d'upgrade : bearer du node réutilisé, redirections refusées, artefact figé et
  validé. Pas encore de signature hors bande, mTLS, clés dédiées par helper, attestation
  matérielle ou défense contre un CP/root compromis. Un token de node compromis reste
  une limite du modèle de confiance existant.

Ces limites ne dispensent jamais du SHA, de la validation du ZIP, de l'autorisation du
node ni de la confirmation/rollback. Elles interdisent de présenter alpha.5 comme un
canal de distribution logiciel durci pour une exposition publique.
