# Rapport de correction v1.6 — 27 août 2026

## Périmètre et état

Départ : `release/v1.6.0-alpha.5`, commit `7316d8a`. Aucun accès distant, aucune opération Docker réelle, aucun push, aucun merge vers main/develop, aucune réécriture d’historique. La version produit reste `1.6.0-alpha.5-dev` ; aucune livraison de production n’est déclarée.

Le code est prêt pour une validation E2E contrôlée. Cela ne signifie pas que Plex, les permissions Linux, les volumes réels ou la procédure de rollback ont été validés sur le terrain.

## Correctifs, causes et preuves

| Sujet / cause | Correction | Tests associés |
|---|---|---|
| Health et registre annonçaient une capture non intrusive | Capacité vraie et registre Phase 1 aligné | `test_control_plane_health_and_footer_report_development_version`, `test_schema_and_plex_builder_are_installed` |
| ZIP avec fins de ligne et métadonnées variables | Générateur déterministe, LF, dates/modes/ordre fixes, ZIP_STORED ; `.gitattributes` | `test_package_is_reproducible_across_checkout_line_endings`, comparaison complète du ZIP, `test_packaged_agent_imports_without_repository` |
| Préférences ne retiraient pas tous les attributs de claim | Politique commune identité/claim/token/password/secret ; conservation Language, AcceptedEULA, etc. | `test_claim_and_credentials_removed_but_template_preferences_preserved`, test de capture canonique |
| Tar insuffisamment vérifié, notamment gzip tronqué ou types spéciaux | Prévalidation complète, contrôle chemins Windows/POSIX, liens et doublons, streaming ; contrôle DB canonique et SQLite dans copie temporaire | `test_unsafe_archive_rejected_before_any_extraction`, `test_archive_with_corrupt_sqlite_is_rejected` |
| Erreurs SQLite et nettoyage incomplets | Conservation de la copie privée, fermeture des connexions, diagnostic de nettoyage ; source inchangée | `test_corrupt_database_fails_with_cleanup_and_no_source_mutation`, tests diagnostics et sidecars existants |
| Upload acceptait un SHA correct sur un fichier qui n’était pas une archive Plex | SHA recalculé, validation tar/gzip/SQLite avant rename, verrou et immutabilité ; validations lourdes hors boucle HTTP | `test_streamed_upload_is_atomic_and_verified`, `test_failed_upload_preserves_previous_archive`, `test_upload_lock_rejects_concurrent_transfer` |
| Publication écrasait le manifeste et utilisait alpha.3 | Manifeste enrichi conservé, taille vérifiée, version réelle, publication idempotente et versions distinctes par build | `test_success_creates_published_catalogue_entry`, `test_invalid_result_never_publishes` |
| Restore supprimait la configuration existante et écrivait Compose trop tôt | AppBox entière préparée en staging, validation avant publication ; refus de l’écrasement ; recreate sans réapplication de référence | `test_failed_restore_does_not_write_compose_or_touch_existing_appbox`, `test_reference_restore_validates_and_uses_cache` |
| Téléchargement incomplet et cache non suivi | Fichier partiel unique, checksum, validation, cache SHA et états transferring/ready/failed | `test_download_failures_never_publish_target`, `test_restore_reuses_verified_cache_without_network`, `test_cache_failure_clears_ready_metadata` |
| Succès Docker assimilé à santé et usage réussi | Contrôle running/HTTP/identité ; confirmation obligatoire pour restore alpha.5 ; état awaiting_claim ; résultat tardif refusé après interruption | `test_restore_job_rejects_unverified_agent_success`, `test_interrupted_job_does_not_accept_late_agent_result` |
| Retrait du préfixe ab conservait un tiret initial | Suffixe Plex nettoyé, nouveaux IDs ambigus refusés, anciens noms encore reconnus | `test_naming_consistent_for_ab_prefix_separator`, scénario synthétique complet |
| Claim nettoyait l’environnement Docker uniquement sur succès | Nettoyage sur tous les chemins contrôlés, deux recréations, vérification d’identité et association finale, timeout englobant | tests claim success/failure/absent/HTTP/timeout/cleanup, `test_control_plane_claim_erases_queued_token_on_success_and_failure` |
| Résultats distants susceptibles de contenir des tokens | Expurgation agent et avant persistance Control Plane | `test_command_error_redacts_plex_header_and_structured_secrets`, `test_remote_result_secrets_are_redacted_before_persistence` |
| Tests dépendants de Linux/UTF-8 et caches suivis | Connexions fermées explicitement, lectures UTF-8, fsync répertoire conditionnel, retrait du suivi des 22 bytecodes | Suite complète sous Windows, test atomic_write existant |
| Changelog désordonné et alpha.4 absente | Historique conservé, ordre décroissant, section alpha.4 distinguant import et preuve terrain | Revue documentaire, sans inventer de delta alpha.3→alpha.4 |

