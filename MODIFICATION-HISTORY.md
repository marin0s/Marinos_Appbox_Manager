# 1.4.0 — Sprint 4 Phase 2

- Suppression standard transactionnelle avec vérification distante.
- Commit BDD uniquement après absence confirmée des conteneurs et du dossier AppBox.
- Audit et job conservés après suppression.
- Correctif d’archivage et packaging agent systemd.
- Exclusion de `control-plane-runtime/` du script de mise à niveau.

# Historique des modifications

| Version | Date | Modification principale |
|---|---|---|
| 1.2.3 | 2026-07-30 | Final Polish du Sprint 3 et corrections UX |
| 1.2.2 | 2026-07-29 | Actualisation automatique de l'UI après les actions de cycle de vie |
| 1.2.1 | 2026-07-29 | Hotfix du cycle de vie distant et correction du node cible |
| 1.2.0 | 2026-07-29 | Remote Deployment Engine |

## 2026-07-30 — v1.4.1 Transaction Engine

- Refactor du commit final de suppression avec transaction SQLite immédiate.
- Ajout d'un rollback explicite dans le gestionnaire de connexion SQLite.
- Protection de la boucle worker contre toute exception issue d'un workflow distant.
- Ajout d'une transition terminale best-effort pour éviter les jobs silencieusement bloqués.
- Ajout d'un Recovery Manager au démarrage et d'un watchdog périodique.
- Version Control Plane et agent synchronisée en 1.4.1.
