# Marinos AppBox Manager V0.4.3

## Correctif popup

La cause réelle était dans le helper JavaScript :

```javascript
$$('.deploy-step', '#job-steps')
```

Le second paramètre était une chaîne de caractères alors que le helper attend un élément DOM. L’exécution s’arrêtait donc juste après la mise à jour de la barre à 100 %, avant la mise à jour des étapes.

La correction est :

```javascript
$$('.deploy-step', $('#job-steps'))
```

## Nouveautés

- étapes de popup réellement synchronisées ;
- bouton `Fermer` visible dans le bas de la popup une fois le job terminé ;
- croix toujours disponible ;
- changelog des versions majeures accessible depuis le footer ;
- volet Logs sur chaque Node :
  - état des conteneurs et événements Docker ;
  - logs du provisioner AppBox Manager.

## Installation

```bash
cd /root
unzip marinos-appbox-manager-v0.4.3-artemis.zip
cd appbox-manager-poc-v0.4.3
chmod 755 upgrade-v0.4.3-artemis.sh
./upgrade-v0.4.3-artemis.sh
```
