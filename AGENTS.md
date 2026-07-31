# Instructions pour les agents de développement

## Source de vérité

Le dépôt GitHub est la source de vérité. La version de production actuellement déployée est `1.6.0-alpha.4`. La prochaine version est `1.6.0-alpha.5`.

## Architecture et responsabilités

- **CRONOS** : control plane uniquement. Ne pas y déployer d'AppBox.
- **Agents de nœud** : exécutent les commandes Docker, remontent l'inventaire et construisent/restaurent les images de référence.
- **DEMETER** : serveur bare metal client en production. Aucun test destructif ou expérimental.
- **HADES** : hors périmètre de placement AppBox bare metal.

L'intelligence métier reste dans le Control Plane. L'agent doit rester simple, déterministe et limité aux primitives d'exécution distantes.

## Conventions de nommage

Utiliser des noms liés au serveur ou au client, par exemple :

- `appbox-manager-cronos`
- `plex-artemis`
- `portainer-artemis`
- `plex-appb-34ah`

Éviter les anciens noms génériques comme `PMS-1` ou `PlexMediaServer`.

## Sécurité et production

- Ne jamais exposer de secrets dans Git, les logs ou les Pull Requests.
- Ne jamais supprimer une donnée de production sans sauvegarde et rollback explicites.
- Toute opération distante doit être idempotente et produire une erreur exploitable.
- Les états initiaux des conteneurs doivent être restaurés dans un bloc `finally` lorsqu'une opération temporaire les modifie.

## Images de référence

Objectif Plex : déployer une nouvelle instance avec bibliothèques, collections, bases, Metadata et Media déjà présents, sans rescan massif. Les médias réels restent sur RDAD et doivent conserver les mêmes chemins internes.

Inclure : `Metadata`, `Media`, snapshots SQLite cohérents, plugins/scanners/profiles utiles et `Preferences.xml` assaini.

Exclure : caches, logs, crash reports, codecs, diagnostics, PID, transcodes, WAL/SHM non consolidés et données d'identité du serveur source.

## Définition de terminé

Une tâche est terminée uniquement avec :

1. code fonctionnel ;
2. tests associés ;
3. `CHANGELOG.md` mis à jour ;
4. documentation concernée mise à jour ;
5. procédure de vérification et rollback.
