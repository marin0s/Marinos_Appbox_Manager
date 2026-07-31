# Marinos AppBox Manager 1.6.0-alpha.2 — Delivery 2

Correctif de sécurité et de cohérence pour la capture d'une référence Plex active.

## Changements

- sauvegarde SQLite à chaud via l'API `sqlite3_backup` de Python ;
- connexion en lecture seule à la base source ;
- validation `PRAGMA quick_check` de chaque snapshot ;
- suppression des fichiers `-wal` et `-shm` dans la référence ;
- Plex n'est ni arrêté ni redémarré ;
- aucune écriture dans le `/config` source ;
- suppression de la copie de staging complète de la configuration Plex ;
- création directe de l'archive depuis la source avec un overlay assaini ;
- exclusion de Cache, Logs, Crash Reports, Codecs, PID et transcode ;
- remplacement de `Preferences.xml` par une copie sans identité ni jeton Plex ;
- ajout de tests de régression pour une base active en mode WAL et pour le contenu de l'archive.

## Validation

21 tests unitaires passent, dont :

- capture SQLite cohérente pendant qu'une connexion d'écriture reste ouverte ;
- présence des données validées dans le snapshot ;
- absence de WAL/SHM dans la référence ;
- exclusion des données d'exécution ;
- assainissement de l'identité Plex.

## Limites de cette alpha

La première capture réelle reste une opération lourde en lecture disque et compression CPU. Elle ne modifie pas Plex, mais peut temporairement augmenter l'I/O du serveur source.
