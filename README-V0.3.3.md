# Marinos AppBox Manager V0.3.3

## Changements

- Page du parc : statut toutes les 8 secondes.
- Aucun polling des jobs sur la page du parc.
- Page d'une AppBox : statut toutes les 3 secondes.
- Jobs toutes les 1,5 seconde uniquement pendant une opération active.
- Jobs toutes les 10 secondes au repos.
- Libellé de job :
  - premier lancement : `Déploiement de l’AppBox`
  - conteneur existant et arrêté : `Démarrage de l’AppBox`
- Boutons adaptés aux états `absent`, `exited` et `running`.
- Claim Plex toujours synchrone.
