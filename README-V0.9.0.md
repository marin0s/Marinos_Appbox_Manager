# Marinos AppBox Manager V0.9.0 — Node Agent V1

Cette version transforme le registre d’agents de la V0.8 en protocole fonctionnel.

## Fonctions

- création de jetons d’enrôlement par node ;
- jetons stockés uniquement sous forme SHA-256 ;
- heartbeat authentifié ;
- inventaire CPU, RAM, disque, OS, kernel, Docker, Compose, GPU et RDAD ;
- état ONLINE après heartbeat récent ;
- file de commandes sécurisée ;
- commandes autorisées : `ping` et `inventory` ;
- service systemd redémarré automatiquement.

## Sécurité

L’agent V1 ne sait pas encore exécuter un déploiement Docker distant.

La capacité :

```json
"deployment_executor": false
```

reste volontairement désactivée. Un node distant peut être supervisé mais ne devient pas encore une cible réelle de provisioning.

## Installation sur un node

1. Enregistrer le node dans la page Nodes.
2. Lui attribuer `AppBox-Node` ou `Bare-Metal`.
3. Ouvrir Agents.
4. Cliquer sur « Générer un jeton ».
5. Copier la commande générée sur le node.

Le dossier `agent/` contient aussi le script d’installation manuel.
