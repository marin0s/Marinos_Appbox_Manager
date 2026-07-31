# Marinos AppBox Manager V0.6.0 — Resource Manager

## Fonctionnalités opérationnelles

- Volume Mounts configurables dans l’interface.
- Groupes de montages réutilisables.
- Validation des montages obligatoires avant création.
- Génération Compose dynamique pour Plex et Jellyfin.
- Port média automatique ou manuel à la création.
- Registre de catalogues Plex/Jellyfin préchargés.
- Profils de provisioning vierges ou liés à un snapshot.
- Copie indépendante d’un snapshot vers la configuration AppBox.
- Traçabilité du profil, du snapshot et des mounts par AppBox.
- Logo Jellyfin redimensionné comme l’icône Plex.

## Limite volontaire de cette première V0.6

La distribution réseau des snapshots vers plusieurs nodes et le cache par node seront activés avec les agents V0.7. En V0.6.0, le chemin source du snapshot doit être accessible depuis ARTEMIS/Control Plane.

## Installation

```bash
cd /root
unzip marinos-appbox-manager-v0.6.0-artemis.zip
cd appbox-manager-poc-v0.6.0
chmod 755 upgrade-v0.6.0-artemis.sh
./upgrade-v0.6.0-artemis.sh
```
