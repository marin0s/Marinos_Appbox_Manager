# Images de référence

Une image de référence est une capture assainie d'une instance multimédia servant à créer de nouvelles AppBox déjà préparées.

## Plex

La référence doit préserver :

- bibliothèques, collections et catalogue ;
- `Metadata/` et `Media/` ;
- bases SQLite dans un état cohérent ;
- plugins, scanners et profils utiles ;
- préférences non liées à l'identité.

Elle doit retirer les identifiants et tokens du serveur source ainsi que les caches, logs, diagnostics, transcodes et fichiers temporaires.

Les chemins de médias vus dans le conteneur doivent rester identiques entre la source et la cible. Les fichiers vidéo ne sont pas intégrés à l'archive : ils restent servis par RDAD.

Pendant une capture complète, l'arrêt temporaire de Plex est autorisé lorsque nécessaire à la cohérence. Le builder doit enregistrer l'état initial et redémarrer la source dans tous les chemins de sortie.
