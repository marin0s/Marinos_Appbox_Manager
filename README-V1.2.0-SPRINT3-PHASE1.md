# Marinos AppBox Manager 1.2.0 — Sprint 3 Phase 1

## Remote Deployment Engine

Cette version ajoute le déploiement distant sécurisé des AppBox via les agents.

### Ajouts

- manifeste de déploiement versionné (`schema_version: 1`) ;
- checksum SHA-256 du manifeste, de `compose.yml` et de `.env` ;
- écriture atomique des fichiers sur le node ;
- confinement strict dans `/srv/appboxes/<client_id>` ;
- validation stricte des identifiants AppBox ;
- création automatique des répertoires requis par l’installateur ;
- retour structuré du résultat d’exécution au Control Plane.

### Compatibilité

La version conserve les fonctions Sprint 2 : heartbeat, inventaire, réconciliation, dérive, orphelins et exécution distante existante.

### Installation agent

L’installateur crée automatiquement :

```text
/etc/marinos-appbox-agent
/var/lib/marinos-appbox-agent
/srv/appboxes
```

### Limites de cette phase

Les boutons Docker, les modales UI et le cycle de vie complet seront traités dans les phases suivantes du Sprint 3.
