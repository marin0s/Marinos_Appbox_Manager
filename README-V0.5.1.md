# Marinos AppBox Manager V0.5.1 — Workflow Inspector

Cette version améliore la phase 1 du Workflow Engine sans modifier le provisionnement validé.

## Nouveautés

- timeline graphique avec heures, statuts et durées ;
- journaux repliables indépendants pour chaque étape ;
- statistiques de durée par catégorie : validation, Docker, healthcheck et intégrations ;
- export d’un workflow en JSON ;
- export d’un workflow en texte lisible ;
- exécuteur enregistré pour chaque étape ;
- inventaire des ressources créées préparant le futur rollback ;
- migration SQLite additive et compatible avec la base V0.5.0.

## Installation

```bash
cd /root
unzip marinos-appbox-manager-v0.5.1-artemis.zip
cd appbox-manager-poc-v0.5.1
chmod 755 upgrade-v0.5.1-artemis.sh
./upgrade-v0.5.1-artemis.sh
```

Les patchs V0.5.1 ne sont pas ajoutés au changelog public, qui reste réservé aux versions majeures.
