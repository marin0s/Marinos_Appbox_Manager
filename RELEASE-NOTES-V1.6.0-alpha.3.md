# Marinos AppBox Manager 1.6.0-alpha.3 — Plex SQLite hotfix

## Correctifs

- sauvegarde des bases Plex avec `/usr/lib/plexmediaserver/Plex SQLite` depuis le conteneur source ;
- validation `PRAGMA quick_check` avec le moteur SQLite de Plex, compatible avec le tokenizer propriétaire `collating` ;
- repli sûr sur Python SQLite lorsque le binaire Plex est absent ;
- suppression des fichiers temporaires SQLite dans le conteneur après chaque tentative ;
- affichage explicite de `build_failed` dans l’interface ;
- bouton **Relancer la capture** réutilisant le même Reference Build ;
- version Control Plane et agent : `1.6.0-alpha.3`.

La capture ne stoppe pas Plex et ne modifie pas sa base source. Le snapshot temporaire est créé dans `/tmp` du conteneur, copié vers l’overlay de construction, puis supprimé.
