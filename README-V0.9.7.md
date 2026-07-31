# Marinos AppBox Manager V0.9.7

Correctif du cycle de vie distant des AppBox.

## Corrections

- le Control Plane n’exige plus un `compose.yml` local pour gérer une AppBox distante ;
- l’agent utilise en priorité `/srv/appboxes/<id>/compose.yml` sur le node ;
- repli direct sur `docker start`, `docker stop`, `docker restart` ou `docker rm -f` si le Compose est absent ;
- `deploy` et `recreate` écrivent le Compose transmis par CRONOS avant exécution ;
- `recreate` est refusé proprement si aucun Compose n’est disponible ;
- l’installation d’un agent existant effectue maintenant un vrai `systemctl restart`.

## Validation recommandée

1. Mettre CRONOS à jour.
2. Réinstaller l’agent ARTEMIS depuis Nodes > Installer l’agent.
3. Tester arrêt puis démarrage de `testjelly5` depuis l’interface.
4. Tester une recréation seulement après validation des actions simples.
5. Déployer ensuite une nouvelle AppBox de test sur ORION.