## Pipeline réellement implémenté

Découverte Plex → arrêt si source active → DB copiées avec sidecars en espace privé → snapshots SQLite et préférences assainies → archive incluant Metadata/Media/configuration utile → validation et checksum → restauration de l’état source dans finally → upload streaming → validation Control Plane → publication automatique de la version et du manifeste.

Au déploiement : distribution **à la demande** vers cache du nœud → validation gzip/tar/SQLite → extraction dans une AppBox temporaire → Compose/manifeste → rename → Docker → running et identité HTTP → job de déploiement réussi mais référence `restored_unclaimed` / déploiement `awaiting_claim` → claim explicite fourni par l’opérateur → nettoyage du jeton → seconde vérification → association confirmée.

Le cache ready décrit une distribution validée, pas l’association à un compte Plex. Aucun nouveau service de prédistribution, scheduler ou moteur transactionnel n’a été ajouté. Les films/épisodes RDAD ne sont pas intégrés à l’archive.

## Vérifications exécutées

Windows, Python 3.12 du runtime local, dépendances installées dans `.venv` (requirements du dépôt, pytest 9.1.1 et httpx 0.28.1). Les commandes de test ont nécessité l’autorisation d’accès aux répertoires temporaires Windows ; elles n’accèdent pas aux nœuds.

```powershell
& '.\.venv\Scripts\python.exe' -m pytest -q --tb=short
& '.\.venv\Scripts\python.exe' -m unittest discover -s tests -q
& '.\.venv\Scripts\python.exe' scripts/package_agent.py --check
& '.\.venv\Scripts\python.exe' -m compileall -q app agent scripts
```

- Pytest final : **74 réussis**, **0 échoué**, **0 skipped**, **23 sous-tests réussis**. Quatre avertissements FastAPI sur `on_event` déprécié ; pas de changement du lifecycle applicatif hors périmètre.
- Unittest final : **74 tests, OK**, sans tests ignorés.
- Packaging : artefact complet identique à sa reconstruction et import isolé hors dépôt validé.
- Compilation Python : réussie. `git diff --check` : aucune erreur d’espacement.
- La première exécution après les premières corrections avait donné 36 réussis/11 échecs : O_DIRECTORY absent sous Windows, ZIP encore ancien, connexions SQLite laissées ouvertes dans les tests et lectures non UTF-8. Ces échecs ne sont pas masqués ou skipped.
- Les premiers essais d’environnement ont échoué faute de pytest puis d’accès au temporaire pour ensurepip ; installation réussie après autorisation dans le venv. Aucun changement du Python système.

### Intégration simulée

`test_synthetic_capture_upload_publish_restore_and_claim` traverse les fonctions réelles de capture, upload, publication, cache, restore et claim avec de vrais fichiers tar/SQLite synthétiques. Docker et le transport HTTP du nœud sont simulés. Les tests d’upload utilisent un flux découpé et une interruption. Les tests de claim simulent HTTP indisponible, identité absente, refus, timeout et nettoyage défaillant.

### Non exécuté

Build/exécution Docker Linux, systemd, OURANOS/ARTEMIS, Plex réel, claim auprès du service Plex, gros volumes et absence de rescan massif, lecture RDAD, observation de la production et rollback terrain. Docker n’est pas disponible dans cet environnement ; WSL n’offre pas de distribution exploitable lors du contrôle local. Aucun de ces essais n’est présenté comme réussi ni compté comme skipped pytest.

Le [runbook E2E](reference-images-alpha5-e2e.md) contient les préconditions, actions, commandes, preuves attendues et rollback. Il impose une autorisation explicite des nœuds et protège DEMETER.

## Risques et limites

