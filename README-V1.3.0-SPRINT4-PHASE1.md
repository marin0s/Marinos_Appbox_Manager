# AppBox Manager 1.3.0 — Sprint 4 Phase 1

## Suppression sécurisée

- `archive` : retire les ressources Docker et conserve le dossier AppBox ;
- `delete` : retire Docker et supprime le dossier AppBox ;
- `purge` : ajoute le nettoyage des références techniques associées.

Les médias montés via RDAD ne sont jamais supprimés. La commande est exécutée par l’agent du node cible et le Control Plane ne met à jour l’inventaire qu’après succès de l’agent.

## Validation recommandée

Utiliser une AppBox laboratoire dédiée. Commencer par `archive`, vérifier la conservation du dossier, puis recréer une AppBox de test pour les modes `delete` et `purge`. Ne pas tester sur DEMETER ni sur une AppBox client en production.
