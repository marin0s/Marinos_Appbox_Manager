# Critères d'acceptation 1.6.0-alpha.5

La version est validée lorsque :

Les tests automatisés ne remplacent pas ces critères terrain. Utiliser le [runbook E2E et rollback](reference-images-alpha5-e2e.md) ; aucun nœud n’est autorisé implicitement par ce document.

- une référence Plex peut être construite depuis OURANOS ;
- l'état initial du conteneur source est restauré même après une erreur ;
- l'archive contient les bibliothèques, Metadata, Media et des bases cohérentes ;
- les identifiants et tokens de la source sont absents ;
- une nouvelle AppBox peut être déployée from scratch sur ARTEMIS ;
- les bibliothèques et le contenu sont disponibles sans reconstruction complète ;
- le claim est exécuté par l'agent du nœud et fonctionne depuis l'interface ;
- les noms de conteneurs ne comportent pas de double tiret involontaire ;
- les tests automatisés, le changelog et la procédure de rollback sont présents.
