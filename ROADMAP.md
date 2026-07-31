# Roadmap

## 1.6.0-alpha.5 — Images de référence Plex

- fiabiliser la capture complète de la médiathèque ;
- arrêter proprement le Plex source si nécessaire ;
- garantir la restauration de son état initial ;
- produire une archive et un manifeste cohérents ;
- valider un déploiement Plex from scratch ;
- corriger le double tiret dans les noms générés ;
- confirmer le workflow de claim distant.

## Après validation Plex

- images de référence Jellyfin avec la même exigence de fiabilité ;
- amélioration progressive de la distribution et du cache des références ;
- reprise de la roadmap multi-nœuds sans élargir le périmètre avant stabilisation.

## Principes

- priorité à la fiabilité avant l'ajout de fonctionnalités ;
- DEMETER reste protégé comme serveur client en production ;
- chaque livraison comporte tests, changelog, documentation et rollback.
