# AppBox Manager 1.5.1 — Plex Discovery

Cette version ajoute l'analyse distante en lecture seule d'une instance Plex via l'agent distribué.

## Garanties

- aucun arrêt de Plex ;
- aucune écriture dans `/config` ;
- aucune copie ou compression ;
- aucune suppression ;
- lecture de Docker, de l'identité Plex et de la base SQLite en mode `mode=ro`.

## Données détectées

Version et image Plex, conteneur, montage `/config`, bibliothèques, films, séries, saisons, épisodes, tailles des répertoires, montages RDAD/NAS, espace libre, score de compatibilité et politique d'inclusion/exclusion.

## Déploiement

```bash
cd /root
tar -xzf appbox-manager-v1.5.1.tar.gz
cd /root/appbox-manager-v1.5.1
./upgrade-v1.5.1-cronos.sh
```

Après l'upgrade, mettre à jour l'agent du node source depuis l'interface avant de lancer l'analyse Plex.
