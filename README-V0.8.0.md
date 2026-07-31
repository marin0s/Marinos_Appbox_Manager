# Marinos AppBox Manager V0.8.0 — Control Plane Foundation

## Placement des AppBox

Trois principes sont désormais inscrits dans le modèle métier :

- placement manuel disponible ;
- placement automatique facultatif ;
- exclusion stricte des nodes `Bare-Metal` en automatique.

ARTEMIS reçoit par défaut les tags :

- `AppBox-Node`
- `Control-Plane`
- `Media`

Le mode global par défaut reste `manual`.

## Registre de nodes et tags

La page Nodes permet d’enregistrer les futurs serveurs et de leur attribuer :

- AppBox-Node ;
- Bare-Metal ;
- Control-Plane ;
- Media ;
- Maintenance ;
- Test.

Un node distant sans agent peut être inventorié, mais ne peut pas encore recevoir un déploiement réel.

## Fondations distribuées

Nouvelles pages :

- Distribution ;
- Déploiements ;
- Agents.

Nouvelles tables :

- node_tags ;
- node_tag_assignments ;
- placement_settings ;
- placement_decisions ;
- node_agents ;
- reference_image_distribution ;
- control_plane_deployments.

## Limite volontaire

La V0.8.0 enregistre et évalue les nodes distants, mais seul ARTEMIS possède l’agent embarqué capable d’exécuter Docker. Le déploiement distant et le transfert des images seront activés avec le premier agent externe.
