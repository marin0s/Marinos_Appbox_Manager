# Marinos AppBox Manager V0.4.2

## Correctifs et nouveautés

- Nouveau logo fourni par Marinos.
- Correction du dépassement des cartes dans « Activité récente ».
- Suppression propre d’une AppBox :
  - passage par la file globale ;
  - popup animée et progression live ;
  - `docker compose down --remove-orphans` ;
  - suppression du dossier AppBox ;
  - retrait de l’inventaire.
- Monitoring Node sous forme de graphes sur une heure.
- Graphes CPU, RAM, disque, réseau et I/O.
- Claim Plex affiché comme label d’état.
- VAAPI et RDAD déplacés dans la zone d’informations.
- Icône Plex ou Jellyfin à gauche du nom de chaque AppBox.
- Polling des jobs par identifiant de job pour fiabiliser les popups.

## Installation

```bash
cd /root
unzip marinos-appbox-manager-v0.4.2-artemis.zip
cd appbox-manager-poc-v0.4.2
chmod 755 upgrade-v0.4.2-artemis.sh
./upgrade-v0.4.2-artemis.sh
```

## Test de suppression

Utiliser uniquement une AppBox de test. Ouvrir sa fiche, descendre dans « Zone dangereuse » puis lancer la suppression. La popup doit suivre l’arrêt Docker, le nettoyage des fichiers et le retrait de l’inventaire.
