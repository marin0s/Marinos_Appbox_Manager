# Marinos AppBox Manager V0.9.5

## Installation autonome de l'agent

Cette version publie nativement :

- `GET /downloads/appbox-agent-latest.zip`
- `GET /downloads/install-agent.sh`

Dans **Agents**, la génération d'un jeton affiche désormais une commande unique :

```bash
curl -fsSL 'http://CONTROL-PLANE:8090/downloads/install-agent.sh' | sudo bash -s -- 'node-id' 'http://CONTROL-PLANE:8090' 'TOKEN'
```

Le bootstrap télécharge l'archive depuis CRONOS, vérifie son contenu, installe le service systemd et déclenche le premier heartbeat.

Le ZIP reste également téléchargeable directement depuis les pages **Nodes** et **Agents**.
