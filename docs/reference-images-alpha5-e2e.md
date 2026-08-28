# Validation terrain alpha.5 — non exécutée

## Autorisation préalable

Cette procédure est préparée, pas exécutée. Aucun accès distant n’a été utilisé pendant la correction.
OURANOS (source) et ARTEMIS (destination) sont les candidats des critères historiques, **pas une autorisation de les utiliser**. Faire confirmer par l’opérateur le caractère non critique, les conteneurs exacts, les chemins, les ports libres et la fenêtre d’arrêt. DEMETER est interdit ; HADES est hors périmètre ; CRONOS n’accueille aucune AppBox. Si l’un de ces points manque, s’arrêter ici.

Ne pas exécuter les scripts historiques d’upgrade alpha.4 pour installer alpha.5. Le package et le Control Plane doivent provenir du même commit de release.

## Préparation locale et sauvegardes

Sur une machine Linux de validation avec Docker (pas sur un nœud choisi arbitrairement) :

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt pytest httpx
python scripts/package_agent.py --check
python -m pytest -q
python -m compileall -q app agent scripts
docker compose build appbox-manager
```

Avant remplacement du Control Plane, sur CRONOS **après autorisation de maintenance**, depuis le checkout réellement déployé :

```bash
umask 077
docker inspect --format '{{.Image}}' appbox-manager-cronos
docker image tag "$(docker inspect --format '{{.Image}}' appbox-manager-cronos)" appbox-manager:pre-alpha5-validation
docker compose exec -T appbox-manager python -c 'import sqlite3; a=sqlite3.connect("/data/appbox-manager.db"); b=sqlite3.connect("/data/pre-alpha5-validation.sqlite"); a.backup(b); b.close(); a.close()'
```

Copier la sauvegarde SQLite, le Compose, la configuration et les fichiers agent actuels dans un emplacement de sauvegarde privé validé par l’opérateur, hors du checkout et hors Git. Vérifier la sauvegarde SQLite avec `PRAGMA quick_check` et conserver les versions/images Docker exactes. Une sauvegarde peut contenir des secrets : permissions restrictives, aucune pièce jointe publique.

Sur chaque nœud autorisé, sauvegarder `/usr/local/sbin/marinos-appbox-agent.py`, l’éventuel `reference_contract.py`, le service systemd et la configuration de l’agent. Vérifier l’espace libre : archive compressée + cache + configuration extraite + copies temporaires des DB. Ne pas lancer de capture sans réserve suffisante et sauvegarde du `/config` source vérifiée.

## Déployer le candidat

Après sauvegardes et autorisation, mettre à jour CRONOS avec le checkout de release vérifié :

```bash
docker compose up -d --build appbox-manager
curl -fsS http://127.0.0.1:8090/health
```

Exiger version `1.6.0-alpha.5-dev` et `reference_build_intrusive_actions=false` : une source Plex running reste active sans stop/restart pendant la capture.
Depuis Agents, télécharger le ZIP de ce Control Plane. Sur les seuls nœuds approuvés, décompresser dans un répertoire privé et installer ensemble les deux fichiers Python :

```bash
sudo install -m 755 marinos-appbox-agent.py /usr/local/sbin/marinos-appbox-agent.py
sudo install -m 644 reference_contract.py /usr/local/sbin/reference_contract.py
sudo systemctl restart marinos-appbox-agent.service
sudo systemctl is-active marinos-appbox-agent.service
```

Ne pas repasser de jeton d’enrôlement en argument de commande. Conserver l’enrôlement existant. Attendre heartbeat et inventaire dans l’interface, puis vérifier versions, disponibilité RDAD, GPU si requis et capacités de capture/déploiement. Les réglages systemd doivent autoriser les chemins temporaires/cache utilisés ; ne pas désactiver globalement le sandbox du service.

## Scénario et preuves à conserver

1. Relever sur la **source approuvée** le nom exact du conteneur, l’état Docker, la version Plex, le nombre de bibliothèques/collections et les chemins internes des médias. Relever séparément son identité pour comparaison privée, sans la publier dans les logs.
2. Interface **Images de référence → Depuis un serveur** : sélectionner explicitement le nœud et le conteneur approuvés, analyser puis construire. L’analyse enchaîne actuellement la capture si les préconditions sont satisfaites : la fenêtre d’arrêt doit donc être autorisée avant cette action.
3. Contrôler le redémarrage source, `/identity`, les journaux de build, la validation SQLite, Metadata/Media, tailles et checksum. Vérifier le contenu du tar avec `tar -tzf CHEMIN_ARCHIVE_APPROUVE` et `sha256sum CHEMIN_ARCHIVE_APPROUVE` ; ne jamais extraire dans le `/config` source. La version est publiée automatiquement après validation technique.
4. Si `schema-readable-tokenizer-unavailable` apparaît, valider les DB avec le moteur Plex du même build sur une copie de test. Ne pas assimiler ce fallback à un quick_check natif réussi.
5. Créer une **nouvelle** AppBox de test avec un ID explicitement réservé, par exemple `ab-e2e16`, et la référence publiée. Choisir manuellement la destination approuvée et les mêmes chemins RDAD internes. Vérifier le port libre ; ne jamais réutiliser le répertoire ou les ports d’une AppBox existante.
6. Lancer le déploiement. La distribution se fait à la demande : téléchargement/cache → staging → SQLite/préférences → Compose → rename → démarrage → health. Vérifier job, état `node_reference_cache`, absence de `.partial`, manifeste et checksum. Le nom Plex attendu pour `ab-e2e16` est `plex-appb-e2e16`, stack et répertoire gardent `ab-e2e16`.
7. Vérifier que Plex est accessible, a une nouvelle identité et n’est pas déjà associé au compte source. Un conteneur running seul ne valide pas cette étape.
8. Depuis l’interface, fournir un claim éphémère destiné au compte de test autorisé. Ne pas le copier dans une commande, un ticket ou un log. Vérifier association effective après la seconde recréation, absence de PLEX_CLAIM dans les fichiers et dans l’environnement Docker (contrôle privé sans afficher l’environnement complet).
9. Dans Plex : comparer bibliothèques, collections, affiches, chemins et lecture d’un média de test. Vérifier qu’aucune reconstruction complète n’est déclenchée. Observer inventaire, métriques, réconciliation et réservation des ports après un nouveau heartbeat.
10. Sur ce seul environnement de test, simuler une interruption de transfert et un refus de claim. Exiger échec lisible, absence de publication partielle et source restaurée. Conserver les preuves expurgées et les durées (arrêt, capture, upload, restore, claim).

Consigner chaque étape en exécutée/réussie/échouée/non exécutée, avec date, opérateur, commit, versions agents/Plex, source et destination exactes. Aucun verdict production avant réussite complète et validation du rollback.

## Rollback

- **Capture** : confirmer l’état initial source. Si elle était running et reste arrêtée, utiliser `docker start NOM_SOURCE_APPROUVE`, puis vérifier running et `/identity`. Ne pas lancer une nouvelle capture avant retour à l’état initial.
- **Upload interrompu** : les erreurs ordinaires nettoient le temporaire. Après arrêt brutal, confirmer qu’aucun agent n’upload, puis retirer uniquement le verrou `.upload.lock` et les `.uploading` du build concerné. Une archive déjà stockée différente impose un nouveau build. Ne pas supprimer une version publiée utilisée.
- **Restore échoué** : ne pas retenter sur le répertoire partiellement préparé. Inspecter l’AppBox de test, conserver les diagnostics, puis archiver via le workflow existant. Toute suppression de ce répertoire exige sauvegarde et confirmation du chemin exact. Aucun nettoyage récursif global du cache ou de `/srv/appboxes`.
- **Claim** : les fichiers sont nettoyés même après refus. Si la recréation de nettoyage a échoué, vérifier les fichiers privés puis recréer seulement le service Plex de l’AppBox test depuis le Compose nettoyé ; vérifier l’absence du jeton dans Docker sans le journaliser. Le claim n’est pas déclaré réussi tant que la santé finale n’est pas confirmée.
- **Agent** : restaurer ensemble les fichiers sauvegardés et redémarrer le service. Ne pas associer l’ancien script à un nouveau package partiel.
- **Control Plane** : arrêter le candidat pendant une maintenance approuvée. Utiliser un override Compose privé contenant `services: {appbox-manager: {image: appbox-manager:pre-alpha5-validation}}`, puis `docker compose -f docker-compose.yml -f CHEMIN_OVERRIDE_PRIVE up -d --no-build appbox-manager`. Cette tâche n’introduit pas de migration de schéma ; restaurer la sauvegarde DB uniquement si nécessaire, application arrêtée, après conservation de l’état courant et décision opérateur.

Vérifier enfin `/health`, heartbeat, inventaires, ports et accès aux AppBox existantes. Ne pas faire de merge main/develop ou de tag production pendant cette procédure de validation.
