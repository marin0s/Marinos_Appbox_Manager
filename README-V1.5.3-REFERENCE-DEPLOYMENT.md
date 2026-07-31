# AppBox Manager 1.5.3 — Reference Deployment

## Objectif

Fermer la boucle entre la bibliothèque des images de référence et la création d’une AppBox.

## Parcours opérateur

1. Ouvrir **AppBox**.
2. Choisir le type Plex ou Jellyfin.
3. Choisir une **Image de déploiement** : image vierge ou image de référence publiée.
4. Choisir le node cible et créer l’AppBox.
5. Lancer le déploiement puis réclamer Plex.

## Sécurité Plex

Avant le premier démarrage d’un clone, AppBox Manager supprime les identifiants propres au serveur source, les jetons Plex, les caches, les logs et les fichiers PID.

## Déploiement distant

Le Control Plane prépare une archive de déploiement mise en cache. L’agent du node cible la télécharge via une route authentifiée, vérifie son SHA-256, l’extrait dans un dossier temporaire puis effectue un remplacement atomique de la configuration.

## Compatibilité

Les anciens profils restent présents en base pour les AppBox existantes et les API historiques. Le parcours principal utilise désormais le catalogue unifié des images de déploiement.
