# Marinos AppBox Manager V0.3.2

## Périmètre

Seul le déploiement devient asynchrone.

- `POST /appboxes/{client_id}/deploy` crée un job puis répond immédiatement.
- Le déploiement Docker continue dans un thread de travail.
- États persistants : `queued`, `running`, `success`, `error`.
- Progression persistante : 0 à 100 %.
- Blocage des doubles déploiements simultanés d'une même AppBox.
- Rafraîchissement de l'historique toutes les 1,5 seconde.
- Bouton Déployer désactivé pendant un job actif.
- Claim Plex, arrêt, recréation et suppression restent synchrones.
- Réseau externe `appbox-shared` conservé dans tous les nouveaux Compose.

## Stockage

`/data/jobs.json`, monté sur l'hôte dans :

`/opt/appbox-manager-poc/data/jobs.json`
