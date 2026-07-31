# Marinos AppBox Manager 1.4.1 — Transaction Engine

## Objectif

Cette version stabilise le moteur de workflows et corrige le blocage observé lors d'une suppression distante après l'étape `cleanup_files`.

## Correctifs principaux

- Le worker est protégé par une barrière globale : une exception non gérée termine le job en erreur sans arrêter la file.
- La finalisation d'une suppression utilise `BEGIN IMMEDIATE`, `COMMIT` et `ROLLBACK` explicites.
- La suppression en base est idempotente : une AppBox déjà absente représente un état final valide.
- Les références d'inventaire sont nettoyées dans une transaction unique.
- `PRAGMA foreign_key_check` est exécuté avant validation du commit.
- Les workflows restés `running` après un redémarrage sont clôturés proprement en erreur au démarrage.
- Un watchdog clôture les jobs sans activité au-delà du délai configuré.
- Une suppression interrompue génère un audit `FAILED` au lieu de rester `QUEUED` ou `RUNNING`.

## Variables nouvelles

```env
APPBOX_JOB_TIMEOUT_SECONDS=900
APPBOX_JOB_WATCHDOG_INTERVAL=30
```

Le délai par défaut est de 15 minutes. Le watchdog s'exécute toutes les 30 secondes.

## Mise à niveau sur CRONOS

Depuis le dossier extrait :

```bash
chmod +x upgrade-v1.4.1-cronos.sh
./upgrade-v1.4.1-cronos.sh
```

Le script :

1. vérifie l'intégrité de SQLite ;
2. sauvegarde intégralement l'installation ;
3. conserve la base, le `.env`, le compose de production et les fichiers générés ;
4. reconstruit le Control Plane ;
5. vérifie `/health` et la version 1.4.1 ;
6. vérifie les clés étrangères et `quick_check`.

## Validation après déploiement

```bash
curl -fsS http://127.0.0.1:8090/health | jq

docker logs --since 10m appbox-manager-cronos 2>&1 | tail -100

sqlite3 /opt/appbox-manager-poc/data/appbox-manager.db <<'SQL'
PRAGMA quick_check;
PRAGMA foreign_key_check;
SELECT job_id,client_id,action,status,progress,detail
FROM jobs
ORDER BY created_at DESC
LIMIT 10;
SQL
```

## Test fonctionnel recommandé

Effectuer d'abord une suppression sur une AppBox de laboratoire. Ne pas utiliser DEMETER pour ce test : ce node reste un serveur client en production.

Attendus :

- toutes les étapes terminent en `success`, `failed` ou `skipped` ;
- aucun job ne reste figé à 50 % ;
- l'audit termine en `SUCCESS` ou `FAILED` ;
- `PRAGMA foreign_key_check` ne retourne aucune ligne.
