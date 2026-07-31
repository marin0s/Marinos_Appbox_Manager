# Marinos AppBox Manager V0.7.0 — Images de référence

## Nouvelle AppBox simplifiée

Le formulaire principal ne demande plus que :

- identifiant ;
- type Plex/Jellyfin ;
- profil de déploiement.

Les réglages techniques sont regroupés dans **Options avancées** :

- groupe de montages ;
- version d’image de référence ;
- port automatique ou manuel ;
- accélération matérielle ;
- Tautulli ;
- déploiement immédiat.

## Images de référence

Nouvelle rubrique permettant de :

- créer une référence Plex ou Jellyfin ;
- enregistrer plusieurs versions ;
- indiquer la version Plex/Jellyfin ;
- enregistrer le chemin source ;
- mémoriser taille, checksum et nombre d’éléments ;
- publier une version courante ;
- lier une version à un profil de déploiement.

Les déploiements vierges restent disponibles via les profils Plex vierge et Jellyfin vierge.

## Compatibilité

Les anciens `catalog_snapshots` restent conservés comme couche technique interne.
Les nouvelles images de référence s’appuient dessus afin de préserver les AppBox et profils existants.
