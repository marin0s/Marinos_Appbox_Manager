# Marinos AppBox Manager — V1.0.0 Sprint 1

## Inventaire runtime distribué

- endpoint agent dédié `POST /api/agent/v1/{node_id}/inventory` ;
- remontée complète des conteneurs Docker par chaque agent ;
- synchronisation de la table `containers` par node ;
- suppression des entrées obsolètes du node après inventaire complet ;
- état, santé, image, ports, montages, réseaux et labels ;
- identité Plex/Jellyfin remontée par l’agent ;
- pages AppBox distantes alimentées par l’inventaire du node et non par Docker sur CRONOS ;
- synchronisation immédiate après une action AppBox ;
- correction systemd permanente : `/srv/appboxes` est accessible en écriture à l’agent.

La V1 Sprint 1 conserve les workflows distants de la V0.9.7.
