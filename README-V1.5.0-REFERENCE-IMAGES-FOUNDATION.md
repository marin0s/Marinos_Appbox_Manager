# Marinos AppBox Manager 1.5.0

## Reference Images Foundation

Cette version installe la fondation générique de la future création automatisée des références Plex puis Jellyfin.

### Fonctionnalités livrées

- Reference Build Engine et pipeline métier `reference_build`.
- Registre extensible des builders applicatifs.
- Builder Plex enregistré en mode non intrusif.
- Déclaration des capacités du builder par les agents.
- Tables persistantes pour les builds, journaux, capacités et rapports.
- Extension du versioning des images avec manifest, archive, tailles, compatibilité et rapports.
- Nouvelle interface « Bibliothèque de références ».
- Création d'un projet de référence depuis un node, sans action distante.
- Séparation du flux principal automatisé et du futur import manuel expert.
- Stockage central configurable, par défaut `/srv/appbox-manager/reference-images`.

### Sécurité de cette phase

La capture distante n'est pas encore activée. La version 1.5.0 ne peut donc pas :

- arrêter Plex ;
- copier ses métadonnées ;
- modifier son fichier de préférences ;
- transférer une archive depuis un node.

Le registre stocke explicitement `intrusive_actions_enabled=0` et le healthcheck expose `reference_build_intrusive_actions=false`.

### Étape suivante

La prochaine phase ajoutera l'analyse automatique non destructive de l'instance Plex source : version, bibliothèques, compteurs Films/Séries, chemins RDAD/NAS et estimation des volumes.
