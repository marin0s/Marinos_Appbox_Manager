# AppBox Manager 1.5.2 — Reference Images UX

Cette version uniformise le parcours opérateur des Images de référence sans activer la capture intrusive.

## Parcours principal

- Bibliothèque des références publiées.
- Ajout depuis un serveur existant.
- Analyse Plex en lecture seule.
- État clair : compatible, analyse en cours ou erreur actionnable.
- Import depuis un fichier identifié comme parcours secondaire, volontairement non activé avant la validation automatique des artefacts.

## Navigation Ressources

- Images de référence
- Déploiements
- Agents
- Stockage

L’ancienne entrée Distribution est retirée du menu. La route reste présente pour compatibilité interne.

## Stockage

La page Stockage se concentre désormais sur les Volume Mounts et les groupes de montages. Les données et API existantes ne sont pas supprimées ; seule l’interface opérateur est simplifiée.

## Sécurité

La découverte Plex reste strictement non intrusive. La création réelle d’une archive et l’import de fichiers ne sont pas activés dans cette version.
