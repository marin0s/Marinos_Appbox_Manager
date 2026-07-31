# Processus de livraison

1. Développer sur une branche issue de `develop`.
2. Mettre à jour tests, documentation et changelog.
3. Valider compilation, tests et image Docker.
4. Ouvrir une Pull Request vers `develop`.
5. Tester la version sur CRONOS et sur un nœud non critique.
6. Préparer une branche `release/<version>` si nécessaire.
7. Fusionner vers `main` après validation.
8. Créer le tag et les notes de release.

Toute livraison doit inclure une sauvegarde préalable et une procédure de rollback vérifiée.
