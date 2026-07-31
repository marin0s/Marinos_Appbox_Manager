# Marinos AppBox Manager V0.4.1 — Command Center UI/UX

## Objectif

Refonte visuelle complète rouge/noir, pensée comme un produit extensible et préparée pour le futur control-plane multinode ARTEMIS + DEMETER.

## Nouveautés

- Command Center comme page d’accueil.
- Navigation latérale modulaire.
- Pages : Nodes, AppBox, Jobs, Notifications et Paramètres.
- Cartes Node et AppBox enrichies.
- Supervision ARTEMIS en temps réel.
- DEMETER affiché comme futur node non enrôlé.
- Fenêtre animée pour les déploiements, arrêts et recréations.
- Barre de progression, étapes et logs dynamiques.
- Opérations sans rechargement de page grâce aux API existantes.
- Centre de notifications fondé sur la table `events`.
- Design responsive desktop/mobile.
- CSS et JavaScript séparés pour faciliter les futures fonctionnalités.

## Installation

```bash
cd /root
unzip marinos-appbox-manager-v0.4.1-artemis.zip
cd appbox-manager-poc-v0.4.1
chmod 755 upgrade-v0.4.1-artemis.sh
./upgrade-v0.4.1-artemis.sh
```

## Tests

- Ouvrir le Command Center.
- Vérifier les pages `/nodes`, `/appboxes`, `/jobs`, `/notifications`.
- Lancer un arrêt ou un démarrage d’une AppBox.
- Vérifier l’ouverture automatique de la popup de progression.
- Vérifier que la barre et les logs évoluent jusqu’au résultat final.

## Suite

V0.4.2 : moteur de workflows détaillé et étapes persistantes.
