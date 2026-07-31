# AppBox Manager 1.4.2 — Deletion Integrity Hotfix

## Objectif

Corriger l'échec de suppression observé sur `TEST141` après la suppression distante du runtime et des fichiers.

## Cause confirmée

Deux tables historiques conservaient une clé étrangère vers `appboxes.client_id` :

- `placement_decisions.client_id`
- `control_plane_deployments.client_id`

La transaction supprimait l'AppBox sans détacher ces deux références, ce qui déclenchait `sqlite3.IntegrityError: FOREIGN KEY constraint failed`.

## Correctif

`finalize_appbox_deletion()` détache désormais ces deux historiques dans la même transaction que les autres références conservées, avant le `DELETE FROM appboxes`.

Les historiques restent présents avec `client_id = NULL`. Les inventaires actifs sont supprimés. Toute erreur provoque un rollback complet.

## Tests ajoutés

- reproduction complète du cas `TEST141` ;
- conservation et détachement des historiques ;
- suppression des inventaires actifs ;
- idempotence ;
- rollback en présence d'une clé étrangère inconnue ;
- contrôle `PRAGMA foreign_key_check`.

## Déploiement CRONOS

Depuis `/root`, après téléversement de l'archive :

```bash
cd /root
tar -xzf appbox-manager-v1.4.2.tar.gz
cd appbox-manager-v1.4.2
./upgrade-v1.4.2-cronos.sh
```
