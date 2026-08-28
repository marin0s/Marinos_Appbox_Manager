# Liveness des nodes — critères E2E alpha.5 (non exécutés)

Source de vérité : lot 2, commit `ccac757`. Aucun nouveau mécanisme de disponibilité.
Les upgrades ont leur propre état et ne remplacent ni le heartbeat ni la fraîcheur metrics.
Ces vérifications terrain nécessitent une autorisation préalable et un node de test.
Ne pas utiliser DEMETER. CRONOS reste Control Plane uniquement.

Noter la valeur effective de `APPBOX_AGENT_ONLINE_SECONDS` (180 secondes par défaut),
l'heure du dernier heartbeat, son âge et le statut dans `/api/nodes/{node_id}/status` et dans l'UI.
À la borne exacte du timeout le heartbeat reste frais ; il expire au-delà. Ajouter
la cadence de rafraîchissement UI au délai d'observation, sans redémarrer le CP.

| Scénario | Résultat attendu |
| --- | --- |
| Node allumé, heartbeat récent | ONLINE dans API et UI |
| Arrêter complètement le service agent | OFFLINE après expiration du dernier heartbeat, même si nodes.status historique vaut online |
| Redémarrer l'agent | ONLINE au heartbeat suivant |
| Éteindre physiquement le node | OFFLINE après le timeout, sans autre intervention sur le CP |
| Suspendre inventory/metrics mais conserver le heartbeat | ONLINE ; metrics_fresh=false et âge métriques distinct du heartbeat |
| Activer la maintenance, heartbeat présent ou absent | MAINTENANCE prioritaire |
| Placement automatique avec node offline | Node exclu ; CRONOS reste exclu dans tous les cas |
| Observer les transitions dans une page déjà ouverte | Badges et choix de placement se rafraîchissent sans restart du Control Plane |

Compléter avec une commande longue sur un node autorisé : observer plusieurs heartbeats
pendant cette commande, absence de deuxième commande métier concurrente et absence
de rajeunissement artificiel des anciennes métriques. Avec heartbeat frais + métriques
stale, START/STOP/RESTART/CLAIM/RECREATE ne sont pas refusés uniquement pour stale metrics.
Le placement automatique peut refuser une capacité non fiable en indiquant explicitement
« metrics stale » ; le statut du node reste ONLINE. Un placement manuel garde les
contrôles de capacités/maintenance/contraintes techniques et n'invente pas un faux offline.

Conserver les horodatages API et captures UI comme preuves, sans tokens. Rétablir le
service, la collecte et la maintenance initiale après les essais. Ces critères ne
constituent pas une validation réalisée sur ARTEMIS.

## Layout du détail node

Les rangées métriques et Système/Agent partagent désormais une grille parent `.grid` :
gap de 16 px déjà utilisé dans le design, sans marge locale ni CSS spécifique au node.
Les breakpoints existants restent inchangés : quatre métriques sur grand écran, deux
à 1100 px et moins, une à 520 px et moins ; Système/Agent passent sur une colonne à 1100 px.
Vérifier autour de 1100 et 520 px l'absence de chevauchement, de double espacement et
de débordement horizontal. Le layout du Control Plane local reste inchangé.
