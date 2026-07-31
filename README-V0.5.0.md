# Marinos AppBox Manager V0.5.0 — Phase 1 Workflow Engine

## Évolution majeure

Les opérations ne reposent plus sur un pourcentage artificiel. Chaque déploiement, arrêt, recréation ou suppression crée maintenant une liste d’étapes persistantes dans `job_steps`.

Chaque étape conserve :

- statut réel ;
- date et heure de début ;
- date et heure de fin ;
- durée ;
- progression ;
- logs propres ;
- résultat success, failed, warning ou skipped.

## Workflows

### Déploiement
- Validation du node
- Validation RDAD et GPU
- Validation du Compose
- Création et démarrage Docker
- Healthcheck
- Refresh ciblé (préparé / skipped pour cette phase)
- Watchdog (préparé / skipped pour cette phase)
- Notification interne

### Suppression
- Validation AppBox
- Suppression Docker
- Nettoyage fichiers
- Retrait inventaire
- Notification

## UI

- La popup lit directement `job.steps` depuis l’API.
- Nouvelle console `/jobs/<job_id>`.
- Logs et durées visibles par étape.
- En cas d’échec, les étapes restantes passent en `skipped`.

## Installation

```bash
cd /root
unzip marinos-appbox-manager-v0.5.0-artemis.zip
cd appbox-manager-poc-v0.5.0
chmod 755 upgrade-v0.5.0-artemis.sh
./upgrade-v0.5.0-artemis.sh
```
