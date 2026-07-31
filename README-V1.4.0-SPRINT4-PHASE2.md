# AppBox Manager 1.4.0 — Sprint 4 Phase 2

## Suppression définitive transactionnelle

Cette version complète le mode **Supprimer l’AppBox** :

- arrêt et suppression Docker sur le node cible ;
- suppression du dossier persistant `/srv/appboxes/<client_id>` ;
- vérification distante de l’absence du dossier et des conteneurs ;
- commit BDD uniquement après validation complète ;
- conservation du journal d’audit et du job de résultat ;
- maintien de l’AppBox en base avec état d’erreur en cas d’échec partiel ;
- médias RDAD exclus du périmètre de suppression ;
- correctif natif du statut `ARCHIVÉE` ;
- archive agent corrigée avec service systemd inclus.

## Installation

```bash
cd /root
tar -xzf appbox-manager-v1.4.0-sprint4-phase2.tar.gz
cd appbox-manager-poc-v1.4.0
./upgrade-v1.4.0-cronos.sh
```

Réinstaller ensuite l’agent du node laboratoire depuis le Control Plane et vérifier qu’il annonce `1.4.0`.

## Test recommandé

Utiliser exclusivement une AppBox laboratoire neuve, par exemple `ab38ah`, et choisir le mode **Supprimer l’AppBox**. Ne pas tester sur DEMETER ni sur une AppBox client en production.
