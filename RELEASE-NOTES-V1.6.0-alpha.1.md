# AppBox Manager V1.6.0-alpha.2 — Reference Build Orchestration

## Objectif

Rendre fonctionnelle la chaîne Plex suivante sans refonte de la page :

`Discovery → capture assainie → archive → Image → Version → publication → catalogue`

## Changements

- Une Discovery Plex réussie et compatible met automatiquement en file une commande `reference_build`.
- L’agent source copie le `/config` Plex dans un staging temporaire sans modifier l’instance source.
- Les identifiants Plex, jetons, caches, logs, crash reports, codecs et PID sont retirés de la copie.
- L’archive `tar.gz` est envoyée en flux au Control Plane avec authentification et SHA256.
- CRONOS stocke l’archive dans `/srv/appbox-manager/reference-images/builds/<build_id>/`.
- Le Control Plane crée automatiquement `reference_images`, `catalog_snapshots` et `reference_image_versions`, puis publie la version.
- L’image publiée apparaît dans `/api/deployment-images/plex` et dans le formulaire de création AppBox.
- L’archive préconstruite est réutilisée lors d’un déploiement, avec contrôle du checksum.

## Prérequis opérationnel

L’agent du node source doit être mis à jour vers `1.6.0-alpha.2`. Une ancienne Discovery déjà terminée n’est pas reprise automatiquement : créer un nouveau Reference Build après mise à jour de l’agent.

## Sécurité

- Aucune modification du `/config` Plex source.
- Téléversement réservé à l’agent authentifié du node déclaré dans le build.
- Limite d’archive serveur : 500 Gio.
- Vérification SHA256 au téléversement et au téléchargement.
- Refus des liens symboliques à l’extraction sur le node cible.

## Limites de cette alpha

- La capture nécessite assez d’espace temporaire sur le node source pour une copie assainie et une archive compressée.
- Le processus ne fige pas Plex pendant la copie. La validation réelle devra confirmer la cohérence de la base SQLite produite ; un mécanisme de snapshot cohérent pourra être ajouté si nécessaire.
- Plex uniquement.