- **Plex natif** : le fallback sans tokenizer valide la lisibilité du schéma, pas toute la sémantique Plex. Vérification native et lecture des bibliothèques obligatoires sur copies de test.
- **Durée/espace** : la validation relit l’archive et copie les DB ; mesurer sur les volumes réels. L’upload et la validation ne chargent pas l’archive entière en RAM. Les métadonnées de membres restent en mémoire, avec limites de taille/nombre.
- **Arrêt brutal** : finally couvre les erreurs Python contrôlées, pas un kill/panne hôte. Les verrous, staging et l’état source peuvent nécessiter une intervention ; aucune suppression automatique de données existantes.
- **Claim** : si l’écriture des fichiers nettoyés ou la recréation de nettoyage échoue, l’erreur exige une intervention. Ne pas supposer le jeton absent de Docker dans ce cas. La confirmation `claimed` n’identifie pas à elle seule le compte attendu : l’opérateur doit vérifier ce compte dans Plex pendant l’E2E.
- **Templates** : identité et secrets connus des préférences sont retirés ; les plugins/SQLite métier ne font pas l’objet d’un scanner universel de secrets. Diffusion limitée aux templates contrôlés.
- **Compatibilité** : installer le script agent et son nouveau module ensemble. Les restores alpha.5 exigent la confirmation de santé du nouveau package ; opérations ordinaires des agents anciens conservées.
- **Reprise** : une AppBox existante ne peut pas être écrasée par restore. Après échec, conserver/inspecter puis utiliser un nouvel ID. Pas de reprise automatique de migration ni de garbage collector de cache ajoutés.
- **Dette** : monolithes serveur/agent conservés ; API FastAPI on_event dépréciée ; E2E Linux et non-régression terrain encore nécessaires.

## Audit Git historique passif

Chemins identifiés : `.env`, `data/appbox-manager.db`, `data/appbox-manager-pre-v1.6.0-alpha.1.db`. Les blobs restent dans l’historique ; aucune réécriture ou suppression de données historiques n’a été effectuée.

Sondage limité de la dernière version historique lisible de chaque chemin, sans affichage des valeurs : aucune clé `.env` au nom sensible non vide détectée ; aucune chaîne de forme claim détectée dans les deux blobs SQLite. Les champs renseignés détectés sont `agent_enrollment_tokens.token_id`, `agent_enrollment_tokens.token_hash` et `agent_commands.claimed_at` — identifiants, hashes et horodatages, pas une preuve de token actif en clair. Les DB ont été inspectées en mémoire avec adaptation de l’en-tête WAL de cette seule copie pour lecture hors ligne ; aucun fichier historique n’a été réécrit.

**Rotation** : aucune compromission de secret actif en clair n’est établie par ce sondage. Rotation/révocation préventive recommandée pour les anciens enrôlements encore actifs si la diffusion du dépôt n’est pas maîtrisée. Rotation obligatoire de tout credential actif retrouvé dans un audit exhaustif des JSON, données ou autres révisions. Ce sondage ne certifie pas l’absence de secrets. Prévoir séparément une politique de purge de l’historique et de rotation, avec accord explicite ; ne rien réécrire dans cette tâche.

## Git et stratégie de branche

- `main` reste à `256bba6` (base alpha.4 importée).
- `origin/develop` reste à `f7e3cc2` (socle Sprint 0).
- `origin/release/v1.6.0-alpha.5` reste à `7316d8a` dans les références locales du clone ; pas de fetch ni push.
- La release contenait déjà les trois chantiers audités, huit commits après develop. Les commits de cette tâche s’ajoutent uniquement à la release locale.
- Commits initiaux de correction : `6d63cd8` (health/packaging/changelog), `a306293` (pipeline et lifecycle), `3b475ce` (portabilité et bytecodes). Le commit de clôture contient le runbook, ce rapport et le test/timeout de claim côté Control Plane ; son identifiant est donné dans le compte rendu final.
- Fichiers temporaires de travail sous `builds/`, venv et caches restent ignorés ; aucun artefact de données synthétiques n’est suivi. Aucun secret n’est ajouté au dépôt.

### Fichiers concernés

Code/package : `.gitattributes`, `app/main.py`, `agent/marinos-appbox-agent.py`, `agent/reference_contract.py`, `agent/install-agent.sh`, `agent/appbox-agent-latest.zip`, `scripts/package_agent.py`.

Tests : `tests/reference_fixtures.py`, `test_agent_deployment.py`, `test_alpha5_version_reporting.py`, `test_plex_hot_snapshot.py`, `test_plex_sqlite_diagnostics.py`, `test_reference_build_foundation.py`, `test_reference_build_orchestration.py`, `test_reference_discovery.py`, `test_reference_images_ux.py`, `test_transaction_engine.py` (tous sous `tests/`).

Documentation : `CHANGELOG.md`, `CONTRIBUTING.md`, `README.md`, `ROADMAP.md`, `docs/reference-images.md`, `docs/reference-images-alpha5-acceptance.md`, `docs/reference-images-alpha5-e2e.md`, ce rapport. Les 22 anciens fichiers suivis sous `agent/__pycache__`, `app/__pycache__` et `tests/__pycache__` sont retirés de l’index, pas effacés de l’historique.

V1.6 READY FOR E2E VALIDATION
