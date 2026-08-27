from __future__ import annotations

import json
import hashlib
import secrets
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import tarfile
import time
import uuid
import urllib.request
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Any

os.environ.setdefault("PSUTIL_PROCFS_PATH", os.getenv("APPBOX_PROCFS", "/host/proc"))
import psutil

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

PRODUCT_VERSION = "1.6.0-alpha.5"
VERSION = f"{PRODUCT_VERSION}-dev"
APP_DIR = Path(__file__).resolve().parent
APPBOX_MODE = os.getenv("APPBOX_MODE", "mock").lower()
HOSTNAME = os.getenv("APPBOX_HOSTNAME", "artemis").lower()
BASE_DIR = Path(os.getenv("APPBOX_BASE_DIR", "/srv/appboxes"))
DATA_DIR = Path(os.getenv("APPBOX_DATA_DIR", "/data"))
DB_FILE = Path(os.getenv("APPBOX_DATABASE", str(DATA_DIR / "appbox-manager.db")))
INVENTORY_FILE = Path(os.getenv("APPBOX_INVENTORY", str(DATA_DIR / "appboxes.json")))
JOBS_FILE = Path(os.getenv("APPBOX_JOBS", str(DATA_DIR / "jobs.json")))
AGENT_ASSET_DIR = Path(os.getenv("APPBOX_AGENT_ASSET_DIR", "/app/agent"))
METRICS_INTERVAL = max(5, int(os.getenv("APPBOX_METRICS_INTERVAL", "10")))
JOB_TIMEOUT_SECONDS = max(60, int(os.getenv("APPBOX_JOB_TIMEOUT_SECONDS", "900")))
JOB_WATCHDOG_INTERVAL = max(15, int(os.getenv("APPBOX_JOB_WATCHDOG_INTERVAL", "30")))
REFERENCE_ROOT = Path(os.getenv("APPBOX_REFERENCE_ROOT", "/srv/appbox-manager/reference-images"))
PLEX_RANGE = range(int(os.getenv("APPBOX_PLEX_PORT_START", "32435")), int(os.getenv("APPBOX_PLEX_PORT_END", "32499")) + 1)
TAUTULLI_RANGE = range(int(os.getenv("APPBOX_TAUTULLI_PORT_START", "8182")), int(os.getenv("APPBOX_TAUTULLI_PORT_END", "8249")) + 1)
JELLYFIN_RANGE = range(int(os.getenv("APPBOX_JELLYFIN_PORT_START", "8100")), int(os.getenv("APPBOX_JELLYFIN_PORT_END", "8179")) + 1)

CLIENT_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,20}$")
CLAIM_RE = re.compile(r"^claim-[A-Za-z0-9_-]{8,128}$")
db_lock = Lock()
worker_wakeup = Event()
worker_stop = Event()

CHANGELOG = [
    {
        "version": "1.6.0-alpha.3",
        "released_at": "2026-07-30",
        "title": "Reference Build Orchestration",
        "items": [
            "Chaînage automatique Discovery → capture → Image → Version → publication.",
            "Téléversement authentifié de l’archive Plex depuis le node source.",
            "Manifeste, checksum SHA256 et rapports de construction persistés.",
            "Publication automatique dans le catalogue des images de déploiement.",
        ],
    },
    {
        "version": "1.5.3",
        "released_at": "2026-07-30 17:00 CEST",
        "title": "Reference Deployment",
        "items": [
            "Remplacement du terme Profil de déploiement par Image de déploiement dans le parcours opérateur.",
            "Catalogue dynamique réunissant Plex vierge et les images de référence Plex publiées et disponibles.",
            "Déploiement local ou distant d’une AppBox depuis une image de référence validée.",
            "Transfert authentifié de la configuration de référence vers l’agent du node cible avant le premier démarrage.",
            "Personnalisation Plex avant démarrage afin de supprimer les identifiants et jetons propres au serveur source.",
            "Conservation de la compatibilité avec les anciens profils et les AppBox existantes.",
        ],
    },
    {
        "version": "1.5.2",
        "released_at": "2026-07-30 16:15 CEST",
        "title": "Reference Images UX",
        "items": [
            "Refonte de la page Images de référence autour de la bibliothèque et d’un parcours guidé depuis un serveur.",
            "Suppression du score de compatibilité au profit d’états binaires et de messages d’erreur actionnables.",
            "Séparation nette entre Images de référence et Stockage ; Stockage se concentre sur les mounts et groupes de mounts.",
            "Menu Ressources simplifié : Images de référence, Déploiements, Agents et Stockage.",
            "Masquage des détails internes du moteur dans le parcours opérateur principal.",
        ],
    },
    {
        "version": "1.5.1",
        "released_at": "2026-07-30 15:30 CEST",
        "title": "Plex Discovery",
        "items": [
            "Analyse distante et strictement en lecture seule des instances Plex.",
            "Détection automatique de la version, du conteneur, du /config, des bibliothèques et des volumes médias.",
            "Rapport de tailles, pré-validation, score de compatibilité et politique d'inclusion/exclusion.",
            "Exécution asynchrone via les commandes agents et intégration au système de jobs existant.",
        ],
    },
    {
        "version": "1.5.0",
        "released_at": "2026-07-30 14:30 CEST",
        "title": "Reference Images Foundation",
        "items": [
            "Fondation générique du Reference Build Engine pour Plex puis Jellyfin.",
            "Cycle de vie versionné des références, builds, étapes et journaux persistants.",
            "Déclaration des capacités de builders par les agents distribués.",
            "Nouvelle interface orientée opérateur avec création depuis un serveur et import expert séparé.",
            "Stockage central configurable sous /srv/appbox-manager/reference-images.",
            "Aucune capture intrusive n’est exécutée dans cette phase de fondation.",
        ],
    },
    {
        "version": "1.4.2",
        "released_at": "2026-07-30 12:30 CEST",
        "title": "Deletion Integrity Hotfix",
        "items": [
            "Correction de la suppression définitive lorsque des historiques de placement ou de déploiement référencent encore l’AppBox.",
            "Détachement transactionnel de placement_decisions.client_id et control_plane_deployments.client_id avant suppression.",
            "Tests de non-régression reproduisant l’erreur FOREIGN KEY observée sur TEST141.",
            "Conservation des historiques de placement, de déploiement, des jobs, événements et notifications.",
        ],
    },
    {
        "version": "1.4.1",
        "released_at": "2026-07-30 23:55 CEST",
        "title": "Transaction Engine — Stabilisation des workflows",
        "items": [
            "Protection globale du worker : une exception de workflow ne peut plus arrêter silencieusement la file de jobs.",
            "Commit SQLite atomique et idempotent pour la suppression définitive des AppBox.",
            "Diagnostic détaillé des contraintes de clés étrangères lors de la finalisation.",
            "Watchdog des jobs bloqués avec transition automatique vers FAILED et finalisation des étapes restantes.",
            "Récupération au démarrage des workflows interrompus par un redémarrage du Control Plane.",
            "Audit d’échec garanti pour les suppressions interrompues ou en erreur.",
        ],
    },
    {
        "version": "1.4.0",
        "released_at": "2026-07-30 01:55 CEST",
        "title": "Sprint 4 Phase 2 — Suppression définitive transactionnelle",
        "items": [
            "Suppression standard complète après vérification distante de l’absence des conteneurs et du dossier AppBox.",
            "Conservation de l’AppBox en base en cas d’échec partiel afin d’éviter les états incohérents.",
            "Nettoyage transactionnel des références actives tout en conservant le journal d’audit et le job de résultat.",
            "Correction native de l’état ARCHIVÉE dans la réconciliation et le dashboard.",
            "Correction du packaging de l’agent avec inclusion du service systemd.",
            "Protection explicite des médias RDAD, qui ne sont jamais inclus dans le dossier AppBox supprimé.",
        ],
    },
    {
        "version": "1.3.0",
        "released_at": "2026-07-30 00:30 CEST",
        "title": "Sprint 4 Phase 1 — Suppression sécurisée",
        "items": [
            "Ajout de trois modes de suppression : archivage avec données conservées, suppression standard et purge complète.",
            "Exécution distribuée de la suppression par l’agent du node cible.",
            "Confirmation renforcée pour les AppBox protégées avec saisie explicite du mot SUPPRIMER.",
            "Ajout d’un journal d’audit persistant des opérations sensibles.",
            "Conservation de la base et des fichiers tant que l’étape Docker distante n’est pas terminée avec succès.",
            "Réconciliation et actualisation automatique de l’interface après suppression.",
        ],
    },
    {
        "version": "1.2.3",
        "released_at": "2026-07-30 00:05 CEST",
        "title": "Sprint 3 — Final Polish",
        "items": [
            "Suppression de la carte CRONOS du menu latéral et du badge Mock de la barre supérieure.",
            "Footer et build UI synchronisés avec la version réelle de l’application.",
            "Normalisation des numéros de version du changelog.",
            "Actualisation automatique après un nouveau déploiement réussi.",
            "Remplacement de la confirmation navigateur de suppression par une modale intégrée.",
            "Détails techniques des workflows repliés dans un panneau Afficher plus de détails.",
            "Correction responsive de la carte Provisioning et de ses montages.",
            "Affichage de la version Jellyfin sur les AppBox Jellyfin.",
        ],
    },
    {
        "version": "1.2.2",
        "released_at": "2026-07-29 23:15 CEST",
        "title": "Sprint 3 — Actualisation automatique de l’interface",
        "items": [
            "Actualisation automatique de la page après le succès des actions Start, Stop, Restart et Recreate.",
            "Affichage d’un message de synchronisation avant le rechargement afin de laisser remonter l’inventaire et la réconciliation.",
            "Protection contre les rechargements multiples lors du polling final d’un job.",
            "Invalidation du cache navigateur pour le JavaScript principal de l’interface.",
            "Aucune modification du moteur distant, de la base, du compose CRONOS ou de l’agent de node.",
        ],
    },
    {
        "version": "1.2.1",
        "released_at": "2026-07-29 22:55 CEST",
        "title": "Sprint 3 Phase 2 — Correctif cycle de vie distant",
        "items": [
            "Ajout des actions distantes Start et Restart comme opérations de premier niveau.",
            "Validation du cycle Stop, Start, Restart et Recreate via les agents de node.",
            "Correction de jobs.node_id afin d’enregistrer le node cible réel au lieu du Control Plane.",
            "Conservation du verrou d’opération unique par AppBox pour toutes les actions du cycle de vie.",
            "Mise à jour des actions de la fiche AppBox selon l’état réel du runtime Docker.",
            "Régénération automatique de l’archive agent pendant la mise à niveau.",
            "Mise à niveau non destructive : conservation stricte du compose CRONOS, de la base, du .env et des runtimes.",
            "Les validations et actions restent exécutées exclusivement par l’agent du node cible.",
        ],
    },
    {
        "version": "1.2.0",
        "released_at": "2026-07-29 19:30 CEST",
        "title": "Sprint 3 Phase 1 — Remote Deployment Engine",
        "items": [
            "Ajout d’un manifeste de déploiement versionné et vérifié par checksum.",
            "Déploiement distant atomique de compose.yml et .env dans /srv/appboxes/<client_id>.",
            "Validation stricte des identifiants AppBox et confinement des chemins sur les nodes.",
            "Retour structuré des étapes et du résultat d’exécution vers le Control Plane.",
            "Verrou existant par AppBox conservé afin d’empêcher les opérations concurrentes.",
            "Installateur agent rendu autonome avec création de /srv/appboxes et des répertoires d’état.",
            "Compatibilité conservée avec l’inventaire, la réconciliation et la détection des orphelins du Sprint 2.",
        ],
    },
    {
        "version": "0.9.7",
        "released_at": "2026-07-29 12:05 CEST",
        "title": "Cycle de vie distant sans dépendance au Compose du Control Plane",
        "items": [
            "Les actions distantes utilisent le Compose conservé sur le node.",
            "Start, stop et delete disposent d’un repli direct sur les conteneurs Docker existants.",
            "Deploy et recreate écrivent le Compose transmis lorsqu’il est disponible.",
            "L’installateur redémarre désormais systématiquement un agent déjà installé.",
        ],
    },
    {
        "version": "0.5.0",
        "released_at": "2026-07-26 10:30 CEST",
        "title": "Workflow Engine persistant",
        "items": [
            "Chaque opération est désormais découpée en étapes enregistrées dans SQLite.",
            "Statuts réels par étape : pending, running, success, warning, failed et skipped.",
            "Logs, dates, durées et progression propres à chaque étape.",
            "La popup et la console des jobs affichent directement l’état du backend.",
            "Ajout d’une page de détail complète pour chaque workflow.",
        ],
    },
    {
        "version": "0.4.3",
        "released_at": "2026-07-25 22:40 CEST",
        "title": "Workflows visuels, changelog et logs Node",
        "items": [
            "Correction définitive de la synchronisation des étapes dans les popups de jobs.",
            "Ajout d’un bouton Fermer visible à la fin des déploiements, suppressions et autres opérations.",
            "Ajout d’un changelog versionné accessible depuis le footer.",
            "Ajout d’un volet Logs sur les fiches Node avec activité Docker et logs du provisioner.",
        ],
    },
    {
        "version": "0.4.2",
        "released_at": "2026-07-25 22:05 CEST",
        "title": "Gestion complète des AppBox et monitoring graphique",
        "items": [
            "Suppression propre d’une AppBox via la file globale et popup de progression.",
            "Graphes de ressources Node : CPU, RAM, disque, réseau et I/O.",
            "Refonte des cartes AppBox avec icônes Plex/Jellyfin, labels Claim, VAAPI et RDAD.",
            "Correction des dépassements dans l’activité récente et intégration du nouveau logo Marinos.",
        ],
    },
    {
        "version": "0.4.1",
        "released_at": "2026-07-25 21:36 CEST",
        "title": "Command Center UI/UX",
        "items": [
            "Nouvelle interface rouge/noir avec navigation latérale extensible.",
            "Command Center centralisant Nodes, AppBox, Jobs et événements.",
            "Pages dédiées Nodes, AppBox, Jobs, Notifications et Paramètres.",
            "Popups animées pour les opérations AppBox.",
        ],
    },
    {
        "version": "0.4.0",
        "released_at": "2026-07-25 21:17 CEST",
        "title": "Fondations mononode",
        "items": [
            "SQLite devient la source de vérité.",
            "Ajout de l’inventaire Node et AppBox.",
            "File globale persistante avec worker unique.",
            "Première collecte CPU, RAM, disque, réseau et état Docker.",
        ],
    },
]

WORKFLOW_DEFINITIONS = {
    "deploy": [
        ("validate_node", "Validation du node"),
        ("validate_storage", "Validation RDAD et GPU"),
        ("validate_compose", "Validation du Compose"),
        ("docker_deploy", "Création et démarrage Docker"),
        ("healthcheck", "Vérification des services"),
        ("refresh", "Enregistrement du refresh ciblé"),
        ("watchdog", "Activation du watchdog"),
        ("notification", "Notification de fin"),
    ],
    "start": [
        ("validate_node", "Validation du node"),
        ("validate_compose", "Validation du Compose"),
        ("docker_start", "Démarrage Docker"),
        ("healthcheck", "Vérification des services"),
        ("notification", "Notification de fin"),
    ],
    "restart": [
        ("validate_node", "Validation du node"),
        ("docker_restart", "Redémarrage Docker"),
        ("healthcheck", "Vérification des services"),
        ("notification", "Notification de fin"),
    ],
    "stop": [
        ("validate_appbox", "Validation de l’AppBox"),
        ("docker_stop", "Arrêt des services Docker"),
        ("verify_stopped", "Vérification de l’arrêt"),
        ("notification", "Notification de fin"),
    ],
    "recreate": [
        ("validate_node", "Validation du node"),
        ("validate_storage", "Validation RDAD et GPU"),
        ("validate_compose", "Validation du Compose"),
        ("docker_pull", "Téléchargement des images"),
        ("docker_recreate", "Recréation Docker"),
        ("healthcheck", "Vérification des services"),
        ("notification", "Notification de fin"),
    ],
    "reference_discovery": [
        ("connecting", "Connexion au node source"),
        ("discovering", "Découverte de l’instance Plex"),
        ("collecting_metadata", "Lecture des bibliothèques et métadonnées"),
        ("compatibility_check", "Calcul de compatibilité"),
        ("completed", "Rapport de découverte"),
    ],
    "reference_build": [
        ("discover", "Découverte de l’instance source"),
        ("preflight", "Pré-validation"),
        ("capture", "Capture applicative"),
        ("sanitize", "Nettoyage des données sensibles"),
        ("package", "Compression et checksum"),
        ("transfer", "Transfert vers le catalogue"),
        ("validate_reference", "Validation de la référence"),
        ("publish", "Publication"),
    ],
    "delete": [
        ("validate_appbox", "Validation et protection de l’AppBox"),
        ("docker_remove", "Arrêt et suppression Docker"),
        ("cleanup_files", "Traitement des données persistantes"),
        ("inventory", "Mise à jour de l’inventaire"),
        ("audit", "Écriture du journal d’audit"),
        ("notification", "Notification de fin"),
    ],
}

app = FastAPI(title="Marinos AppBox Manager", version=VERSION)
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))
templates.env.globals["app_version"] = VERSION


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def deployment_env_for(item: dict[str, Any]) -> str:
    """Fichier .env minimal, stable et traçable pour chaque AppBox."""
    values = {
        "APPBOX_CLIENT_ID": str(item.get("client_id") or ""),
        "APPBOX_NODE_ID": str(item.get("node_id") or ""),
        "APPBOX_MEDIA_TYPE": str(item.get("type") or ""),
        "APPBOX_MANAGER_VERSION": VERSION,
    }
    return "".join(f"{key}={value}\n" for key, value in values.items())


def build_deployment_manifest(item: dict[str, Any], compose: str, env_content: str) -> dict[str, Any]:
    client_id = str(item.get("client_id") or "").strip().lower()
    if not CLIENT_RE.fullmatch(client_id):
        raise RuntimeError(f"Identifiant AppBox invalide : {client_id!r}")
    files = {
        "compose.yml": hashlib.sha256(compose.encode("utf-8")).hexdigest(),
        ".env": hashlib.sha256(env_content.encode("utf-8")).hexdigest(),
    }
    manifest = {
        "schema_version": 1,
        "operation": "deploy",
        "client_id": client_id,
        "node_id": str(item.get("node_id") or "").strip().lower(),
        "application_version": VERSION,
        "generated_at": now_iso(),
        "files": files,
    }
    canonical = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    manifest["checksum"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return manifest


@contextmanager
def db():
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_FILE, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


@contextmanager
def immediate_transaction():
    """Exclusive write transaction used for business commits."""
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_FILE, timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    try:
        con.execute("BEGIN IMMEDIATE")
        yield con
        con.execute("COMMIT")
    except Exception:
        try:
            con.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        con.close()


def init_database() -> None:
    with db_lock, db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS nodes (
            node_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            mode TEXT NOT NULL DEFAULT 'local',
            status TEXT NOT NULL DEFAULT 'online',
            maintenance INTEGER NOT NULL DEFAULT 0,
            docker_version TEXT,
            agent_version TEXT NOT NULL DEFAULT 'embedded-0.4.0',
            rdad_ok INTEGER NOT NULL DEFAULT 0,
            gpu_ok INTEGER NOT NULL DEFAULT 0,
            last_seen TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );


        CREATE TABLE IF NOT EXISTS node_tags (
            tag_id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            description TEXT,
            system_tag INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS node_tag_assignments (
            node_id TEXT NOT NULL,
            tag_id TEXT NOT NULL,
            assigned_at TEXT NOT NULL,
            PRIMARY KEY(node_id,tag_id),
            FOREIGN KEY(node_id) REFERENCES nodes(node_id) ON DELETE CASCADE,
            FOREIGN KEY(tag_id) REFERENCES node_tags(tag_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS placement_settings (
            setting_id TEXT PRIMARY KEY,
            default_mode TEXT NOT NULL DEFAULT 'manual',
            automatic_required_tag TEXT NOT NULL DEFAULT 'appbox-node',
            automatic_excluded_tag TEXT NOT NULL DEFAULT 'bare-metal',
            allow_manual_bare_metal INTEGER NOT NULL DEFAULT 1,
            require_confirmation_bare_metal INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS placement_decisions (
            decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id TEXT,
            requested_mode TEXT NOT NULL,
            requested_node_id TEXT,
            selected_node_id TEXT,
            eligible_nodes_json TEXT NOT NULL DEFAULT '[]',
            rejected_nodes_json TEXT NOT NULL DEFAULT '[]',
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(client_id) REFERENCES appboxes(client_id),
            FOREIGN KEY(selected_node_id) REFERENCES nodes(node_id)
        );

        CREATE TABLE IF NOT EXISTS node_agents (
            node_id TEXT PRIMARY KEY,
            agent_id TEXT,
            agent_version TEXT,
            status TEXT NOT NULL DEFAULT 'not_installed',
            endpoint TEXT,
            token_fingerprint TEXT,
            last_heartbeat TEXT,
            capabilities_json TEXT NOT NULL DEFAULT '{}',
            registered_at TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(node_id) REFERENCES nodes(node_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS reference_image_distribution (
            distribution_id INTEGER PRIMARY KEY AUTOINCREMENT,
            version_id TEXT NOT NULL,
            node_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'missing',
            local_path TEXT,
            expected_checksum TEXT,
            actual_checksum TEXT,
            bytes_total INTEGER NOT NULL DEFAULT 0,
            bytes_transferred INTEGER NOT NULL DEFAULT 0,
            started_at TEXT,
            completed_at TEXT,
            last_error TEXT,
            updated_at TEXT NOT NULL,
            UNIQUE(version_id,node_id),
            FOREIGN KEY(version_id) REFERENCES reference_image_versions(version_id) ON DELETE CASCADE,
            FOREIGN KEY(node_id) REFERENCES nodes(node_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS control_plane_deployments (
            deployment_id TEXT PRIMARY KEY,
            client_id TEXT,
            node_id TEXT,
            placement_decision_id INTEGER,
            reference_version_id TEXT,
            status TEXT NOT NULL DEFAULT 'planned',
            current_step TEXT,
            progress INTEGER NOT NULL DEFAULT 0,
            detail TEXT,
            created_at TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(client_id) REFERENCES appboxes(client_id),
            FOREIGN KEY(node_id) REFERENCES nodes(node_id),
            FOREIGN KEY(placement_decision_id) REFERENCES placement_decisions(decision_id),
            FOREIGN KEY(reference_version_id) REFERENCES reference_image_versions(version_id)
        );


        CREATE TABLE IF NOT EXISTS agent_enrollment_tokens (
            token_id TEXT PRIMARY KEY,
            node_id TEXT NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            label TEXT,
            created_at TEXT NOT NULL,
            expires_at TEXT,
            used_at TEXT,
            revoked_at TEXT,
            FOREIGN KEY(node_id) REFERENCES nodes(node_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS agent_commands (
            command_id TEXT PRIMARY KEY,
            node_id TEXT NOT NULL,
            command_type TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'queued',
            created_at TEXT NOT NULL,
            claimed_at TEXT,
            completed_at TEXT,
            result_json TEXT,
            error_text TEXT,
            FOREIGN KEY(node_id) REFERENCES nodes(node_id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS ix_agent_commands_node_status
            ON agent_commands(node_id,status,created_at);

        CREATE TABLE IF NOT EXISTS agent_node_metrics (
            node_id TEXT PRIMARY KEY,
            hostname TEXT,
            os_name TEXT,
            kernel_version TEXT,
            docker_version TEXT,
            compose_version TEXT,
            cpu_model TEXT,
            cpu_count INTEGER,
            load_1 REAL,
            memory_total_bytes INTEGER,
            memory_available_bytes INTEGER,
            disk_total_bytes INTEGER,
            disk_free_bytes INTEGER,
            temperature_c REAL,
            gpu_present INTEGER NOT NULL DEFAULT 0,
            rdad_present INTEGER NOT NULL DEFAULT 0,
            docker_ok INTEGER NOT NULL DEFAULT 0,
            collected_at TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            FOREIGN KEY(node_id) REFERENCES nodes(node_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS appboxes (
            client_id TEXT PRIMARY KEY,
            node_id TEXT NOT NULL,
            media_type TEXT NOT NULL DEFAULT 'plex',
            with_tautulli INTEGER NOT NULL DEFAULT 0,
            plex_port INTEGER,
            tautulli_port INTEGER,
            status TEXT NOT NULL DEFAULT 'generated',
            path TEXT NOT NULL,
            containers_json TEXT NOT NULL,
            last_message TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(node_id) REFERENCES nodes(node_id)
        );

        CREATE UNIQUE INDEX IF NOT EXISTS ux_appbox_node_plex_port
            ON appboxes(node_id, plex_port) WHERE plex_port IS NOT NULL;
        CREATE UNIQUE INDEX IF NOT EXISTS ux_appbox_node_tautulli_port
            ON appboxes(node_id, tautulli_port) WHERE tautulli_port IS NOT NULL;


        CREATE TABLE IF NOT EXISTS storage_mounts (
            mount_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            node_id TEXT NOT NULL,
            host_path TEXT NOT NULL,
            container_path TEXT NOT NULL,
            read_only INTEGER NOT NULL DEFAULT 1,
            propagation TEXT NOT NULL DEFAULT 'rprivate',
            required INTEGER NOT NULL DEFAULT 0,
            media_types_json TEXT NOT NULL DEFAULT '["plex","jellyfin"]',
            enabled INTEGER NOT NULL DEFAULT 1,
            description TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(node_id) REFERENCES nodes(node_id)
        );

        CREATE UNIQUE INDEX IF NOT EXISTS ux_storage_mount_path
            ON storage_mounts(node_id, host_path, container_path);

        CREATE TABLE IF NOT EXISTS mount_groups (
            group_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT,
            is_default INTEGER NOT NULL DEFAULT 0,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS mount_group_members (
            group_id TEXT NOT NULL,
            mount_id TEXT NOT NULL,
            position INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(group_id,mount_id),
            FOREIGN KEY(group_id) REFERENCES mount_groups(group_id) ON DELETE CASCADE,
            FOREIGN KEY(mount_id) REFERENCES storage_mounts(mount_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS catalog_snapshots (
            snapshot_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            media_type TEXT NOT NULL,
            version TEXT NOT NULL DEFAULT '1',
            source_path TEXT,
            checksum TEXT,
            size_bytes INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'draft',
            expected_paths_json TEXT NOT NULL DEFAULT '[]',
            notes TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );


        CREATE TABLE IF NOT EXISTS reference_images (
            image_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            media_type TEXT NOT NULL,
            description TEXT,
            status TEXT NOT NULL DEFAULT 'draft',
            current_version_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS ix_reference_images_type
            ON reference_images(media_type,status,name);

        CREATE TABLE IF NOT EXISTS reference_image_versions (
            version_id TEXT PRIMARY KEY,
            image_id TEXT NOT NULL,
            version TEXT NOT NULL,
            snapshot_id TEXT NOT NULL,
            application_version TEXT,
            checksum TEXT,
            size_bytes INTEGER NOT NULL DEFAULT 0,
            catalog_items INTEGER NOT NULL DEFAULT 0,
            state TEXT NOT NULL DEFAULT 'draft',
            created_at TEXT NOT NULL,
            published_at TEXT,
            notes TEXT,
            FOREIGN KEY(image_id) REFERENCES reference_images(image_id) ON DELETE CASCADE,
            FOREIGN KEY(snapshot_id) REFERENCES catalog_snapshots(snapshot_id)
        );

        CREATE UNIQUE INDEX IF NOT EXISTS ux_reference_image_version
            ON reference_image_versions(image_id,version);

        CREATE TABLE IF NOT EXISTS reference_builds (
            build_id TEXT PRIMARY KEY,
            image_id TEXT,
            version_id TEXT,
            job_id TEXT,
            application TEXT NOT NULL,
            source_node_id TEXT NOT NULL,
            source_instance TEXT,
            display_name TEXT NOT NULL,
            description TEXT,
            status TEXT NOT NULL DEFAULT 'draft',
            current_stage TEXT NOT NULL DEFAULT 'foundation',
            progress INTEGER NOT NULL DEFAULT 0,
            builder_name TEXT NOT NULL,
            builder_version TEXT NOT NULL,
            manifest_schema INTEGER NOT NULL DEFAULT 1,
            requested_by TEXT NOT NULL DEFAULT 'admin',
            source_report_json TEXT NOT NULL DEFAULT '{}',
            preflight_report_json TEXT NOT NULL DEFAULT '{}',
            result_json TEXT NOT NULL DEFAULT '{}',
            error_text TEXT,
            created_at TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(image_id) REFERENCES reference_images(image_id) ON DELETE SET NULL,
            FOREIGN KEY(version_id) REFERENCES reference_image_versions(version_id) ON DELETE SET NULL,
            FOREIGN KEY(job_id) REFERENCES jobs(job_id) ON DELETE SET NULL,
            FOREIGN KEY(source_node_id) REFERENCES nodes(node_id) ON DELETE RESTRICT
        );

        CREATE INDEX IF NOT EXISTS ix_reference_builds_status
            ON reference_builds(status,created_at DESC);
        CREATE INDEX IF NOT EXISTS ix_reference_builds_source
            ON reference_builds(source_node_id,application,created_at DESC);

        CREATE TABLE IF NOT EXISTS reference_build_logs (
            log_id INTEGER PRIMARY KEY AUTOINCREMENT,
            build_id TEXT NOT NULL,
            stage TEXT NOT NULL,
            level TEXT NOT NULL DEFAULT 'info',
            message TEXT NOT NULL,
            details_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            FOREIGN KEY(build_id) REFERENCES reference_builds(build_id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS ix_reference_build_logs_build
            ON reference_build_logs(build_id,log_id);

        CREATE TABLE IF NOT EXISTS reference_builder_registry (
            builder_key TEXT PRIMARY KEY,
            application TEXT NOT NULL,
            display_name TEXT NOT NULL,
            builder_version TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            intrusive_actions_enabled INTEGER NOT NULL DEFAULT 0,
            supported_manifest_schema INTEGER NOT NULL DEFAULT 1,
            description TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS node_reference_builder_capabilities (
            node_id TEXT NOT NULL,
            builder_key TEXT NOT NULL,
            available INTEGER NOT NULL DEFAULT 0,
            details_json TEXT NOT NULL DEFAULT '{}',
            detected_at TEXT NOT NULL,
            PRIMARY KEY(node_id,builder_key),
            FOREIGN KEY(node_id) REFERENCES nodes(node_id) ON DELETE CASCADE,
            FOREIGN KEY(builder_key) REFERENCES reference_builder_registry(builder_key) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS node_reference_cache (
            node_id TEXT NOT NULL,
            version_id TEXT NOT NULL,
            local_path TEXT,
            checksum TEXT,
            status TEXT NOT NULL DEFAULT 'missing',
            size_bytes INTEGER NOT NULL DEFAULT 0,
            last_checked_at TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(node_id,version_id),
            FOREIGN KEY(node_id) REFERENCES nodes(node_id),
            FOREIGN KEY(version_id) REFERENCES reference_image_versions(version_id)
        );

        CREATE TABLE IF NOT EXISTS provisioning_profiles (
            profile_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            media_type TEXT NOT NULL,
            snapshot_id TEXT,
            mount_group_id TEXT,
            storage_mode TEXT NOT NULL DEFAULT 'independent',
            is_blank INTEGER NOT NULL DEFAULT 1,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(snapshot_id) REFERENCES catalog_snapshots(snapshot_id),
            FOREIGN KEY(mount_group_id) REFERENCES mount_groups(group_id)
        );

        CREATE TABLE IF NOT EXISTS appbox_mounts (
            client_id TEXT NOT NULL,
            mount_id TEXT NOT NULL,
            host_path TEXT NOT NULL,
            container_path TEXT NOT NULL,
            read_only INTEGER NOT NULL,
            propagation TEXT NOT NULL,
            PRIMARY KEY(client_id,mount_id),
            FOREIGN KEY(client_id) REFERENCES appboxes(client_id) ON DELETE CASCADE,
            FOREIGN KEY(mount_id) REFERENCES storage_mounts(mount_id)
        );

        CREATE TABLE IF NOT EXISTS snapshot_deployments (
            deployment_id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id TEXT NOT NULL,
            snapshot_id TEXT NOT NULL,
            status TEXT NOT NULL,
            detail TEXT,
            deployed_at TEXT NOT NULL,
            FOREIGN KEY(client_id) REFERENCES appboxes(client_id),
            FOREIGN KEY(snapshot_id) REFERENCES catalog_snapshots(snapshot_id)
        );

        CREATE TABLE IF NOT EXISTS jobs (
            job_id TEXT PRIMARY KEY,
            client_id TEXT,
            node_id TEXT NOT NULL,
            action TEXT NOT NULL,
            title TEXT NOT NULL,
            status TEXT NOT NULL,
            progress INTEGER NOT NULL DEFAULT 0,
            detail TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            queue_position INTEGER,
            FOREIGN KEY(client_id) REFERENCES appboxes(client_id)
        );

        CREATE INDEX IF NOT EXISTS ix_jobs_queue
            ON jobs(status, created_at);
        CREATE INDEX IF NOT EXISTS ix_jobs_client
            ON jobs(client_id, created_at DESC);

        CREATE TABLE IF NOT EXISTS job_steps (
            step_id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL,
            step_key TEXT NOT NULL,
            title TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued',
            progress INTEGER NOT NULL DEFAULT 0,
            detail TEXT,
            executor TEXT NOT NULL DEFAULT 'control-plane',
            resources_json TEXT NOT NULL DEFAULT '{}',
            started_at TEXT,
            finished_at TEXT,
            FOREIGN KEY(job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
        );

        CREATE UNIQUE INDEX IF NOT EXISTS ux_job_steps_job_key
            ON job_steps(job_id, step_key);

        CREATE TABLE IF NOT EXISTS events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id TEXT,
            node_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            level TEXT NOT NULL DEFAULT 'info',
            message TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS audit_log (
            audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor TEXT NOT NULL DEFAULT 'admin',
            action TEXT NOT NULL,
            client_id TEXT,
            node_id TEXT,
            mode TEXT,
            result TEXT NOT NULL,
            detail TEXT,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS ix_audit_client_time
            ON audit_log(client_id, created_at DESC);


        CREATE TABLE IF NOT EXISTS containers (
            container_id TEXT PRIMARY KEY,
            node_id TEXT NOT NULL,
            appbox_id TEXT,
            name TEXT NOT NULL,
            image TEXT,
            image_id TEXT,
            state TEXT NOT NULL DEFAULT 'unknown',
            status TEXT,
            health TEXT,
            restart_count INTEGER NOT NULL DEFAULT 0,
            ports_json TEXT NOT NULL DEFAULT '[]',
            labels_json TEXT NOT NULL DEFAULT '{}',
            mounts_json TEXT NOT NULL DEFAULT '[]',
            networks_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT,
            last_seen TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(node_id) REFERENCES nodes(node_id),
            FOREIGN KEY(appbox_id) REFERENCES appboxes(client_id)
        );

        CREATE INDEX IF NOT EXISTS ix_containers_node
            ON containers(node_id, name);
        CREATE INDEX IF NOT EXISTS ix_containers_appbox
            ON containers(appbox_id, name);

        CREATE TABLE IF NOT EXISTS reconciliation_events (
            reconciliation_id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id TEXT,
            node_id TEXT NOT NULL,
            desired_state TEXT NOT NULL,
            observed_state TEXT NOT NULL,
            result TEXT NOT NULL,
            drift_json TEXT NOT NULL DEFAULT '[]',
            message TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(client_id) REFERENCES appboxes(client_id) ON DELETE CASCADE,
            FOREIGN KEY(node_id) REFERENCES nodes(node_id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS ix_reconciliation_client_time
            ON reconciliation_events(client_id, created_at DESC);

        CREATE TABLE IF NOT EXISTS networks (
            network_id TEXT PRIMARY KEY,
            node_id TEXT NOT NULL,
            appbox_id TEXT,
            name TEXT NOT NULL,
            driver TEXT,
            scope TEXT,
            internal INTEGER NOT NULL DEFAULT 0,
            attachable INTEGER NOT NULL DEFAULT 0,
            labels_json TEXT NOT NULL DEFAULT '{}',
            containers_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT,
            last_seen TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(node_id) REFERENCES nodes(node_id),
            FOREIGN KEY(appbox_id) REFERENCES appboxes(client_id)
        );

        CREATE INDEX IF NOT EXISTS ix_networks_node
            ON networks(node_id, name);
        CREATE INDEX IF NOT EXISTS ix_networks_appbox
            ON networks(appbox_id, name);

        CREATE TABLE IF NOT EXISTS volumes (
            volume_id TEXT PRIMARY KEY,
            node_id TEXT NOT NULL,
            appbox_id TEXT,
            name TEXT NOT NULL,
            driver TEXT,
            mountpoint TEXT,
            scope TEXT,
            labels_json TEXT NOT NULL DEFAULT '{}',
            options_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT,
            last_seen TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(node_id) REFERENCES nodes(node_id),
            FOREIGN KEY(appbox_id) REFERENCES appboxes(client_id)
        );

        CREATE INDEX IF NOT EXISTS ix_volumes_node
            ON volumes(node_id, name);
        CREATE INDEX IF NOT EXISTS ix_volumes_appbox
            ON volumes(appbox_id, name);

        CREATE TABLE IF NOT EXISTS templates (
            template_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            media_type TEXT NOT NULL,
            version TEXT NOT NULL DEFAULT '1',
            enabled INTEGER NOT NULL DEFAULT 1,
            definition_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS port_reservations (
            reservation_id INTEGER PRIMARY KEY AUTOINCREMENT,
            node_id TEXT NOT NULL,
            client_id TEXT,
            service TEXT NOT NULL,
            port INTEGER NOT NULL,
            protocol TEXT NOT NULL DEFAULT 'tcp',
            status TEXT NOT NULL DEFAULT 'reserved',
            reserved_at TEXT NOT NULL,
            released_at TEXT,
            FOREIGN KEY(node_id) REFERENCES nodes(node_id),
            FOREIGN KEY(client_id) REFERENCES appboxes(client_id)
        );

        CREATE UNIQUE INDEX IF NOT EXISTS ux_port_reservations_active
            ON port_reservations(node_id, port, protocol)
            WHERE status='reserved';

        CREATE INDEX IF NOT EXISTS ix_port_reservations_client
            ON port_reservations(client_id, service);

        CREATE TABLE IF NOT EXISTS settings_store (
            setting_key TEXT PRIMARY KEY,
            value_json TEXT NOT NULL,
            scope TEXT NOT NULL DEFAULT 'global',
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS notifications_queue (
            notification_id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id TEXT,
            node_id TEXT,
            channel TEXT NOT NULL DEFAULT 'internal',
            level TEXT NOT NULL DEFAULT 'info',
            title TEXT NOT NULL,
            message TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            sent_at TEXT,
            last_error TEXT,
            FOREIGN KEY(client_id) REFERENCES appboxes(client_id),
            FOREIGN KEY(node_id) REFERENCES nodes(node_id)
        );

        CREATE INDEX IF NOT EXISTS ix_notifications_queue_status
            ON notifications_queue(status, created_at);

        CREATE TABLE IF NOT EXISTS node_metrics (
            metric_id INTEGER PRIMARY KEY AUTOINCREMENT,
            node_id TEXT NOT NULL,
            collected_at TEXT NOT NULL,
            cpu_percent REAL,
            load_1 REAL,
            ram_percent REAL,
            ram_used INTEGER,
            ram_total INTEGER,
            disk_percent REAL,
            disk_free INTEGER,
            disk_read_bps REAL,
            disk_write_bps REAL,
            net_rx_bps REAL,
            net_tx_bps REAL,
            docker_containers INTEGER,
            running_containers INTEGER,
            FOREIGN KEY(node_id) REFERENCES nodes(node_id)
        );

        CREATE INDEX IF NOT EXISTS ix_metrics_node_time
            ON node_metrics(node_id, collected_at DESC);
        """)

        # Migrations additives pour les bases existantes.
        step_columns = {
            row["name"] for row in con.execute("PRAGMA table_info(job_steps)").fetchall()
        }
        if "executor" not in step_columns:
            con.execute(
                "ALTER TABLE job_steps ADD COLUMN executor TEXT NOT NULL DEFAULT 'control-plane'"
            )
        if "resources_json" not in step_columns:
            con.execute(
                "ALTER TABLE job_steps ADD COLUMN resources_json TEXT NOT NULL DEFAULT '{}'"
            )

        appbox_columns = {row["name"] for row in con.execute("PRAGMA table_info(appboxes)").fetchall()}
        for column, definition in {
            "profile_id": "TEXT",
            "snapshot_id": "TEXT",
            "mount_group_id": "TEXT",
            "storage_mode": "TEXT NOT NULL DEFAULT 'independent'",
            "port_mode": "TEXT NOT NULL DEFAULT 'automatic'",
            "plex_username": "TEXT",
            "reference_image_id": "TEXT",
            "reference_version_id": "TEXT",
            "acceleration_mode": "TEXT NOT NULL DEFAULT 'auto'",
            "placement_mode": "TEXT NOT NULL DEFAULT 'manual'",
            "requested_node_id": "TEXT",
            "selected_node_id": "TEXT",
            "placement_reason": "TEXT",
            "desired_state": "TEXT NOT NULL DEFAULT 'running'",
            "observed_state": "TEXT NOT NULL DEFAULT 'unknown'",
            "reconciliation_status": "TEXT NOT NULL DEFAULT 'unknown'",
            "drift_json": "TEXT NOT NULL DEFAULT '[]'",
            "reconciled_at": "TEXT",
            "protection_level": "TEXT NOT NULL DEFAULT 'standard'",
            "archived_at": "TEXT",
        }.items():
            if column not in appbox_columns:
                con.execute(f"ALTER TABLE appboxes ADD COLUMN {column} {definition}")

        job_columns = {row["name"] for row in con.execute("PRAGMA table_info(jobs)").fetchall()}
        if "options_json" not in job_columns:
            con.execute("ALTER TABLE jobs ADD COLUMN options_json TEXT NOT NULL DEFAULT '{}'")

        profile_columns = {
            row["name"] for row in con.execute(
                "PRAGMA table_info(provisioning_profiles)"
            ).fetchall()
        }
        for column, definition in {
            "reference_image_id": "TEXT",
            "reference_version_id": "TEXT",
            "acceleration_mode": "TEXT NOT NULL DEFAULT 'auto'",
        }.items():
            if column not in profile_columns:
                con.execute(
                    f"ALTER TABLE provisioning_profiles ADD COLUMN {column} {definition}"
                )

        existing_image_columns = {row["name"] for row in con.execute("PRAGMA table_info(reference_images)").fetchall()}
        for column, definition in {
            "default_image": "INTEGER NOT NULL DEFAULT 0",
            "stability": "TEXT NOT NULL DEFAULT 'stable'",
            "source_node_id": "TEXT",
            "last_used_at": "TEXT",
            "deployment_count": "INTEGER NOT NULL DEFAULT 0",
        }.items():
            if column not in existing_image_columns:
                con.execute(f"ALTER TABLE reference_images ADD COLUMN {column} {definition}")

        existing_version_columns = {row["name"] for row in con.execute("PRAGMA table_info(reference_image_versions)").fetchall()}
        for column, definition in {
            "archive_path": "TEXT",
            "archive_format": "TEXT NOT NULL DEFAULT 'tar.zst'",
            "manifest_json": "TEXT NOT NULL DEFAULT '{}'",
            "source_report_json": "TEXT NOT NULL DEFAULT '{}'",
            "sanitization_report_json": "TEXT NOT NULL DEFAULT '{}'",
            "builder_version": "TEXT",
            "compressed_size_bytes": "INTEGER NOT NULL DEFAULT 0",
            "metadata_size_bytes": "INTEGER NOT NULL DEFAULT 0",
            "compatibility_json": "TEXT NOT NULL DEFAULT '{}'",
        }.items():
            if column not in existing_version_columns:
                con.execute(f"ALTER TABLE reference_image_versions ADD COLUMN {column} {definition}")

        stamp = now_iso()
        con.execute("""
            INSERT INTO reference_builder_registry(
                builder_key,application,display_name,builder_version,enabled,
                intrusive_actions_enabled,supported_manifest_schema,description,updated_at
            ) VALUES('plex','plex','Plex Reference Builder','1.0',1,0,1,?,?)
            ON CONFLICT(builder_key) DO UPDATE SET
                display_name=excluded.display_name,
                builder_version=excluded.builder_version,
                enabled=excluded.enabled,
                description=excluded.description,
                updated_at=excluded.updated_at
        """, (
            "Fondation du builder Plex. Analyse et capture distante activées dans les phases suivantes.",
            stamp,
        ))

        con.execute("""
            INSERT INTO nodes (
                node_id, name, mode, status, maintenance, agent_version,
                rdad_ok, gpu_ok, last_seen, created_at, updated_at
            ) VALUES (?, ?, 'local', 'online', 0, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(node_id) DO UPDATE SET
                name=excluded.name,
                status='online',
                agent_version=excluded.agent_version,
                rdad_ok=excluded.rdad_ok,
                gpu_ok=excluded.gpu_ok,
                last_seen=excluded.last_seen,
                updated_at=excluded.updated_at
        """, (
            HOSTNAME, HOSTNAME.upper(), f"embedded-{VERSION}",
            int(Path("/mnt/decypharr-poc/.mnt").exists()),
            int(Path("/dev/dri").exists()),
            stamp, stamp, stamp,
        ))

        con.execute("""
            INSERT OR IGNORE INTO settings_store(setting_key,value_json,scope,updated_at)
            VALUES('inventory.sync_interval_seconds','30','global',?)
        """, (stamp,))
        con.execute("""
            INSERT OR IGNORE INTO settings_store(setting_key,value_json,scope,updated_at)
            VALUES('ports.plex_range','[32430,32499]','global',?)
        """, (stamp,))
        con.execute("""
            INSERT OR IGNORE INTO settings_store(setting_key,value_json,scope,updated_at)
            VALUES('ports.tautulli_range','[8180,8249]','global',?)
        """, (stamp,))

        default_templates = [
            (
                'plex-standard',
                'Plex Standard',
                'plex',
                '1',
                json.dumps({
                    'image': 'lscr.io/linuxserver/plex:latest',
                    'rdad_mount': '/mnt/decypharr-poc',
                    'gpu': '/dev/dri',
                    'tautulli_optional': True,
                }, ensure_ascii=False),
            ),
            (
                'jellyfin-standard',
                'Jellyfin Standard',
                'jellyfin',
                '1',
                json.dumps({
                    'image': 'lscr.io/linuxserver/jellyfin:latest',
                    'rdad_mount': '/mnt/decypharr-poc',
                    'gpu': '/dev/dri',
                    'tautulli_optional': False,
                }, ensure_ascii=False),
            ),
        ]
        con.executemany("""
            INSERT OR IGNORE INTO templates(
                template_id,name,media_type,version,enabled,
                definition_json,created_at,updated_at
            ) VALUES(?,?,?,?,1,?,?,?)
        """, [
            (template_id, name, media_type, version, definition, stamp, stamp)
            for template_id, name, media_type, version, definition in default_templates
        ])

        system_tags = [
            ("appbox-node", "AppBox-Node", "Éligible au provisioning automatique", 1),
            ("bare-metal", "Bare-Metal", "Serveur dédié exclu du placement automatique", 1),
            ("control-plane", "Control-Plane", "Héberge le Control Plane", 1),
            ("maintenance", "Maintenance", "Exclu temporairement des placements", 1),
            ("media", "Media", "Services Plex/Jellyfin", 1),
            ("test", "Test", "Node de validation", 0),
        ]
        con.executemany("""
            INSERT OR IGNORE INTO node_tags(
                tag_id,name,description,system_tag,created_at,updated_at
            ) VALUES(?,?,?,?,?,?)
        """, [
            (tag_id,name,description,system_tag,stamp,stamp)
            for tag_id,name,description,system_tag in system_tags
        ])

        con.execute("""
            INSERT OR IGNORE INTO node_tag_assignments(node_id,tag_id,assigned_at)
            VALUES(?,?,?)
        """, (HOSTNAME, "appbox-node", stamp))
        con.execute("""
            INSERT OR IGNORE INTO node_tag_assignments(node_id,tag_id,assigned_at)
            VALUES(?,?,?)
        """, (HOSTNAME, "control-plane", stamp))
        con.execute("""
            INSERT OR IGNORE INTO node_tag_assignments(node_id,tag_id,assigned_at)
            VALUES(?,?,?)
        """, (HOSTNAME, "media", stamp))

        con.execute("""
            INSERT OR IGNORE INTO placement_settings(
                setting_id,default_mode,automatic_required_tag,
                automatic_excluded_tag,allow_manual_bare_metal,
                require_confirmation_bare_metal,updated_at
            ) VALUES('global','manual','appbox-node','bare-metal',1,1,?)
        """, (stamp,))

        con.execute("""
            INSERT OR IGNORE INTO node_agents(
                node_id,agent_id,agent_version,status,endpoint,
                capabilities_json,registered_at,updated_at
            ) VALUES(?,?,?,'embedded',?, ?,?,?)
        """, (
            HOSTNAME,
            f"embedded-{HOSTNAME}",
            f"embedded-{VERSION}",
            "local://docker-socket",
            json.dumps({
                "docker": True,
                "compose": True,
                "filesystem": True,
                "reference_distribution": False,
            }),
            stamp,
            stamp,
        ))

        default_mounts = [
            ("rdad-media", "RDAD Media", "/mnt/decypharr-poc", "/data", 1, "rshared", 1,
             '["plex","jellyfin"]', "Catalogue RDAD partagé"),
            ("nas-athena", "NAS ATHENA", "/mnt/ATHENA", "/ATHENA", 1, "rprivate", 0,
             '["plex","jellyfin"]', "Bibliothèques locales ATHENA"),
            ("nas-nemesis", "NAS NEMESIS", "/mnt/NEMESIS", "/NEMESIS", 1, "rprivate", 0,
             '["plex","jellyfin"]', "Bibliothèques locales NEMESIS"),
        ]
        con.executemany("""
            INSERT OR IGNORE INTO storage_mounts(
                mount_id,name,node_id,host_path,container_path,read_only,
                propagation,required,media_types_json,enabled,description,
                created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,1,?,?,?)
        """, [
            (
                mount_id,
                name,
                HOSTNAME,
                host_path,
                container_path,
                read_only,
                propagation,
                required,
                media_types_json,
                description,
                stamp,
                stamp,
            )
            for (
                mount_id,
                name,
                host_path,
                container_path,
                read_only,
                propagation,
                required,
                media_types_json,
                description,
            ) in default_mounts
        ])

        con.execute("""
            INSERT OR IGNORE INTO mount_groups(
                group_id,name,description,is_default,enabled,created_at,updated_at
            ) VALUES('rdad-standard','RDAD Standard',
                     'RDAD + ATHENA + NEMESIS',1,1,?,?)
        """, (stamp, stamp))
        con.executemany("""
            INSERT OR IGNORE INTO mount_group_members(group_id,mount_id,position)
            VALUES('rdad-standard',?,?)
        """, [("rdad-media", 10), ("nas-athena", 20), ("nas-nemesis", 30)])

        default_profiles = [
            ("plex-blank", "Plex vierge", "plex", None, "rdad-standard", "independent", 1),
            ("jellyfin-blank", "Jellyfin vierge", "jellyfin", None, "rdad-standard", "independent", 1),
        ]
        con.executemany("""
            INSERT OR IGNORE INTO provisioning_profiles(
                profile_id,name,media_type,snapshot_id,mount_group_id,
                storage_mode,is_blank,enabled,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,1,?,?)
        """, [(*row, stamp, stamp) for row in default_profiles])


def migrate_json_data() -> None:
    with db_lock, db() as con:
        count = con.execute("SELECT COUNT(*) FROM appboxes").fetchone()[0]
        if count == 0 and INVENTORY_FILE.exists():
            try:
                payload = json.loads(INVENTORY_FILE.read_text(encoding="utf-8"))
                for item in payload.get("appboxes", {}).values():
                    stamp = item.get("created_at") or now_iso()
                    con.execute("""
                        INSERT OR IGNORE INTO appboxes (
                            client_id, node_id, media_type, with_tautulli,
                            plex_port, tautulli_port, status, path,
                            containers_json, last_message, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        item["client_id"], HOSTNAME, item.get("type", "plex"),
                        int(bool(item.get("with_tautulli"))),
                        item.get("plex_port"), item.get("tautulli_port"),
                        item.get("status", "generated"), item["path"],
                        json.dumps(item.get("containers", []), ensure_ascii=False),
                        item.get("last_message"), stamp,
                        item.get("updated_at") or stamp,
                    ))
            except Exception as exc:
                record_event(None, "inventory_migration_error", f"Migration inventaire JSON impossible : {exc}", "error")

        job_count = con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        if job_count == 0 and JOBS_FILE.exists():
            try:
                payload = json.loads(JOBS_FILE.read_text(encoding="utf-8"))
                for job in payload.get("jobs", {}).values():
                    con.execute("""
                        INSERT OR IGNORE INTO jobs (
                            job_id, client_id, node_id, action, title, status,
                            progress, detail, created_at, updated_at,
                            started_at, finished_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        job["job_id"], job.get("client_id"), HOSTNAME,
                        job.get("action", "unknown"), job.get("title", "Opération"),
                        job.get("status", "success"), int(job.get("progress", 100)),
                        job.get("detail", ""), job.get("created_at", now_iso()),
                        job.get("updated_at", job.get("created_at", now_iso())),
                        job.get("started_at"), job.get("finished_at"),
                    ))
            except Exception as exc:
                record_event(None, "jobs_migration_error", f"Migration jobs JSON impossible : {exc}", "error")


def row_to_appbox(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "client_id": row["client_id"],
        "host": row["node_id"],
        "node_id": row["node_id"],
        "type": row["media_type"],
        "with_tautulli": bool(row["with_tautulli"]),
        "plex_port": row["plex_port"],
        "tautulli_port": row["tautulli_port"],
        "status": row["status"],
        "path": row["path"],
        "containers": json.loads(row["containers_json"] or "[]"),
        "last_message": row["last_message"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "profile_id": row["profile_id"] if "profile_id" in row.keys() else None,
        "snapshot_id": row["snapshot_id"] if "snapshot_id" in row.keys() else None,
        "mount_group_id": row["mount_group_id"] if "mount_group_id" in row.keys() else None,
        "storage_mode": row["storage_mode"] if "storage_mode" in row.keys() else "independent",
        "port_mode": row["port_mode"] if "port_mode" in row.keys() else "automatic",
        "media_port": row["plex_port"],
        "plex_username": row["plex_username"] if "plex_username" in row.keys() else None,
        "reference_image_id": row["reference_image_id"] if "reference_image_id" in row.keys() else None,
        "reference_version_id": row["reference_version_id"] if "reference_version_id" in row.keys() else None,
        "acceleration_mode": row["acceleration_mode"] if "acceleration_mode" in row.keys() else "auto",
        "placement_mode": row["placement_mode"] if "placement_mode" in row.keys() else "manual",
        "requested_node_id": row["requested_node_id"] if "requested_node_id" in row.keys() else row["node_id"],
        "selected_node_id": row["selected_node_id"] if "selected_node_id" in row.keys() else row["node_id"],
        "placement_reason": row["placement_reason"] if "placement_reason" in row.keys() else None,
        "desired_state": row["desired_state"] if "desired_state" in row.keys() else "running",
        "observed_state": row["observed_state"] if "observed_state" in row.keys() else "unknown",
        "reconciliation_status": row["reconciliation_status"] if "reconciliation_status" in row.keys() else "unknown",
        "drift": json.loads(row["drift_json"] or "[]") if "drift_json" in row.keys() else [],
        "reconciled_at": row["reconciled_at"] if "reconciled_at" in row.keys() else None,
        "archived_at": row["archived_at"] if "archived_at" in row.keys() else None,
        "protection_level": row["protection_level"] if "protection_level" in row.keys() else "standard",
    }


def get_appbox(client_id: str) -> dict[str, Any] | None:
    with db() as con:
        row = con.execute("SELECT * FROM appboxes WHERE client_id=? AND status != 'deleted'", (client_id,)).fetchone()
    return row_to_appbox(row) if row else None




AGENT_ONLINE_SECONDS = 180


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def bearer_token(request: Request) -> str:
    authorization = request.headers.get("authorization", "")
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Jeton agent absent.")
    return authorization.split(" ", 1)[1].strip()


def authenticate_agent(request: Request, node_id: str) -> dict[str, Any]:
    raw_token = bearer_token(request)
    digest = token_hash(raw_token)
    with db_lock, db() as con:
        token = con.execute("""
            SELECT * FROM agent_enrollment_tokens
            WHERE node_id=? AND token_hash=? AND revoked_at IS NULL
        """, (node_id, digest)).fetchone()
        if not token:
            raise HTTPException(401, "Jeton agent invalide.")
        if token["expires_at"] and token["expires_at"] < now_iso():
            raise HTTPException(401, "Jeton agent expiré.")
        if token["used_at"] is None:
            con.execute("""
                UPDATE agent_enrollment_tokens SET used_at=? WHERE token_id=?
            """, (now_iso(), token["token_id"]))
    return dict(token)


def agent_is_online(last_heartbeat: str | None) -> bool:
    if not last_heartbeat:
        return False
    try:
        heartbeat = datetime.fromisoformat(last_heartbeat)
        current = datetime.now(timezone.utc)
        return (current - heartbeat).total_seconds() <= AGENT_ONLINE_SECONDS
    except Exception:
        return False


def create_agent_token(node_id: str, label: str = "enrollment") -> tuple[str, str]:
    raw = secrets.token_urlsafe(36)
    token_id = str(uuid.uuid4())
    with db_lock, db() as con:
        con.execute("""
            INSERT INTO agent_enrollment_tokens(
                token_id,node_id,token_hash,label,created_at
            ) VALUES(?,?,?,?,?)
        """, (
            token_id,
            node_id,
            token_hash(raw),
            label.strip() or "enrollment",
            now_iso(),
        ))
    return token_id, raw


def queue_agent_command(
    node_id: str,
    command_type: str,
    payload: dict[str, Any] | None = None,
) -> str:
    command_id = str(uuid.uuid4())
    with db_lock, db() as con:
        con.execute("""
            INSERT INTO agent_commands(
                command_id,node_id,command_type,payload_json,status,created_at
            ) VALUES(?,?,?,?, 'queued',?)
        """, (
            command_id,
            node_id,
            command_type,
            json.dumps(payload or {}, ensure_ascii=False),
            now_iso(),
        ))
    return command_id



def list_node_tags() -> list[dict[str, Any]]:
    with db() as con:
        return [dict(row) for row in con.execute("""
            SELECT * FROM node_tags ORDER BY system_tag DESC,name
        """).fetchall()]


def node_tags_map() -> dict[str, list[dict[str, Any]]]:
    with db() as con:
        rows = con.execute("""
            SELECT a.node_id,t.tag_id,t.name,t.description,t.system_tag
            FROM node_tag_assignments a
            JOIN node_tags t ON t.tag_id=a.tag_id
            ORDER BY t.name
        """).fetchall()
    result: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        result.setdefault(row["node_id"], []).append(dict(row))
    return result


def list_control_nodes() -> list[dict[str, Any]]:
    tags = node_tags_map()
    with db() as con:
        nodes = [dict(row) for row in con.execute("""
            SELECT n.*,a.status AS agent_status,a.agent_version AS registered_agent_version,
                   a.endpoint AS agent_endpoint,a.last_heartbeat,
                   a.capabilities_json,
                   m.os_name,m.kernel_version,m.compose_version,m.cpu_model,m.cpu_count,
                   m.load_1,m.memory_total_bytes,m.memory_available_bytes,
                   m.disk_total_bytes,m.disk_free_bytes,m.temperature_c,
                   m.gpu_present AS metric_gpu_present,
                   m.rdad_present AS metric_rdad_present,
                   m.docker_ok AS metric_docker_ok,
                   m.collected_at AS metrics_collected_at,
                   m.payload_json AS metrics_payload_json
            FROM nodes n
            LEFT JOIN node_agents a ON a.node_id=n.node_id
            LEFT JOIN agent_node_metrics m ON m.node_id=n.node_id
            ORDER BY n.name
        """).fetchall()]
        appbox_counts = {
            row["node_id"]: int(row["count"])
            for row in con.execute("""
                SELECT node_id,COUNT(*) AS count
                FROM appboxes
                WHERE status!='deleted'
                GROUP BY node_id
            """).fetchall()
        }
    for node in nodes:
        node["tags"] = tags.get(node["node_id"], [])
        node["tag_ids"] = [tag["tag_id"] for tag in node["tags"]]
        node["appbox_count"] = appbox_counts.get(node["node_id"], 0)
        node["capabilities"] = json.loads(node.pop("capabilities_json") or "{}")
        node["is_local"] = node["node_id"] == HOSTNAME
        heartbeat_online = agent_is_online(node.get("last_heartbeat"))
        if node["is_local"]:
            node["agent_status"] = "embedded"
            node["agent_online"] = True
        else:
            node["agent_online"] = heartbeat_online
            node["agent_status"] = "online" if heartbeat_online else (
                node.get("agent_status") or "not_installed"
            )
        node["actionable"] = bool(
            node["is_local"]
            or (
                node["agent_online"]
                and node["capabilities"].get("deployment_executor", False)
            )
        )
    return nodes


def placement_config() -> dict[str, Any]:
    with db() as con:
        row = con.execute("""
            SELECT * FROM placement_settings WHERE setting_id='global'
        """).fetchone()
    return dict(row) if row else {
        "default_mode": "manual",
        "automatic_required_tag": "appbox-node",
        "automatic_excluded_tag": "bare-metal",
        "allow_manual_bare_metal": 1,
        "require_confirmation_bare_metal": 1,
    }


def evaluate_placement(
    requested_mode: str,
    requested_node_id: str | None,
    *,
    allow_bare_metal_override: bool = False,
) -> dict[str, Any]:
    config = placement_config()
    nodes = list_control_nodes()
    by_id = {node["node_id"]: node for node in nodes}
    eligible: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    def reject(node: dict[str, Any], reason: str) -> None:
        rejected.append({"node_id": node["node_id"], "reason": reason})

    if requested_mode == "manual":
        node = by_id.get(requested_node_id or "")
        if not node:
            raise HTTPException(400, "Node cible introuvable.")
        if node["maintenance"]:
            raise HTTPException(409, "Le node cible est en maintenance.")
        if "bare-metal" in node["tag_ids"] and not allow_bare_metal_override:
            raise HTTPException(
                409,
                "Ce node est tagué Bare-Metal. Confirme explicitement le déploiement manuel.",
            )
        if not node["actionable"]:
            raise HTTPException(
                409,
                "Ce node ne possède pas encore d’agent actif. Le déploiement distant sera disponible avec l’agent.",
            )
        reason = f"Sélection manuelle du node {node['name']}."
        return {
            "selected": node,
            "eligible": [node],
            "rejected": rejected,
            "reason": reason,
        }

    required_tag = config["automatic_required_tag"]
    excluded_tag = config["automatic_excluded_tag"]
    for node in nodes:
        if required_tag not in node["tag_ids"]:
            reject(node, f"Tag requis {required_tag} absent")
            continue
        if excluded_tag in node["tag_ids"]:
            reject(node, f"Tag d’exclusion {excluded_tag} présent")
            continue
        if node["maintenance"] or "maintenance" in node["tag_ids"]:
            reject(node, "Node en maintenance")
            continue
        if node["status"] != "online":
            reject(node, f"Node {node['status']}")
            continue
        if not node["actionable"]:
            reject(node, "Agent indisponible")
            continue
        if not node["rdad_ok"]:
            reject(node, "RDAD indisponible")
            continue
        eligible.append(node)

    if not eligible:
        raise HTTPException(
            409,
            "Aucun AppBox-Node éligible au placement automatique.",
        )

    # Transparent, deterministic foundation score:
    # prefer reference-ready/local-capable nodes, then fewer AppBoxes, then name.
    eligible.sort(
        key=lambda node: (
            0 if node["is_local"] else 1,
            node["appbox_count"],
            node["name"].lower(),
        )
    )
    selected = eligible[0]
    reason = (
        f"Placement automatique parmi {len(eligible)} AppBox-Node(s) éligible(s) : "
        f"{selected['name']} retenu (agent disponible, RDAD OK, "
        f"{selected['appbox_count']} AppBox active(s))."
    )
    return {
        "selected": selected,
        "eligible": eligible,
        "rejected": rejected,
        "reason": reason,
    }


def record_placement_decision(
    client_id: str | None,
    requested_mode: str,
    requested_node_id: str | None,
    result: dict[str, Any],
) -> int:
    with db_lock, db() as con:
        cursor = con.execute("""
            INSERT INTO placement_decisions(
                client_id,requested_mode,requested_node_id,selected_node_id,
                eligible_nodes_json,rejected_nodes_json,reason,created_at
            ) VALUES(?,?,?,?,?,?,?,?)
        """, (
            client_id,
            requested_mode,
            requested_node_id,
            result["selected"]["node_id"],
            json.dumps(
                [node["node_id"] for node in result["eligible"]],
                ensure_ascii=False,
            ),
            json.dumps(result["rejected"], ensure_ascii=False),
            result["reason"],
            now_iso(),
        ))
        return int(cursor.lastrowid)


def distribution_matrix() -> list[dict[str, Any]]:
    nodes = list_control_nodes()
    versions = list_reference_versions()
    with db() as con:
        rows = con.execute("""
            SELECT * FROM reference_image_distribution
        """).fetchall()
    states = {
        (row["version_id"], row["node_id"]): dict(row)
        for row in rows
    }
    matrix = []
    for version in versions:
        item = dict(version)
        item["nodes"] = []
        for node in nodes:
            state = states.get((version["version_id"], node["node_id"]))
            item["nodes"].append({
                "node": node,
                "distribution": state or {
                    "status": "local" if (
                        node["is_local"] and version.get("source_available")
                    ) else "missing",
                    "bytes_transferred": 0,
                    "bytes_total": version.get("size_bytes") or 0,
                    "updated_at": None,
                },
            })
        matrix.append(item)
    return matrix



def list_appboxes() -> list[dict[str, Any]]:
    with db() as con:
        rows = con.execute("SELECT * FROM appboxes WHERE status != 'deleted' ORDER BY client_id").fetchall()
    return [row_to_appbox(row) for row in rows]


def save_appbox_status(client_id: str, status: str, message: str) -> None:
    with db_lock, db() as con:
        con.execute("""
            UPDATE appboxes SET status=?, last_message=?, updated_at=?
            WHERE client_id=?
        """, (status, message[-3000:], now_iso(), client_id))


def record_event(client_id: str | None, event_type: str, message: str, level: str = "info") -> None:
    try:
        with db_lock, db() as con:
            con.execute("""
                INSERT INTO events(client_id,node_id,event_type,level,message,created_at)
                VALUES(?,?,?,?,?,?)
            """, (client_id, HOSTNAME, event_type, level, message[-8000:], now_iso()))
    except Exception:
        pass


def record_audit(action: str, client_id: str | None, node_id: str | None, mode: str | None, result: str, detail: str, actor: str = "admin") -> None:
    with db_lock, db() as con:
        con.execute("""
            INSERT INTO audit_log(actor,action,client_id,node_id,mode,result,detail,created_at)
            VALUES(?,?,?,?,?,?,?,?)
        """, (actor, action, client_id, node_id, mode, result, detail[-12000:], now_iso()))


def list_events(client_id: str, limit: int = 50) -> list[dict[str, Any]]:
    with db() as con:
        rows = con.execute("""
            SELECT created_at AS at, event_type AS event, level, message
            FROM events WHERE client_id=? ORDER BY event_id DESC LIMIT ?
        """, (client_id, limit)).fetchall()
    return [dict(row) for row in rows]



def appbox_id_for_resource(name: str, labels: dict[str, Any] | None = None) -> str | None:
    labels = labels or {}
    explicit_appbox_id = str(labels.get("marinos.appbox.id") or "").strip().lower()
    candidates = list_appboxes()
    if explicit_appbox_id:
        for item in candidates:
            if item["client_id"].lower() == explicit_appbox_id:
                return item["client_id"]
    compose_project = str(labels.get("com.docker.compose.project") or "").lower()
    for item in candidates:
        client_id = item["client_id"].lower()
        short_id = client_id.removeprefix("ab")
        expected = {
            client_id,
            f"plex-appb-{short_id}",
            f"tautulli-{client_id}",
            f"jellyfin-{client_id}",
        }
        if name.lower() in expected:
            return item["client_id"]
        if compose_project and (
            compose_project == client_id
            or client_id in compose_project
            or short_id in compose_project
        ):
            return item["client_id"]
    return None


def docker_json(command: list[str], timeout: int = 60) -> tuple[bool, Any, str]:
    try:
        result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, None, f"Commande Docker interrompue après {timeout} secondes."
    except Exception as exc:
        return False, None, f"Erreur Docker : {exc}"
    stdout, stderr = result.stdout or "", result.stderr or ""
    if result.returncode != 0:
        return False, None, (stdout + "\n" + stderr).strip()[-16000:]
    try:
        return True, json.loads(stdout), stdout[-16000:]
    except Exception as exc:
        return False, None, f"JSON Docker invalide : {exc}\n{(stdout + stderr)[-4000:]}"


def sync_container_inventory() -> tuple[int, list[str]]:
    ok, ids_output = run_command(["docker", "ps", "-aq"], timeout=30)
    if not ok:
        return 0, [ids_output]
    ids = [line.strip() for line in ids_output.splitlines() if line.strip()]
    stamp = now_iso()
    errors: list[str] = []
    seen: set[str] = set()

    if ids:
        ok, payload, raw = docker_json(["docker", "inspect", *ids], timeout=60)
        if not ok:
            return 0, [raw]
        with db_lock, db() as con:
            for item in payload:
                container_id = item.get("Id")
                if not container_id:
                    continue
                seen.add(container_id)
                config = item.get("Config") or {}
                state = item.get("State") or {}
                network_settings = item.get("NetworkSettings") or {}
                labels = config.get("Labels") or {}
                name = str(item.get("Name") or "").lstrip("/")
                appbox_id = appbox_id_for_resource(name, labels)

                ports = []
                for key, bindings in (network_settings.get("Ports") or {}).items():
                    container_port, _, proto = key.partition("/")
                    for binding in bindings or [None]:
                        ports.append({
                            "container_port": container_port,
                            "protocol": proto or "tcp",
                            "host_ip": (binding or {}).get("HostIp"),
                            "host_port": (binding or {}).get("HostPort"),
                        })

                mounts = [
                    {
                        "type": mount.get("Type"),
                        "source": mount.get("Source"),
                        "destination": mount.get("Destination"),
                        "mode": mount.get("Mode"),
                        "rw": mount.get("RW"),
                        "propagation": mount.get("Propagation"),
                    }
                    for mount in (item.get("Mounts") or [])
                ]

                networks = [
                    {
                        "name": network_name,
                        "network_id": details.get("NetworkID"),
                        "ip_address": details.get("IPAddress"),
                        "gateway": details.get("Gateway"),
                        "aliases": details.get("Aliases") or [],
                    }
                    for network_name, details in (network_settings.get("Networks") or {}).items()
                ]

                con.execute("""
                    INSERT INTO containers(
                        container_id,node_id,appbox_id,name,image,image_id,state,status,
                        health,restart_count,ports_json,labels_json,mounts_json,
                        networks_json,created_at,last_seen,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(container_id) DO UPDATE SET
                        node_id=excluded.node_id,
                        appbox_id=excluded.appbox_id,
                        name=excluded.name,
                        image=excluded.image,
                        image_id=excluded.image_id,
                        state=excluded.state,
                        status=excluded.status,
                        health=excluded.health,
                        restart_count=excluded.restart_count,
                        ports_json=excluded.ports_json,
                        labels_json=excluded.labels_json,
                        mounts_json=excluded.mounts_json,
                        networks_json=excluded.networks_json,
                        last_seen=excluded.last_seen,
                        updated_at=excluded.updated_at
                """, (
                    container_id,
                    HOSTNAME,
                    appbox_id,
                    name,
                    config.get("Image"),
                    item.get("Image"),
                    state.get("Status") or "unknown",
                    state.get("Status"),
                    (state.get("Health") or {}).get("Status"),
                    int(state.get("RestartCount") or 0),
                    json.dumps(ports, ensure_ascii=False),
                    json.dumps(labels, ensure_ascii=False),
                    json.dumps(mounts, ensure_ascii=False),
                    json.dumps(networks, ensure_ascii=False),
                    item.get("Created"),
                    stamp,
                    stamp,
                ))
            if seen:
                placeholders = ",".join("?" for _ in seen)
                con.execute(
                    f"DELETE FROM containers WHERE node_id=? AND container_id NOT IN ({placeholders})",
                    (HOSTNAME, *seen),
                )
            else:
                con.execute("DELETE FROM containers WHERE node_id=?", (HOSTNAME,))
    else:
        with db_lock, db() as con:
            con.execute("DELETE FROM containers WHERE node_id=?", (HOSTNAME,))
    return len(seen), errors


def sync_network_inventory() -> tuple[int, list[str]]:
    ok, ids_output = run_command(["docker", "network", "ls", "-q"], timeout=30)
    if not ok:
        return 0, [ids_output]
    ids = [line.strip() for line in ids_output.splitlines() if line.strip()]
    stamp = now_iso()
    seen: set[str] = set()
    if ids:
        ok, payload, raw = docker_json(["docker", "network", "inspect", *ids], timeout=60)
        if not ok:
            return 0, [raw]
        with db_lock, db() as con:
            for item in payload:
                network_id = item.get("Id")
                if not network_id:
                    continue
                seen.add(network_id)
                labels = item.get("Labels") or {}
                name = item.get("Name") or network_id[:12]
                appbox_id = appbox_id_for_resource(name, labels)
                attached = [
                    {
                        "container_id": container_id,
                        "name": details.get("Name"),
                        "ipv4": details.get("IPv4Address"),
                        "ipv6": details.get("IPv6Address"),
                    }
                    for container_id, details in (item.get("Containers") or {}).items()
                ]
                con.execute("""
                    INSERT INTO networks(
                        network_id,node_id,appbox_id,name,driver,scope,internal,
                        attachable,labels_json,containers_json,created_at,last_seen,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(network_id) DO UPDATE SET
                        node_id=excluded.node_id,
                        appbox_id=excluded.appbox_id,
                        name=excluded.name,
                        driver=excluded.driver,
                        scope=excluded.scope,
                        internal=excluded.internal,
                        attachable=excluded.attachable,
                        labels_json=excluded.labels_json,
                        containers_json=excluded.containers_json,
                        last_seen=excluded.last_seen,
                        updated_at=excluded.updated_at
                """, (
                    network_id,
                    HOSTNAME,
                    appbox_id,
                    name,
                    item.get("Driver"),
                    item.get("Scope"),
                    int(bool(item.get("Internal"))),
                    int(bool(item.get("Attachable"))),
                    json.dumps(labels, ensure_ascii=False),
                    json.dumps(attached, ensure_ascii=False),
                    item.get("Created"),
                    stamp,
                    stamp,
                ))
            placeholders = ",".join("?" for _ in seen)
            con.execute(
                f"DELETE FROM networks WHERE node_id=? AND network_id NOT IN ({placeholders})",
                (HOSTNAME, *seen),
            )
    return len(seen), []


def sync_volume_inventory() -> tuple[int, list[str]]:
    ok, names_output = run_command(["docker", "volume", "ls", "-q"], timeout=30)
    if not ok:
        return 0, [names_output]
    names = [line.strip() for line in names_output.splitlines() if line.strip()]
    stamp = now_iso()
    seen: set[str] = set()
    if names:
        ok, payload, raw = docker_json(["docker", "volume", "inspect", *names], timeout=60)
        if not ok:
            return 0, [raw]
        with db_lock, db() as con:
            for item in payload:
                name = item.get("Name")
                if not name:
                    continue
                volume_id = f"{HOSTNAME}:{name}"
                seen.add(volume_id)
                labels = item.get("Labels") or {}
                appbox_id = appbox_id_for_resource(name, labels)
                con.execute("""
                    INSERT INTO volumes(
                        volume_id,node_id,appbox_id,name,driver,mountpoint,scope,
                        labels_json,options_json,created_at,last_seen,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(volume_id) DO UPDATE SET
                        node_id=excluded.node_id,
                        appbox_id=excluded.appbox_id,
                        name=excluded.name,
                        driver=excluded.driver,
                        mountpoint=excluded.mountpoint,
                        scope=excluded.scope,
                        labels_json=excluded.labels_json,
                        options_json=excluded.options_json,
                        last_seen=excluded.last_seen,
                        updated_at=excluded.updated_at
                """, (
                    volume_id,
                    HOSTNAME,
                    appbox_id,
                    name,
                    item.get("Driver"),
                    item.get("Mountpoint"),
                    item.get("Scope"),
                    json.dumps(labels, ensure_ascii=False),
                    json.dumps(item.get("Options") or {}, ensure_ascii=False),
                    item.get("CreatedAt"),
                    stamp,
                    stamp,
                ))
            placeholders = ",".join("?" for _ in seen)
            con.execute(
                f"DELETE FROM volumes WHERE node_id=? AND volume_id NOT IN ({placeholders})",
                (HOSTNAME, *seen),
            )
    else:
        with db_lock, db() as con:
            con.execute("DELETE FROM volumes WHERE node_id=?", (HOSTNAME,))
    return len(seen), []


def sync_port_reservations() -> int:
    stamp = now_iso()
    appboxes = list_appboxes()
    expected: set[tuple[str, int, str]] = set()
    with db_lock, db() as con:
        for item in appboxes:
            mappings = [
                ("plex", item.get("plex_port")),
                ("tautulli", item.get("tautulli_port")),
            ]
            for service, port in mappings:
                if not port:
                    continue
                expected.add((item["client_id"], int(port), service))
                con.execute("""
                    INSERT INTO port_reservations(
                        node_id,client_id,service,port,protocol,status,reserved_at
                    ) VALUES(?,?,?,?,?,'reserved',?)
                    ON CONFLICT(node_id,port,protocol) WHERE status='reserved'
                    DO UPDATE SET
                        client_id=excluded.client_id,
                        service=excluded.service
                """, (HOSTNAME, item["client_id"], service, int(port), "tcp", stamp))

        active = con.execute("""
            SELECT reservation_id,client_id,service,port
            FROM port_reservations
            WHERE node_id=? AND status='reserved'
        """, (HOSTNAME,)).fetchall()
        for row in active:
            key = (row["client_id"], int(row["port"]), row["service"])
            if key not in expected:
                con.execute("""
                    UPDATE port_reservations
                    SET status='released',released_at=?
                    WHERE reservation_id=?
                """, (stamp, row["reservation_id"]))
    return len(expected)


def sync_business_inventory() -> dict[str, Any]:
    containers_count, container_errors = sync_container_inventory()
    networks_count, network_errors = sync_network_inventory()
    volumes_count, volume_errors = sync_volume_inventory()
    reservations_count = sync_port_reservations()
    errors = container_errors + network_errors + volume_errors
    result = {
        "containers": containers_count,
        "networks": networks_count,
        "volumes": volumes_count,
        "port_reservations": reservations_count,
        "errors": errors,
        "synced_at": now_iso(),
    }
    if errors:
        record_event(None, "inventory_sync_warning", " | ".join(errors)[-8000:], "warning")
    else:
        record_event(
            None,
            "inventory_sync",
            (
                f"Inventaire synchronisé : {containers_count} conteneur(s), "
                f"{networks_count} réseau(x), {volumes_count} volume(s), "
                f"{reservations_count} réservation(s)."
            ),
            "success",
        )
    return result


def inventory_snapshot() -> dict[str, Any]:
    with db() as con:
        counts = {
            "containers": con.execute("SELECT COUNT(*) FROM containers WHERE node_id=?", (HOSTNAME,)).fetchone()[0],
            "networks": con.execute("SELECT COUNT(*) FROM networks WHERE node_id=?", (HOSTNAME,)).fetchone()[0],
            "volumes": con.execute("SELECT COUNT(*) FROM volumes WHERE node_id=?", (HOSTNAME,)).fetchone()[0],
            "templates": con.execute("SELECT COUNT(*) FROM templates WHERE enabled=1").fetchone()[0],
            "port_reservations": con.execute("""
                SELECT COUNT(*) FROM port_reservations
                WHERE node_id=? AND status='reserved'
            """, (HOSTNAME,)).fetchone()[0],
            "notifications_pending": con.execute("""
                SELECT COUNT(*) FROM notifications_queue WHERE status='pending'
            """).fetchone()[0],
        }

        containers = [
            {
                **dict(row),
                "ports": json.loads(row["ports_json"] or "[]"),
                "labels": json.loads(row["labels_json"] or "{}"),
                "mounts": json.loads(row["mounts_json"] or "[]"),
                "networks": json.loads(row["networks_json"] or "[]"),
            }
            for row in con.execute("""
                SELECT * FROM containers WHERE node_id=? ORDER BY name
            """, (HOSTNAME,)).fetchall()
        ]
        networks = [
            {
                **dict(row),
                "containers": json.loads(row["containers_json"] or "[]"),
                "labels": json.loads(row["labels_json"] or "{}"),
            }
            for row in con.execute("""
                SELECT * FROM networks WHERE node_id=? ORDER BY name
            """, (HOSTNAME,)).fetchall()
        ]
        volumes = [
            {
                **dict(row),
                "labels": json.loads(row["labels_json"] or "{}"),
                "options": json.loads(row["options_json"] or "{}"),
            }
            for row in con.execute("""
                SELECT * FROM volumes WHERE node_id=? ORDER BY name
            """, (HOSTNAME,)).fetchall()
        ]
        templates_rows = [
            {
                **dict(row),
                "definition": json.loads(row["definition_json"] or "{}"),
            }
            for row in con.execute("""
                SELECT * FROM templates ORDER BY media_type,name
            """).fetchall()
        ]
        reservations = [
            dict(row)
            for row in con.execute("""
                SELECT * FROM port_reservations
                WHERE node_id=? AND status='reserved'
                ORDER BY port
            """, (HOSTNAME,)).fetchall()
        ]

    return {
        "counts": counts,
        "containers": containers,
        "networks": networks,
        "volumes": volumes,
        "templates": templates_rows,
        "reservations": reservations,
    }



def slugify_identifier(value: str) -> str:
    value = re.sub(r"[^a-z0-9-]+", "-", value.strip().lower()).strip("-")
    return value[:48]


def list_storage_mounts(enabled_only: bool = False) -> list[dict[str, Any]]:
    query = "SELECT * FROM storage_mounts"
    params: tuple[Any, ...] = ()
    if enabled_only:
        query += " WHERE enabled=1"
    query += " ORDER BY required DESC,name"
    with db() as con:
        rows = con.execute(query, params).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["media_types"] = json.loads(item.pop("media_types_json") or "[]")
        item["available"] = Path(item["host_path"]).exists()
        result.append(item)
    return result


def list_mount_groups() -> list[dict[str, Any]]:
    with db() as con:
        groups = con.execute("""
            SELECT * FROM mount_groups WHERE enabled=1
            ORDER BY is_default DESC,name
        """).fetchall()
        result = []
        for group in groups:
            mounts = con.execute("""
                SELECT m.* FROM storage_mounts m
                JOIN mount_group_members gm ON gm.mount_id=m.mount_id
                WHERE gm.group_id=? AND m.enabled=1
                ORDER BY gm.position,m.name
            """, (group["group_id"],)).fetchall()
            item = dict(group)
            item["mounts"] = []
            for row in mounts:
                mount = dict(row)
                mount["media_types"] = json.loads(mount.pop("media_types_json") or "[]")
                mount["available"] = Path(mount["host_path"]).exists()
                item["mounts"].append(mount)
            result.append(item)
    return result


def mounts_for_group(group_id: str | None, media_type: str) -> list[dict[str, Any]]:
    if not group_id:
        return []
    groups = {item["group_id"]: item for item in list_mount_groups()}
    group = groups.get(group_id)
    if not group:
        raise HTTPException(400, "Groupe de montages introuvable.")
    return [
        mount for mount in group["mounts"]
        if media_type in mount["media_types"]
    ]


def validate_mounts(mounts: list[dict[str, Any]]) -> list[str]:
    errors = []
    targets: set[str] = set()
    for mount in mounts:
        if mount["container_path"] in targets:
            errors.append(f"Chemin conteneur dupliqué : {mount['container_path']}")
        targets.add(mount["container_path"])
        if mount["required"] and not Path(mount["host_path"]).exists():
            errors.append(f"Montage obligatoire absent : {mount['name']} ({mount['host_path']})")
    return errors


def compose_mount_lines(mounts: list[dict[str, Any]]) -> str:
    lines = []
    for mount in mounts:
        if mount["propagation"] and mount["propagation"] != "rprivate":
            lines.extend([
                "      - type: bind",
                f"        source: {mount['host_path']}",
                f"        target: {mount['container_path']}",
                f"        read_only: {'true' if mount['read_only'] else 'false'}",
                "        bind:",
                f"          propagation: {mount['propagation']}",
            ])
        else:
            suffix = ":ro" if mount["read_only"] else ""
            lines.append(f"      - {mount['host_path']}:{mount['container_path']}{suffix}")
    return "\n".join(lines)


def list_snapshots(media_type: str | None = None) -> list[dict[str, Any]]:
    query = "SELECT * FROM catalog_snapshots"
    params: tuple[Any, ...] = ()
    if media_type:
        query += " WHERE media_type=?"
        params = (media_type,)
    query += " ORDER BY media_type,name,version DESC"
    with db() as con:
        rows = con.execute(query, params).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["expected_paths"] = json.loads(item.pop("expected_paths_json") or "[]")
        item["source_available"] = bool(item["source_path"] and Path(item["source_path"]).exists())
        result.append(item)
    return result


def list_profiles(enabled_only: bool = True) -> list[dict[str, Any]]:
    query = """
        SELECT p.*,
               s.name AS snapshot_name,
               s.version AS snapshot_version,
               i.name AS reference_image_name,
               v.version AS reference_version,
               v.state AS reference_state
        FROM provisioning_profiles p
        LEFT JOIN catalog_snapshots s ON s.snapshot_id=p.snapshot_id
        LEFT JOIN reference_images i ON i.image_id=p.reference_image_id
        LEFT JOIN reference_image_versions v ON v.version_id=p.reference_version_id
    """
    if enabled_only:
        query += " WHERE p.enabled=1"
    query += " ORDER BY p.media_type,p.is_blank DESC,p.name"
    with db() as con:
        rows = con.execute(query).fetchall()
    return [dict(row) for row in rows]



def list_reference_images(media_type: str | None = None) -> list[dict[str, Any]]:
    query = """
        SELECT i.*,
               v.version AS current_version,
               v.application_version,
               v.size_bytes,
               v.catalog_items,
               v.state AS version_state,
               v.snapshot_id,
               v.checksum,
               s.source_path
        FROM reference_images i
        LEFT JOIN reference_image_versions v
          ON v.version_id=i.current_version_id
        LEFT JOIN catalog_snapshots s
          ON s.snapshot_id=v.snapshot_id
    """
    params: tuple[Any, ...] = ()
    if media_type:
        query += " WHERE i.media_type=?"
        params = (media_type,)
    query += " ORDER BY i.media_type,i.name"
    with db() as con:
        images = [dict(row) for row in con.execute(query, params).fetchall()]
        for image in images:
            versions = [
                dict(row) for row in con.execute("""
                    SELECT v.*,s.source_path,s.status AS snapshot_status
                    FROM reference_image_versions v
                    JOIN catalog_snapshots s ON s.snapshot_id=v.snapshot_id
                    WHERE v.image_id=?
                    ORDER BY v.created_at DESC
                """, (image["image_id"],)).fetchall()
            ]
            for version in versions:
                version["source_available"] = bool(
                    version.get("source_path")
                    and Path(version["source_path"]).exists()
                )
            image["versions"] = versions
            image["source_available"] = bool(
                image.get("source_path")
                and Path(image["source_path"]).exists()
            )
    return images


def list_reference_versions(media_type: str | None = None) -> list[dict[str, Any]]:
    query = """
        SELECT v.*,i.name AS image_name,i.media_type,i.status AS image_status,
               s.source_path,s.status AS snapshot_status
        FROM reference_image_versions v
        JOIN reference_images i ON i.image_id=v.image_id
        JOIN catalog_snapshots s ON s.snapshot_id=v.snapshot_id
    """
    params: tuple[Any, ...] = ()
    if media_type:
        query += " WHERE i.media_type=?"
        params = (media_type,)
    query += " ORDER BY i.media_type,i.name,v.created_at DESC"
    with db() as con:
        rows = [dict(row) for row in con.execute(query, params).fetchall()]
    for row in rows:
        row["source_available"] = bool(
            row.get("source_path") and Path(row["source_path"]).exists()
        )
    return rows


def get_reference_version(version_id: str | None) -> dict[str, Any] | None:
    if not version_id:
        return None
    with db() as con:
        row = con.execute("""
            SELECT v.*,i.name AS image_name,i.media_type,
                   s.source_path,s.status AS snapshot_status
            FROM reference_image_versions v
            JOIN reference_images i ON i.image_id=v.image_id
            JOIN catalog_snapshots s ON s.snapshot_id=v.snapshot_id
            WHERE v.version_id=?
        """, (version_id,)).fetchone()
    if not row:
        return None
    result = dict(row)
    result["source_available"] = bool(
        result.get("source_path") and Path(result["source_path"]).exists()
    )
    return result


def calculate_directory_size(source: Path) -> int:
    if not source.exists():
        return 0
    total = 0
    try:
        for item in source.rglob("*"):
            if item.is_file():
                total += item.stat().st_size
    except Exception:
        return total
    return total


def register_reference_version(
    *,
    image_id: str,
    image_name: str,
    media_type: str,
    version: str,
    source_path: str,
    application_version: str,
    expected_paths: list[str],
    catalog_items: int,
    checksum: str,
    notes: str,
    publish: bool,
) -> str:
    source = Path(source_path)
    snapshot_id = slugify_identifier(f"{image_id}-{version}-snapshot")
    version_id = slugify_identifier(f"{image_id}-{version}")
    stamp = now_iso()
    size_bytes = calculate_directory_size(source)
    snapshot_status = "ready" if source.exists() else "missing"
    version_state = "published" if publish and source.exists() else "draft"

    with db_lock, db() as con:
        con.execute("""
            INSERT INTO catalog_snapshots(
                snapshot_id,name,media_type,version,source_path,checksum,
                size_bytes,status,expected_paths_json,notes,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(snapshot_id) DO UPDATE SET
                name=excluded.name,
                source_path=excluded.source_path,
                checksum=excluded.checksum,
                size_bytes=excluded.size_bytes,
                status=excluded.status,
                expected_paths_json=excluded.expected_paths_json,
                notes=excluded.notes,
                updated_at=excluded.updated_at
        """, (
            snapshot_id,
            f"{image_name} {version}",
            media_type,
            version,
            str(source),
            checksum.strip() or None,
            size_bytes,
            snapshot_status,
            json.dumps(expected_paths, ensure_ascii=False),
            notes.strip(),
            stamp,
            stamp,
        ))
        con.execute("""
            INSERT INTO reference_image_versions(
                version_id,image_id,version,snapshot_id,application_version,
                checksum,size_bytes,catalog_items,state,created_at,
                published_at,notes
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(version_id) DO UPDATE SET
                application_version=excluded.application_version,
                checksum=excluded.checksum,
                size_bytes=excluded.size_bytes,
                catalog_items=excluded.catalog_items,
                state=excluded.state,
                published_at=excluded.published_at,
                notes=excluded.notes
        """, (
            version_id,
            image_id,
            version,
            snapshot_id,
            application_version.strip() or None,
            checksum.strip() or None,
            size_bytes,
            max(0, int(catalog_items)),
            version_state,
            stamp,
            stamp if version_state == "published" else None,
            notes.strip(),
        ))
        if version_state == "published":
            con.execute("""
                UPDATE reference_images
                SET current_version_id=?,status='published',updated_at=?
                WHERE image_id=?
            """, (version_id, stamp, image_id))
    return version_id




def _reference_build_storage(build_id: str) -> Path:
    root = (REFERENCE_ROOT / "builds" / slugify_identifier(build_id)).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def queue_reference_capture(build_id: str, discovery: dict[str, Any]) -> str:
    """Queue the intrusive-but-source-preserving capture after successful discovery."""
    with db() as con:
        build = con.execute("SELECT * FROM reference_builds WHERE build_id=?", (build_id,)).fetchone()
    if not build:
        raise HTTPException(404, "Build de référence introuvable.")
    preflight = discovery.get("preflight") or {}
    if not preflight.get("can_build", False):
        raise RuntimeError("La pré-validation Plex interdit la capture de cette référence.")
    payload = {
        "build_id": build_id,
        "application": "plex",
        "source_instance": build["source_instance"],
        "upload_path": f"/api/agent/v1/{build['source_node_id']}/reference-builds/{build_id}/archive",
    }
    command_id = queue_agent_command(build["source_node_id"], "reference_build", payload)
    stamp = now_iso()
    with db_lock, db() as con:
        con.execute("UPDATE reference_builds SET status='building',current_stage='capture',progress=55,completed_at=NULL,updated_at=? WHERE build_id=?", (stamp, build_id))
        con.execute("INSERT INTO reference_build_logs(build_id,stage,level,message,details_json,created_at) VALUES(?,'capture','info',?,'{}',?)", (build_id, "Capture Plex assainie mise en file sur le node source.", stamp))
    return command_id


def finalize_reference_build_command(command: sqlite3.Row, status: str, result: dict[str, Any], error: str | None) -> None:
    if command["command_type"] != "reference_build":
        return
    try:
        payload = json.loads(command["payload_json"] or "{}")
    except Exception:
        payload = {}
    build_id = str(payload.get("build_id") or "")
    if not build_id:
        return
    stamp = now_iso()
    if status != "success":
        detail = error or "Échec de la capture Plex."
        with db_lock, db() as con:
            con.execute("UPDATE reference_builds SET status='build_failed',current_stage='capture',progress=100,error_text=?,completed_at=?,updated_at=? WHERE build_id=?", (detail, stamp, stamp, build_id))
            con.execute("INSERT INTO reference_build_logs(build_id,stage,level,message,details_json,created_at) VALUES(?,'capture','error',?,'{}',?)", (build_id, detail, stamp))
        return
    with db() as con:
        build = con.execute("SELECT * FROM reference_builds WHERE build_id=?", (build_id,)).fetchone()
    if not build:
        return
    archive_path = Path(str(result.get("archive_path") or ""))
    checksum = str(result.get("sha256") or "").lower()
    if not archive_path.exists() or not checksum:
        detail = "Archive de référence absente après téléversement."
        with db_lock, db() as con:
            con.execute("UPDATE reference_builds SET status='build_failed',error_text=?,completed_at=?,updated_at=? WHERE build_id=?", (detail, stamp, stamp, build_id))
        return
    image_id = slugify_identifier(f"plex-{build['display_name']}")
    version_label = datetime.now().strftime("%Y.%m.%d-%H%M%S")
    version_id = slugify_identifier(f"{image_id}-{version_label}")
    snapshot_id = slugify_identifier(f"{version_id}-snapshot")
    source_dir = _reference_build_storage(build_id) / "source"
    source_dir.mkdir(parents=True, exist_ok=True)
    # The uploaded archive itself is the immutable source. A marker keeps the existing availability model intact.
    marker = source_dir / "REFERENCE-ARCHIVE.txt"
    marker.write_text(str(archive_path), encoding="utf-8")
    discovery = json.loads(build["source_report_json"] or "{}")
    preflight = json.loads(build["preflight_report_json"] or "{}")
    manifest = {
        "schema_version": 1, "build_id": build_id, "image_id": image_id, "version_id": version_id,
        "application": "plex", "archive_format": "tar.gz", "archive_sha256": checksum,
        "archive_size_bytes": archive_path.stat().st_size, "created_at": stamp,
        "source_node_id": build["source_node_id"], "source_instance": build["source_instance"],
    }
    with db_lock, db() as con:
        con.execute("""INSERT INTO reference_images(image_id,name,media_type,description,status,current_version_id,created_at,updated_at,source_node_id)
                       VALUES(?,?, 'plex',?,'published',?,?,?,?)
                       ON CONFLICT(image_id) DO UPDATE SET name=excluded.name,description=excluded.description,status='published',current_version_id=excluded.current_version_id,updated_at=excluded.updated_at,source_node_id=excluded.source_node_id""",
                    (image_id, build["display_name"], build["description"] or "", version_id, stamp, stamp, build["source_node_id"]))
        con.execute("""INSERT INTO catalog_snapshots(snapshot_id,name,media_type,version,source_path,checksum,size_bytes,status,expected_paths_json,notes,created_at,updated_at)
                       VALUES(?,?, 'plex',?,?,?,?, 'ready',?, ?,?,?)""",
                    (snapshot_id, f"{build['display_name']} {version_label}", version_label, str(source_dir), checksum, int(result.get("uncompressed_size_bytes") or 0), json.dumps(["Library/Application Support/Plex Media Server"], ensure_ascii=False), "Generated automatically by Reference Builder", stamp, stamp))
        con.execute("""INSERT INTO reference_image_versions(version_id,image_id,version,snapshot_id,application_version,checksum,size_bytes,catalog_items,state,created_at,published_at,notes,archive_path,archive_format,manifest_json,source_report_json,sanitization_report_json,builder_version,compressed_size_bytes,metadata_size_bytes,compatibility_json)
                       VALUES(?,?,?,?,?,?,?,?, 'published',?,?,?,?, 'tar.gz',?,?,?,?,?,?,?)""",
                    (version_id,image_id,version_label,snapshot_id,str((discovery.get('instance') or {}).get('plex_version') or ''),checksum,int(result.get('uncompressed_size_bytes') or 0),len(discovery.get('libraries') or []),stamp,stamp,'Generated automatically from reference build',str(archive_path),json.dumps(manifest,ensure_ascii=False),json.dumps(discovery,ensure_ascii=False),json.dumps(result.get('sanitization') or {},ensure_ascii=False),'1.6.0-alpha.3',archive_path.stat().st_size,int((discovery.get('sizes') or {}).get('metadata') or 0),json.dumps(preflight,ensure_ascii=False)))
        con.execute("UPDATE reference_builds SET image_id=?,version_id=?,status='published',current_stage='published',progress=100,result_json=?,error_text=NULL,completed_at=?,updated_at=? WHERE build_id=?",
                    (image_id,version_id,json.dumps({**result,'manifest':manifest},ensure_ascii=False),stamp,stamp,build_id))
        con.execute("INSERT INTO reference_build_logs(build_id,stage,level,message,details_json,created_at) VALUES(?,'published','success',?,?,?)",
                    (build_id,f"Image {image_id} version {version_label} publiée.",json.dumps({'image_id':image_id,'version_id':version_id,'sha256':checksum},ensure_ascii=False),stamp))


def deployment_images(media_type: str | None = None) -> list[dict[str, Any]]:
    """Return operator-facing deployment images.

    System blank images and published reference versions share one catalogue.
    """
    types = [media_type] if media_type else ["plex", "jellyfin"]
    result: list[dict[str, Any]] = []
    for kind in types:
        if kind not in {"plex", "jellyfin"}:
            continue
        result.append({
            "deployment_image_id": f"blank:{kind}",
            "kind": "blank",
            "media_type": kind,
            "name": f"{kind.capitalize()} vierge",
            "version": "",
            "reference_version_id": None,
            "available": True,
        })
    for version in list_reference_versions(media_type):
        if version.get("state") != "published" or version.get("image_status") != "published":
            continue
        result.append({
            "deployment_image_id": f"reference:{version['version_id']}",
            "kind": "reference",
            "media_type": version["media_type"],
            "name": version["image_name"],
            "version": version["version"],
            "reference_version_id": version["version_id"],
            "available": bool(version.get("source_available")),
        })
    return result


def parse_deployment_image(value: str, media_type: str) -> tuple[str | None, str | None]:
    value = (value or "").strip()
    if not value:
        return None, None
    if value == f"blank:{media_type}":
        return None, None
    if value.startswith("reference:"):
        version_id = value.split(":", 1)[1].strip()
        reference = get_reference_version(version_id)
        if not reference or reference.get("media_type") != media_type:
            raise HTTPException(400, "Image de déploiement incompatible avec l’AppBox.")
        if reference.get("state") != "published" or not reference.get("source_available"):
            raise HTTPException(409, "Cette image de déploiement n’est pas disponible.")
        return reference.get("image_id"), version_id
    raise HTTPException(400, "Image de déploiement invalide.")


def reference_deployment_archive(version_id: str) -> tuple[Path, str]:
    reference = get_reference_version(version_id)
    if not reference or not reference.get("source_available"):
        raise HTTPException(404, "Image de déploiement indisponible.")
    stored_archive = Path(str(reference.get("archive_path") or ""))
    if stored_archive.is_file():
        digest = hashlib.sha256()
        with stored_archive.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        actual = digest.hexdigest()
        expected = str(reference.get("checksum") or "").lower()
        if expected and not secrets.compare_digest(actual, expected):
            raise HTTPException(409, "Checksum de l’image de référence invalide.")
        return stored_archive, actual
    source = Path(reference["source_path"]).resolve()
    cache = REFERENCE_ROOT / "deployment-cache"
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / f"{slugify_identifier(version_id)}.tar.gz"
    source_mtime = max((item.stat().st_mtime for item in source.rglob("*") if item.exists()), default=source.stat().st_mtime)
    if not archive.exists() or archive.stat().st_mtime < source_mtime:
        temporary = archive.with_suffix(".tmp")
        temporary.unlink(missing_ok=True)
        with tarfile.open(temporary, "w:gz", compresslevel=3) as tar:
            for item in source.iterdir():
                tar.add(item, arcname=item.name, recursive=True)
        os.replace(temporary, archive)
    digest = hashlib.sha256()
    with archive.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return archive, digest.hexdigest()


def sanitize_plex_clone(config_dir: Path) -> None:
    preferences = config_dir / "Library" / "Application Support" / "Plex Media Server" / "Preferences.xml"
    if preferences.exists():
        try:
            tree = ET.parse(preferences)
            root = tree.getroot()
            for key in (
                "MachineIdentifier", "ProcessedMachineIdentifier", "AnonymousMachineIdentifier",
                "PlexOnlineToken", "PlexOnlineUsername", "PlexOnlineMail", "PlexOnlineHome",
                "CertificateUUID", "PubSubServer", "PubSubServerRegion",
            ):
                root.attrib.pop(key, None)
            tree.write(preferences, encoding="utf-8", xml_declaration=True)
        except Exception as exc:
            raise RuntimeError(f"Impossible de personnaliser Preferences.xml : {exc}") from exc
    for relative in (
        "Library/Application Support/Plex Media Server/Cache",
        "Library/Application Support/Plex Media Server/Logs",
        "Library/Application Support/Plex Media Server/Crash Reports",
        "Library/Application Support/Plex Media Server/Codecs",
    ):
        shutil.rmtree(config_dir / relative, ignore_errors=True)
    for pid in config_dir.rglob("*.pid"):
        pid.unlink(missing_ok=True)


def choose_media_port(media_type: str, requested: str, port_mode: str, reserved: set[int]) -> int:
    candidates = PLEX_RANGE if media_type == "plex" else JELLYFIN_RANGE
    if port_mode == "manual":
        try:
            port = int(requested)
        except Exception:
            raise HTTPException(400, "Le port média manuel est invalide.")
        if port < 1024 or port > 65535:
            raise HTTPException(400, "Le port doit être compris entre 1024 et 65535.")
        if port in reserved or port_in_use(port):
            raise HTTPException(409, f"Le port {port} est déjà utilisé ou réservé.")
        return port
    return reserve_port(candidates, reserved)


def provision_snapshot(snapshot_id: str | None, media_type: str, appbox_dir: Path) -> None:
    if not snapshot_id:
        return
    snapshots = {item["snapshot_id"]: item for item in list_snapshots()}
    snapshot = snapshots.get(snapshot_id)
    if not snapshot or snapshot["media_type"] != media_type:
        raise HTTPException(400, "Snapshot incompatible ou introuvable.")
    source = Path(snapshot["source_path"] or "")
    if not source.exists():
        raise HTTPException(409, "Le snapshot sélectionné n’est pas disponible sur le Control Plane.")
    target = appbox_dir / ("plex-config" if media_type == "plex" else "jellyfin-config")
    shutil.copytree(source, target, dirs_exist_ok=True, symlinks=True)



def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.15)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def reserve_port(candidates: range, reserved: set[int]) -> int:
    for port in candidates:
        if port not in reserved and not port_in_use(port):
            return port
    raise RuntimeError("Aucun port libre dans la plage configurée.")


def compose_for(
    client_id: str,
    media_type: str,
    media_port: int,
    tautulli_port: int | None,
    mounts: list[dict[str, Any]] | None = None,
    acceleration_mode: str = "auto",
    target_node: str | None = None,
) -> str:
    safe_id = client_id.lower()
    compose_node = target_node or HOSTNAME
    short_id = safe_id.removeprefix("ab")
    mount_lines = compose_mount_lines(mounts or [])
    if mount_lines:
        mount_lines = "\n" + mount_lines
    device_lines = ""
    if acceleration_mode != "disabled":
        device_lines = "\n    devices:\n      - /dev/dri:/dev/dri"

    if media_type == "jellyfin":
        return f"""services:
  jellyfin:
    image: jellyfin/jellyfin:latest
    container_name: jellyfin-{safe_id}
    restart: unless-stopped
    user: "0:0"
    environment:
      TZ: Europe/Paris
    ports:
      - "{media_port}:8096"
{device_lines}
    volumes:
      - ./jellyfin-config:/config
      - ./jellyfin-cache:/cache{mount_lines}
    labels:
      io.portainer.accesscontrol.users: appbox{short_id}
      marinos.appbox.id: {safe_id}
      marinos.appbox.type: jellyfin
      marinos.appbox.node: {compose_node}

networks:
  default:
    name: appbox-shared
    external: true
"""
    tautulli = ""
    if tautulli_port:
        tautulli = f"""
  tautulli:
    image: tautulli/tautulli:latest
    container_name: tautulli-{safe_id}
    restart: unless-stopped
    environment:
      PUID: "0"
      PGID: "0"
      TZ: Europe/Paris
    ports:
      - "{tautulli_port}:8181"
    volumes:
      - ./tautulli-config:/config
    labels:
      io.portainer.accesscontrol.users: appbox{short_id}
      marinos.appbox.id: {safe_id}
      marinos.appbox.type: tautulli
      marinos.appbox.node: {compose_node}
"""
    return f"""services:
  plex:
    image: lscr.io/linuxserver/plex:latest
    container_name: plex-appb-{short_id}
    restart: unless-stopped
    environment:
      PUID: "0"
      PGID: "0"
      TZ: Europe/Paris
      VERSION: docker
    ports:
      - "{media_port}:32400"
{device_lines}
    tmpfs:
      - /transcode:size=8G
    volumes:
      - ./plex-config:/config{mount_lines}
    labels:
      io.portainer.accesscontrol.users: appbox{short_id}
      marinos.appbox.id: {safe_id}
      marinos.appbox.type: plex
      marinos.appbox.node: {compose_node}
{tautulli}

networks:
  default:
    name: appbox-shared
    external: true
"""


def run_command(command: list[str], cwd: Path | None = None, timeout: int = 300) -> tuple[bool, str]:
    try:
        result = subprocess.run(command, cwd=cwd, check=False, capture_output=True, text=True, timeout=timeout)
        output = (result.stdout + "\n" + result.stderr).strip()
        return result.returncode == 0, output[-16000:]
    except subprocess.TimeoutExpired:
        return False, f"Commande interrompue après {timeout} secondes."
    except Exception as exc:
        return False, f"Erreur : {exc}"


def run_compose(appbox_dir: Path, *args: str) -> tuple[bool, str]:
    return run_command(["docker", "compose", "-f", str(appbox_dir / "compose.yml"), *args], cwd=appbox_dir)


def docker_container_state(container: str) -> dict[str, Any]:
    ok, out = run_command(["docker", "inspect", container, "--format", "{{json .State}}"], timeout=15)
    if not ok:
        return {"exists": False, "status": "absent", "health": None, "restarts": 0}
    try:
        state = json.loads(out.strip().splitlines()[0])
        return {
            "exists": True,
            "status": state.get("Status", "unknown"),
            "health": (state.get("Health") or {}).get("Status"),
            "restarts": state.get("RestartCount", 0),
        }
    except Exception:
        return {"exists": True, "status": "unknown", "health": None, "restarts": 0}


def plex_identity(container: str) -> dict[str, Any]:
    ok, out = run_command([
        "docker", "exec", container, "sh", "-c",
        "curl -fsS http://127.0.0.1:32400/identity || wget -qO- http://127.0.0.1:32400/identity"
    ], timeout=15)
    if not ok or "<MediaContainer" not in out:
        return {
            "reachable": False, "claimed": None, "machine_id": None,
            "version": None, "username": None,
        }
    username = None
    pref_ok, pref_out = run_command([
        "docker", "exec", container, "sh", "-c",
        """python3 - <<'PY' 2>/dev/null || true
import glob, re
for path in glob.glob('/config/**/Preferences.xml', recursive=True):
    try:
        data=open(path, encoding='utf-8', errors='ignore').read()
    except Exception:
        continue
    match=re.search(r'PlexOnlineUsername="([^"]*)"', data)
    if match:
        print(match.group(1))
        break
PY"""
    ], timeout=15)
    if pref_ok and pref_out.strip():
        username = pref_out.strip().splitlines()[0][:200]
    try:
        start = out.find("<?xml") if "<?xml" in out else out.find("<MediaContainer")
        root = ET.fromstring(out[start:])
        return {
            "reachable": True,
            "claimed": root.attrib.get("claimed") == "1",
            "machine_id": root.attrib.get("machineIdentifier"),
            "version": root.attrib.get("version"),
            "username": username,
        }
    except Exception:
        return {
            "reachable": True, "claimed": None, "machine_id": None,
            "version": None, "username": username,
        }


def jellyfin_identity(container: str) -> dict[str, Any]:
    ok, out = run_command([
        "docker", "exec", container, "sh", "-c",
        "curl -fsS http://127.0.0.1:8096/System/Info/Public || wget -qO- http://127.0.0.1:8096/System/Info/Public"
    ], timeout=15)
    if not ok:
        return {"reachable": False, "version": None, "server_name": None}
    try:
        payload = json.loads(out)
        return {"reachable": True, "version": payload.get("Version"), "server_name": payload.get("ServerName")}
    except Exception:
        return {"reachable": True, "version": None, "server_name": None}


def image_status(container: str) -> dict[str, Any]:
    ok1, current = run_command(["docker", "inspect", container, "--format", "{{.Image}}"], timeout=15)
    ok2, image_ref = run_command(["docker", "inspect", container, "--format", "{{.Config.Image}}"], timeout=15)
    if not (ok1 and ok2):
        return {"state": "unknown", "label": "Indéterminé"}
    ok3, local = run_command(["docker", "image", "inspect", image_ref.strip(), "--format", "{{.Id}}"], timeout=15)
    if not ok3:
        return {"state": "unknown", "label": "Image locale introuvable"}
    if current.strip() == local.strip():
        return {"state": "current", "label": "À jour localement", "image": image_ref.strip()}
    return {"state": "outdated", "label": "Recréation disponible", "image": image_ref.strip()}



def _expected_host_ports(item: dict[str, Any], container_name: str) -> set[str]:
    expected: set[str] = set()
    names = item.get("containers") or []
    if names and container_name == names[0] and item.get("plex_port"):
        expected.add(str(item["plex_port"]))
    if len(names) > 1 and container_name == names[1] and item.get("tautulli_port"):
        expected.add(str(item["tautulli_port"]))
    return expected


def reconcile_node(node_id: str) -> dict[str, Any]:
    stamp = now_iso()
    changed = 0
    counts: dict[str, int] = {}
    with db_lock, db() as con:
        rows = con.execute("SELECT * FROM appboxes WHERE node_id=? AND status!='deleted' ORDER BY client_id", (node_id,)).fetchall()
        for row in rows:
            item = row_to_appbox(row)
            names = item.get("containers") or []
            records = {r["name"]: dict(r) for r in con.execute(
                "SELECT * FROM containers WHERE node_id=? AND name IN (%s)" % (",".join("?" for _ in names) or "''"),
                (node_id, *names),
            ).fetchall()} if names else {}
            drift: list[dict[str, Any]] = []
            states = []

            archived_at = row["archived_at"] if "archived_at" in row.keys() else None
            if archived_at:
                observed = "archived"
                result = "in_sync"
                drift_json = "[]"
                previous = (
                    row["observed_state"] if "observed_state" in row.keys() else None,
                    row["reconciliation_status"] if "reconciliation_status" in row.keys() else None,
                    row["drift_json"] if "drift_json" in row.keys() else None,
                )
                con.execute(
                    """UPDATE appboxes SET desired_state='stopped', observed_state=?,
                       reconciliation_status=?, drift_json=?, reconciled_at=?, updated_at=?
                       WHERE client_id=?""",
                    (observed, result, drift_json, stamp, stamp, item["client_id"]),
                )
                if previous != (observed, result, drift_json):
                    con.execute(
                        """INSERT INTO reconciliation_events(
                           client_id,node_id,desired_state,observed_state,result,drift_json,message,created_at
                           ) VALUES(?,?,?,?,?,?,?,?)""",
                        (item["client_id"], node_id, "stopped", observed, result, drift_json,
                         "AppBox archivée : runtime absent attendu, configuration conservée.", stamp),
                    )
                    changed += 1
                counts[result] = counts.get(result, 0) + 1
                continue

            for name in names:
                rec = records.get(name)
                if not rec:
                    drift.append({"type": "missing_container", "container": name})
                    continue
                states.append(str(rec.get("state") or "unknown"))
                expected_ports = _expected_host_ports(item, name)
                actual_ports = {str(p.get("host_port")) for p in json.loads(rec.get("ports_json") or "[]") if p.get("host_port")}
                if expected_ports and not expected_ports.issubset(actual_ports):
                    drift.append({"type": "port_drift", "container": name, "expected": sorted(expected_ports), "observed": sorted(actual_ports)})
            if not names or not records:
                observed = "missing"
            elif len(records) < len(names):
                observed = "partial"
            elif all(state == "running" for state in states):
                observed = "running"
            elif any(state == "running" for state in states):
                observed = "partial"
            elif states and all(state in {"exited", "created", "dead"} for state in states):
                observed = "stopped"
            else:
                observed = states[0] if states else "unknown"
            desired = str(item.get("desired_state") or "running")
            if drift and any(d.get("type") == "missing_container" for d in drift):
                result = "missing" if observed == "missing" else "drift"
            elif drift:
                result = "drift"
            elif desired == "running" and observed == "running":
                result = "in_sync"
            elif desired == "stopped" and observed == "stopped":
                result = "in_sync"
            else:
                result = "drift"
            previous = (row["observed_state"] if "observed_state" in row.keys() else None, row["reconciliation_status"] if "reconciliation_status" in row.keys() else None, row["drift_json"] if "drift_json" in row.keys() else None)
            drift_json = json.dumps(drift, ensure_ascii=False)
            con.execute("""UPDATE appboxes SET observed_state=?,reconciliation_status=?,drift_json=?,reconciled_at=?,updated_at=? WHERE client_id=?""",
                        (observed,result,drift_json,stamp,stamp,item["client_id"]))
            if previous != (observed,result,drift_json):
                changed += 1
                message = f"Desired={desired}, observed={observed}, result={result}"
                con.execute("""INSERT INTO reconciliation_events(client_id,node_id,desired_state,observed_state,result,drift_json,message,created_at) VALUES(?,?,?,?,?,?,?,?)""",
                            (item["client_id"],node_id,desired,observed,result,drift_json,message,stamp))
            counts[result] = counts.get(result, 0) + 1
        orphan_count = con.execute("""SELECT COUNT(*) FROM containers c LEFT JOIN appboxes a ON a.client_id=c.appbox_id WHERE c.node_id=? AND (c.appbox_id IS NULL OR a.client_id IS NULL)""", (node_id,)).fetchone()[0]
    return {"node_id": node_id, "changed": changed, "counts": counts, "orphans": orphan_count, "reconciled_at": stamp}


def reconciliation_snapshot() -> dict[str, Any]:
    with db_lock, db() as con:
        appboxes = [dict(r) for r in con.execute("""SELECT client_id,node_id,desired_state,observed_state,reconciliation_status,drift_json,reconciled_at FROM appboxes WHERE status!='deleted' ORDER BY node_id,client_id""").fetchall()]
        orphans = [dict(r) for r in con.execute("""SELECT c.node_id,c.name,c.image,c.state,c.health,c.last_seen FROM containers c LEFT JOIN appboxes a ON a.client_id=c.appbox_id WHERE c.appbox_id IS NULL OR a.client_id IS NULL ORDER BY c.node_id,c.name""").fetchall()]
    for item in appboxes:
        item["drift"] = json.loads(item.pop("drift_json") or "[]")
    return {"appboxes": appboxes, "orphans": orphans, "generated_at": now_iso()}


def remote_container_record(node_id: str, container_name: str) -> dict[str, Any] | None:
    with db_lock, db() as con:
        row = con.execute(
            "SELECT * FROM containers WHERE node_id=? AND name=? ORDER BY last_seen DESC LIMIT 1",
            (node_id, container_name),
        ).fetchone()
    if not row:
        return None
    result = dict(row)
    result["ports"] = json.loads(result.get("ports_json") or "[]")
    result["labels"] = json.loads(result.get("labels_json") or "{}")
    result["mounts"] = json.loads(result.get("mounts_json") or "[]")
    result["networks"] = json.loads(result.get("networks_json") or "[]")
    result["service"] = result["labels"].pop("_marinos_service", {})
    return result


def container_runtime(node_id: str, container_name: str) -> dict[str, Any]:
    if node_id == HOSTNAME:
        return docker_container_state(container_name)
    record = remote_container_record(node_id, container_name)
    if not record:
        return {"exists": False, "status": "absent", "health": None, "restarts": 0}
    return {
        "exists": True,
        "status": record.get("state") or record.get("status") or "unknown",
        "health": record.get("health"),
        "restarts": int(record.get("restart_count") or 0),
        "image": record.get("image"),
        "image_id": record.get("image_id"),
        "ports": record.get("ports") or [],
        "last_seen": record.get("last_seen"),
    }


def remote_service_identity(node_id: str, container_name: str, kind: str) -> dict[str, Any]:
    record = remote_container_record(node_id, container_name)
    service = (record or {}).get("service") or {}
    if service.get("kind") == kind:
        return service
    if kind == "plex":
        return {"reachable": False, "claimed": None, "version": None, "username": None}
    return {"reachable": False, "version": None, "server_name": None}


def runtime_image_status(node_id: str, container_name: str, runtime: dict[str, Any]) -> dict[str, Any]:
    if not runtime.get("exists"):
        return {"state": "unknown", "label": "Non déployée"}
    if node_id == HOSTNAME:
        return image_status(container_name)
    image = runtime.get("image")
    return {"state": "current", "label": "Image détectée par l’agent", "image": image}


def enrich_item(item: dict[str, Any]) -> dict[str, Any]:
    result = dict(item)
    container = item.get("containers", [""])[0]
    node_id = str(item.get("node_id") or HOSTNAME)
    result["runtime"] = container_runtime(node_id, container)
    if item.get("type") == "jellyfin":
        if result["runtime"]["exists"]:
            result["jellyfin"] = jellyfin_identity(container) if node_id == HOSTNAME else remote_service_identity(node_id, container, "jellyfin")
        else:
            result["jellyfin"] = {"reachable": False, "version": None, "server_name": None}
        result["plex"] = {"reachable": False, "claimed": None, "version": None}
    else:
        if result["runtime"]["exists"]:
            result["plex"] = plex_identity(container) if node_id == HOSTNAME else remote_service_identity(node_id, container, "plex")
        else:
            result["plex"] = {"reachable": False, "claimed": None, "version": None, "username": None}
        discovered_username = result["plex"].get("username")
        if discovered_username and discovered_username != item.get("plex_username"):
            with db_lock, db() as con:
                con.execute("UPDATE appboxes SET plex_username=?,updated_at=? WHERE client_id=?", (discovered_username, now_iso(), item["client_id"]))
            result["plex_username"] = discovered_username
        else:
            result["plex_username"] = item.get("plex_username")
        result["jellyfin"] = {"reachable": False, "version": None, "server_name": None}
    result["image_info"] = runtime_image_status(node_id, container, result["runtime"])
    return result


def appbox_status_payload(item: dict[str, Any]) -> dict[str, Any]:
    enriched = enrich_item(item)
    tautulli = None
    if enriched.get("with_tautulli") and len(enriched.get("containers", [])) > 1:
        tautulli = container_runtime(str(enriched.get("node_id") or HOSTNAME), enriched["containers"][1])
    runtime = enriched["runtime"]
    service = enriched["jellyfin"] if enriched.get("type") == "jellyfin" else enriched["plex"]
    if not runtime.get("exists"):
        lifecycle = "generated"
    elif runtime.get("status") != "running":
        lifecycle = runtime.get("status") or "unknown"
    elif service.get("reachable") is not True:
        lifecycle = "starting"
    elif enriched.get("type") == "jellyfin":
        lifecycle = "online"
    elif service.get("claimed") is True:
        lifecycle = "claimed"
    else:
        lifecycle = "unclaimed"
    return {
        "client_id": enriched["client_id"],
        "media_type": enriched.get("type", "plex"),
        "lifecycle": lifecycle,
        "runtime": runtime,
        "plex": enriched["plex"],
        "jellyfin": enriched["jellyfin"],
        "tautulli": tautulli,
        "image_info": enriched["image_info"],
        "claim_available": bool(enriched.get("type") == "plex" and runtime.get("exists") and enriched["plex"].get("claimed") is not True),
        "updated_at": now_iso(),
    }


def workflow_definition(action: str) -> list[tuple[str, str]]:
    return WORKFLOW_DEFINITIONS.get(action, [
        ("validate", "Validation"),
        ("execute", "Exécution"),
        ("notification", "Notification de fin"),
    ])


def create_job(client_id: str, action: str, title: str, detail: str = "", node_id: str | None = None, options: dict[str, Any] | None = None) -> str:
    job_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    stamp = now_iso()
    steps = workflow_definition(action)
    with db_lock, db() as con:
        con.execute("""
            INSERT INTO jobs(
                job_id,client_id,node_id,action,title,status,progress,
                detail,created_at,updated_at,options_json
            ) VALUES(?,?,?,?,?,'queued',0,?,?,?,?)
        """, (job_id, client_id, (node_id or HOSTNAME), action, title, detail[-12000:], stamp, stamp, json.dumps(options or {}, ensure_ascii=False)))
        con.executemany("""
            INSERT INTO job_steps(
                job_id,step_key,title,status,progress,detail,executor,resources_json
            ) VALUES(?,?,?,'pending',0,'','control-plane','{}')
        """, [(job_id, key, step_title) for key, step_title in steps])
    worker_wakeup.set()
    return job_id


def update_job(job_id: str, status: str | None = None, progress: int | None = None, detail: str | None = None) -> None:
    with db_lock, db() as con:
        row = con.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if not row:
            return
        stamp = now_iso()
        values = {
            "status": status if status is not None else row["status"],
            "progress": max(0, min(100, int(progress))) if progress is not None else row["progress"],
            "detail": detail[-12000:] if detail is not None else row["detail"],
            "updated_at": stamp,
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
        }
        if status == "running" and not values["started_at"]:
            values["started_at"] = stamp
        if status in {"success", "error", "cancelled"}:
            values["finished_at"] = stamp
        con.execute("""
            UPDATE jobs SET status=:status,progress=:progress,detail=:detail,
                updated_at=:updated_at,started_at=:started_at,finished_at=:finished_at
            WHERE job_id=:job_id
        """, {**values, "job_id": job_id})


def step_rows(job_id: str) -> list[dict[str, Any]]:
    with db() as con:
        rows = con.execute("""
            SELECT step_id,job_id,step_key,title,status,progress,detail,
                   executor,resources_json,started_at,finished_at,
                   CASE
                     WHEN started_at IS NOT NULL AND finished_at IS NOT NULL
                     THEN ROUND((julianday(finished_at)-julianday(started_at))*86400, 3)
                     ELSE NULL
                   END AS duration_seconds
            FROM job_steps WHERE job_id=? ORDER BY step_id
        """, (job_id,)).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        try:
            item["resources"] = json.loads(item.pop("resources_json") or "{}")
        except Exception:
            item["resources"] = {}
            item.pop("resources_json", None)
        result.append(item)
    return result


def workflow_statistics(steps: list[dict[str, Any]]) -> dict[str, Any]:
    groups = {
        "validation": {"duration": 0.0, "steps": 0},
        "docker": {"duration": 0.0, "steps": 0},
        "healthcheck": {"duration": 0.0, "steps": 0},
        "integration": {"duration": 0.0, "steps": 0},
    }
    for step in steps:
        key = step.get("step_key", "")
        duration = float(step.get("duration_seconds") or 0)
        if key.startswith("validate") or key in {"verify_stopped"}:
            group = "validation"
        elif key.startswith("docker") or key in {"cleanup_files"}:
            group = "docker"
        elif key == "healthcheck":
            group = "healthcheck"
        else:
            group = "integration"
        groups[group]["duration"] = round(groups[group]["duration"] + duration, 3)
        groups[group]["steps"] += 1

    completed = [step for step in steps if step.get("status") in {"success", "warning", "failed", "skipped"}]
    return {
        "total_steps": len(steps),
        "completed_steps": len(completed),
        "success_steps": sum(step.get("status") == "success" for step in steps),
        "failed_steps": sum(step.get("status") == "failed" for step in steps),
        "skipped_steps": sum(step.get("status") == "skipped" for step in steps),
        "groups": groups,
    }


def update_step(
    job_id: str,
    step_key: str,
    status: str,
    detail: str = "",
    progress: int | None = None,
    executor: str | None = None,
    resources: dict[str, Any] | None = None,
) -> None:
    with db_lock, db() as con:
        row = con.execute("""
            SELECT * FROM job_steps WHERE job_id=? AND step_key=?
        """, (job_id, step_key)).fetchone()
        if not row:
            return
        stamp = now_iso()
        started_at = row["started_at"]
        finished_at = row["finished_at"]
        if status == "running" and not started_at:
            started_at = stamp
        if status in {"success", "failed", "warning", "skipped"}:
            if not started_at:
                started_at = stamp
            finished_at = stamp
        step_progress = progress
        if step_progress is None:
            step_progress = 100 if status in {"success", "warning", "skipped"} else 0
        con.execute("""
            UPDATE job_steps
            SET status=?,progress=?,detail=?,executor=?,
                resources_json=?,started_at=?,finished_at=?
            WHERE job_id=? AND step_key=?
        """, (
            status,
            max(0, min(100, int(step_progress))),
            detail[-16000:],
            executor or row["executor"] or "control-plane",
            json.dumps(resources if resources is not None else json.loads(row["resources_json"] or "{}"), ensure_ascii=False),
            started_at,
            finished_at,
            job_id,
            step_key,
        ))
    recalculate_job_progress(job_id)


def recalculate_job_progress(job_id: str) -> None:
    with db_lock, db() as con:
        rows = con.execute("""
            SELECT status,progress FROM job_steps WHERE job_id=? ORDER BY step_id
        """, (job_id,)).fetchall()
        if not rows:
            return
        values = []
        for row in rows:
            if row["status"] in {"success", "warning", "skipped"}:
                values.append(100)
            elif row["status"] == "failed":
                values.append(100)
            elif row["status"] == "running":
                values.append(max(10, int(row["progress"] or 10)))
            else:
                values.append(0)
        progress = round(sum(values) / len(values))
        con.execute("UPDATE jobs SET progress=?,updated_at=? WHERE job_id=?",
                    (progress, now_iso(), job_id))


def finish_remaining_steps(job_id: str, status: str = "skipped", detail: str = "") -> None:
    with db_lock, db() as con:
        stamp = now_iso()
        con.execute("""
            UPDATE job_steps
            SET status=?,progress=100,detail=?,
                started_at=COALESCE(started_at,?),
                finished_at=COALESCE(finished_at,?)
            WHERE job_id=? AND status IN ('pending','running')
        """, (status, detail[-16000:], stamp, stamp, job_id))
    recalculate_job_progress(job_id)


def fail_workflow(job_id: str, step_key: str, message: str) -> None:
    update_step(job_id, step_key, "failed", message, 100)
    finish_remaining_steps(job_id, "skipped", "Étape non exécutée après l’échec du workflow.")
    update_job(job_id, "error", 100, message)


def run_workflow_step(
    job_id: str,
    step_key: str,
    callback,
    running_detail: str,
) -> tuple[bool, str]:
    update_step(job_id, step_key, "running", running_detail, 20)
    try:
        ok, detail = callback()
    except Exception as exc:
        ok, detail = False, f"Exception : {exc}"
    update_step(job_id, step_key, "success" if ok else "failed", detail, 100)
    return ok, detail


def job_dict(row: sqlite3.Row, include_steps: bool = False) -> dict[str, Any]:
    result = dict(row)
    if result.get("started_at") and result.get("finished_at"):
        try:
            start = datetime.fromisoformat(result["started_at"])
            finish = datetime.fromisoformat(result["finished_at"])
            result["duration_seconds"] = round((finish - start).total_seconds(), 3)
        except Exception:
            result["duration_seconds"] = None
    else:
        result["duration_seconds"] = None
    if include_steps:
        result["steps"] = step_rows(result["job_id"])
        result["statistics"] = workflow_statistics(result["steps"])
    return result


def latest_jobs_for(client_id: str, limit: int = 10) -> list[dict[str, Any]]:
    with db() as con:
        rows = con.execute("""
            SELECT * FROM jobs WHERE client_id=?
            ORDER BY created_at DESC LIMIT ?
        """, (client_id, limit)).fetchall()
    return [job_dict(row) for row in rows]


def active_job_for(client_id: str) -> dict[str, Any] | None:
    with db() as con:
        row = con.execute("""
            SELECT * FROM jobs
            WHERE client_id=? AND status IN ('queued','running')
            ORDER BY created_at LIMIT 1
        """, (client_id,)).fetchone()
    return job_dict(row) if row else None


def queued_jobs(limit: int = 50) -> list[dict[str, Any]]:
    with db() as con:
        rows = con.execute("""
            SELECT * FROM jobs WHERE status='queued'
            ORDER BY created_at LIMIT ?
        """, (limit,)).fetchall()
    return [job_dict(row) for row in rows]


def wait_agent_command(command_id: str, timeout: int = 300) -> tuple[bool, dict[str, Any], str]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with db() as con:
            row = con.execute(
                "SELECT status,result_json,error_text FROM agent_commands WHERE command_id=?",
                (command_id,),
            ).fetchone()
        if not row:
            return False, {}, "Commande agent introuvable."
        if row["status"] == "success":
            return True, json.loads(row["result_json"] or "{}"), ""
        if row["status"] == "failed":
            return False, json.loads(row["result_json"] or "{}"), row["error_text"] or "Échec distant."
        time.sleep(1)
    return False, {}, f"Délai d’attente agent dépassé ({timeout} s)."


def finalize_appbox_deletion(client_id: str, job_id: str, purge: bool = False) -> bool:
    """Atomically remove an AppBox after runtime verification.

    The operation is idempotent: an already removed AppBox is considered a
    successful final state. Jobs and audit history are retained and detached.
    """
    stamp = now_iso()
    with db_lock, immediate_transaction() as con:
        exists = con.execute(
            "SELECT 1 FROM appboxes WHERE client_id=?", (client_id,)
        ).fetchone()
        if not exists:
            return False

        # Detach historical rows first. These records must survive deletion.
        con.execute("UPDATE jobs SET client_id=NULL WHERE client_id=?", (client_id,))
        con.execute("UPDATE events SET client_id=NULL WHERE client_id=?", (client_id,))
        con.execute("UPDATE notifications_queue SET client_id=NULL WHERE client_id=?", (client_id,))
        con.execute("UPDATE placement_decisions SET client_id=NULL WHERE client_id=?", (client_id,))
        con.execute("UPDATE control_plane_deployments SET client_id=NULL WHERE client_id=?", (client_id,))
        con.execute(
            "UPDATE port_reservations SET client_id=NULL,status='released',released_at=? WHERE client_id=?",
            (stamp, client_id),
        )

        # Remove active inventory rows. DELETE is naturally idempotent.
        for table, column in (
            ("appbox_mounts", "client_id"),
            ("snapshot_deployments", "client_id"),
            ("reconciliation_events", "client_id"),
            ("containers", "appbox_id"),
            ("networks", "appbox_id"),
            ("volumes", "appbox_id"),
        ):
            con.execute(f"DELETE FROM {table} WHERE {column}=?", (client_id,))

        deleted = con.execute(
            "DELETE FROM appboxes WHERE client_id=?", (client_id,)
        ).rowcount
        if deleted != 1:
            raise RuntimeError(
                f"Commit BDD impossible : suppression inattendue ({deleted} ligne)."
            )

        violations = con.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            formatted = "; ".join(
                f"table={row[0]} rowid={row[1]} parent={row[2]} fk={row[3]}"
                for row in violations[:10]
            )
            raise RuntimeError(f"Intégrité SQLite invalide après suppression : {formatted}")
    return True


def execute_remote_job(job: dict[str, Any], item: dict[str, Any]) -> None:
    job_id, client_id, action = job["job_id"], job["client_id"], job["action"]
    job_options = json.loads(job.get("options_json") or "{}")
    deletion_mode = str(job_options.get("deletion_mode") or "delete").lower()
    node_id = item["node_id"]
    appbox_dir = Path(item["path"])
    compose_path = appbox_dir / "compose.yml"
    # Le Compose d'une AppBox distante appartient au node d'exécution.
    # Le Control Plane peut en transmettre un pour un déploiement/recreate,
    # mais start/stop/restart ne doivent jamais dépendre de sa présence locale.
    control_plane_compose = compose_path.read_text(encoding="utf-8") if compose_path.exists() else ""

    nodes = {node["node_id"]: node for node in list_control_nodes()}
    node = nodes.get(node_id)
    if not node or not node.get("agent_online"):
        fail_workflow(job_id, "validate_node", f"Agent du node {node_id} hors ligne.")
        return
    if not node.get("capabilities", {}).get("deployment_executor"):
        fail_workflow(job_id, "validate_node", f"Agent {node_id} sans capacité deployment_executor.")
        return
    if node.get("maintenance") and action in {"deploy", "start", "restart", "recreate"}:
        fail_workflow(job_id, "validate_node", f"Node {node_id} en maintenance.")
        return

    first_validation = "validate_appbox" if action in {"stop", "delete"} else "validate_node"
    update_step(job_id, first_validation, "success", f"Agent {node_id} en ligne et exécuteur disponible.", 100, executor=f"agent-{node_id}")
    directories: list[str] = []
    if action in {"deploy", "recreate"}:
        if item.get("type") == "jellyfin":
            directories.extend(["jellyfin-config", "jellyfin-cache"])
        else:
            directories.append("plex-config")
            if item.get("with_tautulli"):
                directories.append("tautulli-config")
    env_content = deployment_env_for(item) if action in {"deploy", "recreate"} else ""
    manifest = build_deployment_manifest(item, control_plane_compose, env_content) if action in {"deploy", "recreate"} else None
    reference_archive = None
    if action in {"deploy", "recreate"} and item.get("reference_version_id"):
        archive, archive_checksum = reference_deployment_archive(item["reference_version_id"])
        reference_archive = {
            "version_id": item["reference_version_id"],
            "download_path": f"/api/agent/v1/{node_id}/reference-deployments/{item['reference_version_id']}/archive",
            "sha256": archive_checksum,
            "size_bytes": archive.stat().st_size,
            "target_directory": "plex-config" if item.get("type") == "plex" else "jellyfin-config",
        }
    payload = {
        "client_id": client_id,
        "action": action,
        "compose": control_plane_compose if action in {"deploy", "recreate"} else "",
        "env": env_content,
        "manifest": manifest,
        "directories": directories,
        "containers": item.get("containers") or [],
        "deletion_mode": deletion_mode,
        "reference_archive": reference_archive,
    }
    command_id = queue_agent_command(node_id, "appbox_action", payload)
    docker_step = {
        "deploy": "docker_deploy", "start": "docker_start", "restart": "docker_restart", "recreate": "docker_recreate",
        "stop": "docker_stop", "delete": "docker_remove",
    }.get(action, "docker_deploy")
    if action in {"deploy", "recreate"}:
        update_step(job_id, "validate_storage", "success", "Validation déléguée à l’agent distant.", 100, executor=f"agent-{node_id}")
        compose_detail = (
            f"Manifeste {manifest['checksum'][:12]} vérifié et Compose/.env transférés à l’agent."
            if control_plane_compose and manifest else
            "Utilisation du Compose déjà présent sur le node, avec repli direct sur les conteneurs existants."
        )
        update_step(job_id, "validate_compose", "success", compose_detail, 100, executor=f"agent-{node_id}")
    elif action == "start":
        update_step(job_id, "validate_compose", "success", "Compose et conteneurs existants validés par l’agent distant.", 100, executor=f"agent-{node_id}")
    update_step(job_id, docker_step, "running", f"Commande {command_id[:8]} envoyée à {node_id}.", 20, executor=f"agent-{node_id}")
    ok, result, error = wait_agent_command(command_id, timeout=360)
    detail = result.get("output") or error or "Commande distante terminée."
    if not ok:
        update_step(job_id, docker_step, "failed", detail, 100, executor=f"agent-{node_id}", resources=result)
        fail_workflow(job_id, docker_step, detail)
        save_appbox_status(client_id, "error", detail)
        return
    update_step(job_id, docker_step, "success", detail, 100, executor=f"agent-{node_id}", resources=result)
    if action in {"deploy", "start", "restart", "recreate"}:
        update_step(job_id, "healthcheck", "success", result.get("state", "Services vérifiés par l’agent."), 100, executor=f"agent-{node_id}")
        if action == "deploy":
            update_step(job_id, "refresh", "skipped", "Intégration refresh ciblé prévue.", 100)
            update_step(job_id, "watchdog", "skipped", "Intégration watchdog prévue.", 100)
        save_appbox_status(client_id, "running", detail)
    elif action == "stop":
        update_step(job_id, "verify_stopped", "success", result.get("state", "Services arrêtés."), 100, executor=f"agent-{node_id}")
        save_appbox_status(client_id, "stopped", detail)
    elif action == "delete":
        if deletion_mode == "archive":
            update_step(job_id, "cleanup_files", "success", "Configuration et données conservées sur le node.", 100, executor=f"agent-{node_id}")
            with db_lock, db() as con:
                con.execute("UPDATE appboxes SET status='archived', desired_state='stopped', archived_at=?, last_message=?, updated_at=? WHERE client_id=?",
                            (now_iso(), detail[-3000:], now_iso(), client_id))
            inventory_message = "AppBox archivée."
        else:
            path_exists = bool(result.get("path_exists", True))
            containers_remaining = result.get("containers_remaining") or []
            if path_exists or containers_remaining:
                verify_detail = f"Vérification refusée : path_exists={path_exists}, containers_remaining={containers_remaining}"
                update_step(job_id, "cleanup_files", "failed", verify_detail, 100, executor=f"agent-{node_id}", resources=result)
                fail_workflow(job_id, "cleanup_files", verify_detail)
                save_appbox_status(client_id, "error", verify_detail)
                record_audit("DELETE_APPBOX", client_id, node_id, deletion_mode, "FAILED", verify_detail)
                return
            update_step(job_id, "cleanup_files", "success", "Conteneurs et dossier AppBox absents, commit autorisé.", 100, executor=f"agent-{node_id}", resources=result)
            finalize_appbox_deletion(client_id, job_id, purge=(deletion_mode == "purge"))
            inventory_message = "AppBox supprimée de l’inventaire actif."
        update_step(job_id, "inventory", "success", inventory_message, 100, executor="control-plane")
        record_audit("DELETE_APPBOX", client_id, node_id, deletion_mode, "SUCCESS", detail)
        update_step(job_id, "audit", "success", "Opération enregistrée dans le journal d’audit.", 100, executor="control-plane")
    update_step(job_id, "notification", "success", "Résultat distant enregistré.", 100)
    update_job(job_id, "success", 100, detail)
    record_event(client_id, f"{action}_success", detail, "success")


def execute_job(job: dict[str, Any]) -> None:
    job_id = job["job_id"]
    client_id = job["client_id"]
    action = job["action"]
    job_options = json.loads(job.get("options_json") or "{}")
    deletion_mode = str(job_options.get("deletion_mode") or "delete").lower()
    item = get_appbox(client_id)

    update_job(job_id, "running", 0, f"Workflow {action} démarré.")
    record_event(client_id, f"{action}_start", f"Début du workflow {action}.", "progress")

    if not item:
        first_step = workflow_definition(action)[0][0]
        fail_workflow(job_id, first_step, "AppBox introuvable.")
        return

    if item.get("node_id") and item["node_id"] != HOSTNAME:
        execute_remote_job(job, item)
        return

    appbox_dir = Path(item["path"])
    output_parts: list[str] = []

    def validate_node() -> tuple[bool, str]:
        if not Path("/var/run/docker.sock").exists():
            return False, "Socket Docker indisponible."
        with db() as con:
            node = con.execute("SELECT maintenance,status FROM nodes WHERE node_id=?", (HOSTNAME,)).fetchone()
        if not node or node["status"] != "online":
            return False, "Node local indisponible."
        if node["maintenance"] and action in {"deploy", "start", "recreate"}:
            return False, "Node en mode maintenance : opération bloquée."
        ok, version = run_command(["docker", "version", "--format", "{{.Server.Version}}"], timeout=15)
        return ok, f"Docker opérationnel — version {version.strip() if ok else 'inconnue'}"

    def validate_storage() -> tuple[bool, str]:
        checks = {
            "RDAD": Path("/mnt/decypharr-poc/.mnt").exists(),
            "GPU": Path("/dev/dri").exists(),
            "Dossier AppBox": appbox_dir.exists(),
        }
        failed = [name for name, state in checks.items() if not state]
        detail = " · ".join(f"{name}={'OK' if state else 'KO'}" for name, state in checks.items())
        return not failed, detail

    def validate_compose() -> tuple[bool, str]:
        compose_path = appbox_dir / "compose.yml"
        if not compose_path.exists():
            return False, f"Compose absent : {compose_path}"
        ok, output = run_compose(appbox_dir, "config", "--quiet")
        return ok, output or "Compose valide."

    def healthcheck_running() -> tuple[bool, str]:
        deadline = time.monotonic() + 90
        last = ""
        while time.monotonic() < deadline:
            states = [docker_container_state(name) for name in item["containers"]]
            last = " · ".join(
                f"{name}={state.get('status','unknown')}"
                for name, state in zip(item["containers"], states)
            )
            if states and all(state.get("status") == "running" for state in states):
                return True, f"Services opérationnels : {last}"
            time.sleep(3)
        return False, f"Healthcheck expiré : {last}"

    def verify_stopped() -> tuple[bool, str]:
        states = [docker_container_state(name) for name in item["containers"]]
        active = [
            name for name, state in zip(item["containers"], states)
            if state.get("status") == "running"
        ]
        return not active, "Tous les services sont arrêtés." if not active else f"Encore actifs : {', '.join(active)}"

    try:
        if action in {"deploy", "start"}:
            for key, callback, detail in [
                ("validate_node", validate_node, "Contrôle du node et de Docker."),
                ("validate_storage", validate_storage, "Contrôle RDAD, GPU et stockage."),
                ("validate_compose", validate_compose, "Validation syntaxique du Compose."),
            ]:
                ok, msg = run_workflow_step(job_id, key, callback, detail)
                output_parts.append(msg)
                if not ok:
                    fail_workflow(job_id, key, msg)
                    return

            ok, msg = run_workflow_step(
                job_id, "docker_deploy" if action == "deploy" else "docker_start",
                lambda: run_compose(appbox_dir, "up", "-d"),
                "Exécution de docker compose up -d.",
            )
            output_parts.append(msg)
            if not ok:
                fail_workflow(job_id, "docker_deploy" if action == "deploy" else "docker_start", msg)
                return

            ok, msg = run_workflow_step(job_id, "healthcheck", healthcheck_running, "Attente des conteneurs.")
            output_parts.append(msg)
            if not ok:
                fail_workflow(job_id, "healthcheck", msg)
                save_appbox_status(client_id, "error", msg)
                return

            if action == "deploy":
                update_step(job_id, "refresh", "skipped", "Intégration refresh ciblé prévue dans la phase dédiée.", 100)
                update_step(job_id, "watchdog", "skipped", "Intégration watchdog prévue dans la phase dédiée.", 100)
            docker_resources = {
                "containers": item["containers"],
                "compose_path": str(appbox_dir / "compose.yml"),
                "appbox_path": str(appbox_dir),
            }
            update_step(
                job_id,
                "docker_deploy" if action == "deploy" else "docker_start",
                "success",
                output_parts[-2] if len(output_parts) >= 2 else "Services Docker démarrés.",
                100,
                executor=f"embedded-{HOSTNAME}",
                resources=docker_resources,
            )
            update_step(job_id, "notification", "success", "Événement interne enregistré.", 100)
            save_appbox_status(client_id, "running", "\n".join(output_parts))

        elif action == "restart":
            ok, msg = run_workflow_step(job_id, "validate_node", validate_node, "Contrôle du node et de Docker.")
            if not ok:
                fail_workflow(job_id, "validate_node", msg)
                return
            ok, msg = run_workflow_step(job_id, "docker_restart", lambda: run_compose(appbox_dir, "restart"), "Redémarrage des conteneurs.")
            output_parts.append(msg)
            if not ok:
                fail_workflow(job_id, "docker_restart", msg)
                return
            ok, msg = run_workflow_step(job_id, "healthcheck", healthcheck_running, "Attente des conteneurs.")
            output_parts.append(msg)
            if not ok:
                fail_workflow(job_id, "healthcheck", msg)
                save_appbox_status(client_id, "error", msg)
                return
            update_step(job_id, "notification", "success", "Événement interne enregistré.", 100)
            save_appbox_status(client_id, "running", "\n".join(output_parts))

        elif action == "stop":
            ok, msg = run_workflow_step(job_id, "validate_appbox", lambda: (appbox_dir.exists(), f"Dossier : {appbox_dir}"), "Validation de l’AppBox.")
            if not ok:
                fail_workflow(job_id, "validate_appbox", msg)
                return
            ok, msg = run_workflow_step(job_id, "docker_stop", lambda: run_compose(appbox_dir, "stop"), "Arrêt des conteneurs.")
            output_parts.append(msg)
            if not ok:
                fail_workflow(job_id, "docker_stop", msg)
                return
            ok, msg = run_workflow_step(job_id, "verify_stopped", verify_stopped, "Vérification de l’état Docker.")
            if not ok:
                fail_workflow(job_id, "verify_stopped", msg)
                return
            update_step(job_id, "notification", "success", "Événement interne enregistré.", 100)
            save_appbox_status(client_id, "stopped", "\n".join(output_parts))

        elif action == "recreate":
            for key, callback, detail in [
                ("validate_node", validate_node, "Contrôle du node et de Docker."),
                ("validate_storage", validate_storage, "Contrôle RDAD, GPU et stockage."),
                ("validate_compose", validate_compose, "Validation syntaxique du Compose."),
            ]:
                ok, msg = run_workflow_step(job_id, key, callback, detail)
                if not ok:
                    fail_workflow(job_id, key, msg)
                    return
            ok, pull = run_workflow_step(job_id, "docker_pull", lambda: run_compose(appbox_dir, "pull"), "Téléchargement des images.")
            output_parts.append(pull)
            if not ok:
                fail_workflow(job_id, "docker_pull", pull)
                return
            ok, deploy = run_workflow_step(
                job_id, "docker_recreate",
                lambda: run_compose(appbox_dir, "up", "-d", "--force-recreate"),
                "Recréation des conteneurs.",
            )
            output_parts.append(deploy)
            if not ok:
                fail_workflow(job_id, "docker_recreate", deploy)
                return
            ok, msg = run_workflow_step(job_id, "healthcheck", healthcheck_running, "Attente des conteneurs.")
            if not ok:
                fail_workflow(job_id, "healthcheck", msg)
                return
            update_step(
                job_id,
                "docker_recreate",
                "success",
                output_parts[-1] if output_parts else "Conteneurs recréés.",
                100,
                executor=f"embedded-{HOSTNAME}",
                resources={
                    "containers": item["containers"],
                    "compose_path": str(appbox_dir / "compose.yml"),
                    "appbox_path": str(appbox_dir),
                },
            )
            update_step(job_id, "notification", "success", "Événement interne enregistré.", 100)
            save_appbox_status(client_id, "running", "\n".join(output_parts))

        elif action == "delete":
            ok, msg = run_workflow_step(job_id, "validate_appbox", lambda: (appbox_dir.exists(), f"Dossier : {appbox_dir}"), "Validation avant suppression.")
            if not ok:
                fail_workflow(job_id, "validate_appbox", msg)
                return
            ok, down = run_workflow_step(job_id, "docker_remove", lambda: run_compose(appbox_dir, "down", "--remove-orphans"), "Suppression des ressources Docker.")
            output_parts.append(down)
            if not ok:
                fail_workflow(job_id, "docker_remove", down)
                record_audit("DELETE_APPBOX", client_id, item.get("node_id"), deletion_mode, "FAILED", down)
                return

            def cleanup() -> tuple[bool, str]:
                if deletion_mode == "archive":
                    return True, f"Configuration conservée : {appbox_dir}"
                if appbox_dir.exists():
                    shutil.rmtree(appbox_dir)
                return True, f"Dossier supprimé : {appbox_dir}"

            ok, msg = run_workflow_step(job_id, "cleanup_files", cleanup, "Traitement des données persistantes.")
            if not ok:
                fail_workflow(job_id, "cleanup_files", msg)
                record_audit("DELETE_APPBOX", client_id, item.get("node_id"), deletion_mode, "FAILED", msg)
                return

            if deletion_mode == "archive":
                with db_lock, db() as con:
                    con.execute("UPDATE appboxes SET status='archived',desired_state='stopped',archived_at=?,last_message=?,updated_at=? WHERE client_id=?",
                                (now_iso(), msg, now_iso(), client_id))
                inventory_message = "AppBox archivée."
            else:
                remaining = [name for name in (item.get("containers") or []) if run_command(["docker", "inspect", name], timeout=20)[0]]
                if appbox_dir.exists() or remaining:
                    verify_detail = f"Suppression incomplète : dossier={appbox_dir.exists()}, conteneurs={remaining}"
                    fail_workflow(job_id, "cleanup_files", verify_detail)
                    save_appbox_status(client_id, "error", verify_detail)
                    record_audit("DELETE_APPBOX", client_id, item.get("node_id"), deletion_mode, "FAILED", verify_detail)
                    return
                finalize_appbox_deletion(client_id, job_id, purge=(deletion_mode == "purge"))
                inventory_message = "AppBox supprimée de l’inventaire actif."
            update_step(job_id, "inventory", "success", inventory_message, 100)
            record_audit("DELETE_APPBOX", client_id, item.get("node_id"), deletion_mode, "SUCCESS", msg)
            update_step(job_id, "audit", "success", "Opération enregistrée dans le journal d’audit.", 100)
            update_step(job_id, "notification", "success", "Événement interne enregistré.", 100)

        else:
            first_step = workflow_definition(action)[0][0]
            fail_workflow(job_id, first_step, f"Action inconnue : {action}")
            return

        update_job(job_id, "success", 100, "\n".join(output_parts) or "Workflow terminé.")
        record_event(None if action == "delete" else client_id, f"{action}_done", "Workflow terminé avec succès.", "success")
    except Exception as exc:
        current_steps = step_rows(job_id)
        running = next((step for step in current_steps if step["status"] == "running"), None)
        key = running["step_key"] if running else workflow_definition(action)[0][0]
        fail_workflow(job_id, key, f"Erreur non gérée : {exc}")
        record_event(client_id, f"{action}_error", str(exc), "error")


def _force_fail_job(job_id: str, message: str, client_id: str | None = None, action: str | None = None) -> None:
    """Best-effort terminal transition that must not raise to the worker."""
    try:
        steps = step_rows(job_id)
        running = next((step for step in steps if step["status"] == "running"), None)
        pending = next((step for step in steps if step["status"] in {"pending", "queued"}), None)
        key = (running or pending or {"step_key": "workflow"})["step_key"]
        if key != "workflow":
            update_step(job_id, key, "failed", message, 100)
        finish_remaining_steps(job_id, "skipped", "Étape non exécutée après l’échec du workflow.")
        update_job(job_id, "error", 100, message)
        record_event(client_id, f"{action or 'workflow'}_error", message, "error")
        if action == "delete":
            record_audit("DELETE_APPBOX", client_id, None, None, "FAILED", message)
    except Exception as recovery_exc:
        print(f"[worker] impossible de finaliser le job {job_id}: {recovery_exc}", flush=True)


def queue_worker() -> None:
    while not worker_stop.is_set():
        row = None
        changed = 0
        try:
            with db_lock, db() as con:
                row = con.execute("""
                    SELECT * FROM jobs WHERE status='queued'
                    ORDER BY created_at LIMIT 1
                """).fetchone()
                if row:
                    stamp = now_iso()
                    changed = con.execute("""
                        UPDATE jobs SET status='running', started_at=?, updated_at=?
                        WHERE job_id=? AND status='queued'
                    """, (stamp, stamp, row["job_id"])).rowcount
            if row and changed:
                job = dict(row)
                try:
                    execute_job(job)
                except BaseException as exc:
                    message = f"Exception worker non gérée : {type(exc).__name__}: {exc}"
                    print(f"[worker] {message}", flush=True)
                    _force_fail_job(job["job_id"], message, job.get("client_id"), job.get("action"))
                continue
        except BaseException as exc:
            print(f"[worker] erreur de boucle: {type(exc).__name__}: {exc}", flush=True)
            if row and changed:
                job = dict(row)
                _force_fail_job(job["job_id"], f"Erreur de boucle worker : {exc}", job.get("client_id"), job.get("action"))
            time.sleep(2)
        worker_wakeup.wait(2)
        worker_wakeup.clear()


def recover_interrupted_jobs() -> int:
    """Close jobs left running by a previous Control Plane process."""
    with db_lock, db() as con:
        rows = con.execute(
            "SELECT job_id,client_id,action FROM jobs WHERE status='running'"
        ).fetchall()
    for row in rows:
        _force_fail_job(
            row["job_id"],
            "Workflow interrompu par un redémarrage du Control Plane. Relance manuelle sûre requise.",
            row["client_id"],
            row["action"],
        )
    return len(rows)


def job_watchdog_loop() -> None:
    while not worker_stop.wait(JOB_WATCHDOG_INTERVAL):
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=JOB_TIMEOUT_SECONDS)).isoformat()
        try:
            with db_lock, db() as con:
                rows = con.execute("""
                    SELECT job_id,client_id,action,updated_at FROM jobs
                    WHERE status='running' AND COALESCE(updated_at,started_at,created_at) < ?
                """, (cutoff,)).fetchall()
            for row in rows:
                _force_fail_job(
                    row["job_id"],
                    f"Watchdog : workflow bloqué depuis plus de {JOB_TIMEOUT_SECONDS} secondes.",
                    row["client_id"],
                    row["action"],
                )
        except Exception as exc:
            print(f"[watchdog] erreur: {exc}", flush=True)


def docker_counts() -> tuple[int, int]:
    ok, out = run_command(["docker", "ps", "-a", "--format", "{{.State}}"], timeout=10)
    if not ok:
        return 0, 0
    states = [line.strip() for line in out.splitlines() if line.strip()]
    return len(states), sum(1 for state in states if state == "running")


def get_disk_path() -> str:
    for candidate in (str(BASE_DIR), "/mnt/decypharr-poc", "/"):
        if Path(candidate).exists():
            return candidate
    return "/"


def collect_metrics_loop() -> None:
    previous_disk = psutil.disk_io_counters()
    previous_net = psutil.net_io_counters()
    previous_time = time.monotonic()
    psutil.cpu_percent(interval=None)

    while not worker_stop.wait(METRICS_INTERVAL):
        try:
            current_time = time.monotonic()
            elapsed = max(0.1, current_time - previous_time)
            cpu = psutil.cpu_percent(interval=None)
            vm = psutil.virtual_memory()
            load = os.getloadavg()[0]
            usage = shutil.disk_usage(get_disk_path())
            disk_now = psutil.disk_io_counters()
            net_now = psutil.net_io_counters()
            read_bps = ((disk_now.read_bytes - previous_disk.read_bytes) / elapsed) if disk_now and previous_disk else 0
            write_bps = ((disk_now.write_bytes - previous_disk.write_bytes) / elapsed) if disk_now and previous_disk else 0
            rx_bps = ((net_now.bytes_recv - previous_net.bytes_recv) / elapsed) if net_now and previous_net else 0
            tx_bps = ((net_now.bytes_sent - previous_net.bytes_sent) / elapsed) if net_now and previous_net else 0
            total_containers, running_containers = docker_counts()
            rdad_ok = int(Path("/mnt/decypharr-poc/.mnt").exists())
            gpu_ok = int(Path("/dev/dri").exists())
            stamp = now_iso()

            with db_lock, db() as con:
                con.execute("""
                    INSERT INTO node_metrics(
                        node_id,collected_at,cpu_percent,load_1,ram_percent,
                        ram_used,ram_total,disk_percent,disk_free,
                        disk_read_bps,disk_write_bps,net_rx_bps,net_tx_bps,
                        docker_containers,running_containers
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    HOSTNAME, stamp, cpu, load, vm.percent, vm.used, vm.total,
                    (usage.used / usage.total * 100) if usage.total else 0,
                    usage.free, read_bps, write_bps, rx_bps, tx_bps,
                    total_containers, running_containers,
                ))
                con.execute("""
                    UPDATE nodes SET status='online',rdad_ok=?,gpu_ok=?,
                        last_seen=?,updated_at=? WHERE node_id=?
                """, (rdad_ok, gpu_ok, stamp, stamp, HOSTNAME))
                cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
                con.execute("DELETE FROM node_metrics WHERE collected_at < ?", (cutoff,))

            previous_disk, previous_net, previous_time = disk_now, net_now, current_time
        except Exception as exc:
            record_event(None, "metrics_error", f"Erreur collecte métriques : {exc}", "error")


def latest_node_metric() -> dict[str, Any]:
    with db() as con:
        row = con.execute("""
            SELECT * FROM node_metrics WHERE node_id=?
            ORDER BY metric_id DESC LIMIT 1
        """, (HOSTNAME,)).fetchone()
    return dict(row) if row else {
        "cpu_percent": 0, "load_1": 0, "ram_percent": 0,
        "ram_used": 0, "ram_total": 0, "disk_percent": 0,
        "disk_free": 0, "disk_read_bps": 0, "disk_write_bps": 0,
        "net_rx_bps": 0, "net_tx_bps": 0,
        "docker_containers": 0, "running_containers": 0,
        "collected_at": None,
    }


def node_payload() -> dict[str, Any]:
    with db() as con:
        node = con.execute("SELECT * FROM nodes WHERE node_id=?", (HOSTNAME,)).fetchone()
        appbox_count = con.execute(
            "SELECT COUNT(*) FROM appboxes WHERE node_id=? AND status!='deleted'",
            (HOSTNAME,),
        ).fetchone()[0]
        queued = con.execute("SELECT COUNT(*) FROM jobs WHERE node_id=? AND status='queued'", (HOSTNAME,)).fetchone()[0]
        running_jobs = con.execute("SELECT COUNT(*) FROM jobs WHERE node_id=? AND status='running'", (HOSTNAME,)).fetchone()[0]
    result = dict(node) if node else {}
    result["metrics"] = latest_node_metric()
    result["appbox_count"] = appbox_count
    result["queued_jobs"] = queued
    result["running_jobs"] = running_jobs
    return result


def ensure_claim_variable(compose_path: Path) -> None:
    content = compose_path.read_text(encoding="utf-8")
    if "PLEX_CLAIM:" not in content:
        content = content.replace('      VERSION: docker\n', '      VERSION: docker\n      PLEX_CLAIM: "${PLEX_CLAIM:-}"\n', 1)
        compose_path.write_text(content, encoding="utf-8")


def remove_claim_variable(compose_path: Path) -> None:
    content = compose_path.read_text(encoding="utf-8")
    content = content.replace('      PLEX_CLAIM: "${PLEX_CLAIM:-}"\n', "")
    compose_path.write_text(content, encoding="utf-8")


@app.on_event("startup")
def startup() -> None:
    print(f"[startup] Marinos AppBox Manager {VERSION}", flush=True)
    init_database()
    print("[startup] SQLite migrations OK", flush=True)
    migrate_json_data()
    try:
        sync_business_inventory()
    except Exception as exc:
        record_event(None, "inventory_startup_error", str(exc), "warning")
    recovered = recover_interrupted_jobs()
    if recovered:
        print(f"[startup] {recovered} workflow(s) interrompu(s) finalisé(s) en erreur", flush=True)
    Thread(target=queue_worker, name="appbox-queue-worker", daemon=True).start()
    Thread(target=job_watchdog_loop, name="appbox-job-watchdog", daemon=True).start()
    Thread(target=collect_metrics_loop, name="appbox-metrics", daemon=True).start()
    worker_wakeup.set()


@app.on_event("shutdown")
def shutdown() -> None:
    worker_stop.set()
    worker_wakeup.set()


@app.get("/", response_class=HTMLResponse)
def command_center(request: Request):
    appboxes = [enrich_item(item) for item in list_appboxes()]
    running = sum(1 for item in appboxes if item["runtime"]["status"] == "running")
    claimed = sum(1 for item in appboxes if item["plex"].get("claimed") is True)
    stopped = sum(1 for item in appboxes if item["runtime"]["exists"] and item["runtime"]["status"] != "running")
    recent_jobs = []
    recent_events = []
    with db() as con:
        recent_jobs = [dict(row) for row in con.execute("""
            SELECT * FROM jobs ORDER BY created_at DESC LIMIT 8
        """).fetchall()]
        recent_events = [dict(row) for row in con.execute("""
            SELECT event_id,client_id,node_id,event_type,level,message,created_at
            FROM events ORDER BY event_id DESC LIMIT 8
        """).fetchall()]
    return templates.TemplateResponse(request, "command_center.html", {
        "appboxes": appboxes,
        "mode": APPBOX_MODE,
        "hostname": HOSTNAME,
        "rdad_ok": Path("/mnt/decypharr-poc/.mnt").exists(),
        "running": running,
        "claimed": claimed,
        "stopped": stopped,
        "node": node_payload(),
        "queue": queued_jobs(10),
        "recent_jobs": recent_jobs,
        "recent_events": recent_events,
        "active_page": "dashboard",
    })


@app.get("/appboxes", response_class=HTMLResponse)
def appboxes_page(request: Request):
    appboxes = [enrich_item(item) for item in list_appboxes()]
    return templates.TemplateResponse(request, "appboxes.html", {
        "appboxes": appboxes,
        "profiles": list_profiles(),
        "deployment_images": deployment_images(),
        "mount_groups": list_mount_groups(),
        "snapshots": list_snapshots(),
        "reference_versions": list_reference_versions(),
        "nodes": list_control_nodes(),
        "placement": placement_config(),
        "mode": APPBOX_MODE,
        "hostname": HOSTNAME,
        "active_page": "appboxes",
    })


@app.get("/downloads/appbox-agent-latest.zip")
def download_agent_archive():
    archive = AGENT_ASSET_DIR / "appbox-agent-latest.zip"
    if not archive.is_file():
        raise HTTPException(503, "Archive agent indisponible.")
    return FileResponse(
        archive,
        media_type="application/zip",
        filename=f"marinos-appbox-agent-v{VERSION}.zip",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/downloads/install-agent.sh")
def download_agent_bootstrap():
    installer = AGENT_ASSET_DIR / "bootstrap-install-agent.sh"
    if not installer.is_file():
        raise HTTPException(503, "Installateur agent indisponible.")
    return FileResponse(
        installer,
        media_type="text/x-shellscript; charset=utf-8",
        filename="install-agent.sh",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/nodes", response_class=HTMLResponse)
def nodes_page(request: Request):
    return templates.TemplateResponse(request, "nodes.html", {
        "mode": APPBOX_MODE,
        "hostname": HOSTNAME,
        "nodes": list_control_nodes(),
        "tags": list_node_tags(),
        "placement": placement_config(),
        "active_page": "nodes",
    })


@app.post("/nodes")
def register_node(
    node_id: str = Form(...),
    name: str = Form(...),
    status: str = Form("offline"),
    tag_ids: list[str] = Form([]),
):
    node_id = slugify_identifier(node_id)
    if not node_id or status not in {"online", "offline", "planned"}:
        raise HTTPException(400, "Définition du node invalide.")
    stamp = now_iso()
    valid_tags = {tag["tag_id"] for tag in list_node_tags()}
    selected_tags = sorted(set(tag_ids) & valid_tags)
    with db_lock, db() as con:
        con.execute("""
            INSERT INTO nodes(
                node_id,name,mode,status,maintenance,docker_version,
                agent_version,rdad_ok,gpu_ok,last_seen,created_at,updated_at
            ) VALUES(?,?, 'remote', ?,0,NULL,'not-installed',0,0,NULL,?,?)
            ON CONFLICT(node_id) DO UPDATE SET
                name=excluded.name,status=excluded.status,updated_at=excluded.updated_at
        """, (node_id, name.strip(), status, stamp, stamp))
        con.execute("""
            INSERT OR IGNORE INTO node_agents(
                node_id,status,capabilities_json,updated_at
            ) VALUES(?,'not_installed','{}',?)
        """, (node_id, stamp))
        con.execute("DELETE FROM node_tag_assignments WHERE node_id=?", (node_id,))
        con.executemany("""
            INSERT INTO node_tag_assignments(node_id,tag_id,assigned_at)
            VALUES(?,?,?)
        """, [(node_id, tag_id, stamp) for tag_id in selected_tags])
    record_event(None, "node_registered", f"Node {name} enregistré.", "success")
    return RedirectResponse("/nodes", status_code=303)


@app.post("/nodes/{node_id}/edit")
def edit_node(
    node_id: str,
    name: str = Form(...),
    status: str = Form(...),
    tag_ids: list[str] = Form([]),
    maintenance: bool = Form(False),
):
    nodes = {node["node_id"]: node for node in list_control_nodes()}
    if node_id not in nodes:
        raise HTTPException(404, "Node introuvable.")
    if status not in {"online", "offline", "planned"}:
        raise HTTPException(400, "État du node invalide.")
    valid_tags = {tag["tag_id"] for tag in list_node_tags()}
    selected_tags = sorted(set(tag_ids) & valid_tags)
    stamp = now_iso()
    with db_lock, db() as con:
        con.execute("""
            UPDATE nodes
            SET name=?,status=?,maintenance=?,updated_at=?
            WHERE node_id=?
        """, (
            name.strip() or node_id.upper(),
            status,
            int(maintenance),
            stamp,
            node_id,
        ))
        con.execute(
            "DELETE FROM node_tag_assignments WHERE node_id=?",
            (node_id,),
        )
        con.executemany("""
            INSERT INTO node_tag_assignments(node_id,tag_id,assigned_at)
            VALUES(?,?,?)
        """, [
            (node_id, tag_id, stamp)
            for tag_id in selected_tags
        ])
    record_event(
        None,
        "node_updated",
        f"Node {node_id} modifié.",
        "success",
    )
    return RedirectResponse("/nodes", status_code=303)


@app.post("/nodes/{node_id}/delete")
def delete_node(node_id: str):
    if node_id == HOSTNAME:
        raise HTTPException(
            409,
            "Le node local du Control Plane ne peut pas être supprimé.",
        )
    with db_lock, db() as con:
        node = con.execute(
            "SELECT * FROM nodes WHERE node_id=?",
            (node_id,),
        ).fetchone()
        if not node:
            raise HTTPException(404, "Node introuvable.")
        appbox_count = con.execute("""
            SELECT COUNT(*) FROM appboxes
            WHERE node_id=? AND status!='deleted'
        """, (node_id,)).fetchone()[0]
        if appbox_count:
            raise HTTPException(
                409,
                f"Suppression impossible : {appbox_count} AppBox active(s) sur ce node.",
            )
        con.execute("DELETE FROM nodes WHERE node_id=?", (node_id,))
    record_event(
        None,
        "node_deleted",
        f"Node {node_id} supprimé.",
        "warning",
    )
    return RedirectResponse("/nodes", status_code=303)


@app.post("/nodes/{node_id}/tags")
def update_node_tags(
    node_id: str,
    tag_ids: list[str] = Form([]),
    maintenance: bool = Form(False),
):
    nodes = {node["node_id"]: node for node in list_control_nodes()}
    if node_id not in nodes:
        raise HTTPException(404, "Node introuvable.")
    valid_tags = {tag["tag_id"] for tag in list_node_tags()}
    selected_tags = sorted(set(tag_ids) & valid_tags)
    stamp = now_iso()
    with db_lock, db() as con:
        con.execute("DELETE FROM node_tag_assignments WHERE node_id=?", (node_id,))
        con.executemany("""
            INSERT INTO node_tag_assignments(node_id,tag_id,assigned_at)
            VALUES(?,?,?)
        """, [(node_id, tag_id, stamp) for tag_id in selected_tags])
        con.execute("""
            UPDATE nodes SET maintenance=?,updated_at=? WHERE node_id=?
        """, (int(maintenance), stamp, node_id))
    record_event(None, "node_tags_updated", f"Tags du node {node_id} modifiés.", "success")
    return RedirectResponse("/nodes", status_code=303)


@app.post("/settings/placement")
def update_placement_settings(
    default_mode: str = Form(...),
    automatic_required_tag: str = Form("appbox-node"),
    automatic_excluded_tag: str = Form("bare-metal"),
    allow_manual_bare_metal: bool = Form(False),
    require_confirmation_bare_metal: bool = Form(False),
):
    if default_mode not in {"manual", "automatic", "ask"}:
        raise HTTPException(400, "Mode de placement invalide.")
    tags = {tag["tag_id"] for tag in list_node_tags()}
    if automatic_required_tag not in tags or automatic_excluded_tag not in tags:
        raise HTTPException(400, "Tags de placement invalides.")
    with db_lock, db() as con:
        con.execute("""
            UPDATE placement_settings
            SET default_mode=?,automatic_required_tag=?,automatic_excluded_tag=?,
                allow_manual_bare_metal=?,require_confirmation_bare_metal=?,
                updated_at=?
            WHERE setting_id='global'
        """, (
            default_mode,
            automatic_required_tag,
            automatic_excluded_tag,
            int(allow_manual_bare_metal),
            int(require_confirmation_bare_metal),
            now_iso(),
        ))
    record_event(None, "placement_settings_updated", "Politique de placement modifiée.", "success")
    return RedirectResponse("/settings", status_code=303)


@app.post("/nodes/{node_id}/agent-token-json")
def generate_agent_token_json(request: Request, node_id: str, label: str = Form("installation")):
    nodes = {node["node_id"]: node for node in list_control_nodes()}
    if node_id not in nodes:
        raise HTTPException(404, "Node introuvable.")
    token_id, raw_token = create_agent_token(node_id, label)
    base_url = str(request.base_url).rstrip("/")
    return JSONResponse({
        "token_id": token_id,
        "node_id": node_id,
        "token": raw_token,
        "control_plane_url": base_url,
        "installer_url": f"{base_url}/downloads/install-agent.sh",
        "archive_url": f"{base_url}/downloads/appbox-agent-latest.zip",
        "warning": "Ce jeton ne sera plus réaffiché.",
    })


@app.post("/nodes/{node_id}/agent-command")
def create_agent_command(
    node_id: str,
    command_type: str = Form(...),
):
    allowed = {"ping", "inventory"}
    if command_type not in allowed:
        raise HTTPException(400, "Commande agent non autorisée.")
    nodes = {node["node_id"]: node for node in list_control_nodes()}
    if node_id not in nodes:
        raise HTTPException(404, "Node introuvable.")
    command_id = queue_agent_command(node_id, command_type)
    record_event(
        None,
        "agent_command_queued",
        f"Commande {command_type} envoyée à {node_id}.",
        "success",
    )
    return RedirectResponse("/agents", status_code=303)


@app.post("/api/agent/v1/{node_id}/heartbeat")
async def agent_heartbeat(node_id: str, request: Request):
    authenticate_agent(request, node_id)
    payload = await request.json()
    stamp = now_iso()
    capabilities = payload.get("capabilities") or {}
    metrics = payload.get("metrics") or {}
    agent_version = str(payload.get("agent_version") or "unknown")
    endpoint = str(payload.get("endpoint") or "")
    with db_lock, db() as con:
        node = con.execute(
            "SELECT node_id FROM nodes WHERE node_id=?",
            (node_id,),
        ).fetchone()
        if not node:
            raise HTTPException(404, "Node non enregistré.")
        con.execute("""
            INSERT INTO node_agents(
                node_id,agent_id,agent_version,status,endpoint,last_heartbeat,
                capabilities_json,registered_at,updated_at
            ) VALUES(?,?,?,'online',?,?,?, ?,?)
            ON CONFLICT(node_id) DO UPDATE SET
                agent_id=excluded.agent_id,
                agent_version=excluded.agent_version,
                status='online',
                endpoint=excluded.endpoint,
                last_heartbeat=excluded.last_heartbeat,
                capabilities_json=excluded.capabilities_json,
                updated_at=excluded.updated_at
        """, (
            node_id,
            str(payload.get("agent_id") or f"agent-{node_id}"),
            agent_version,
            endpoint,
            stamp,
            json.dumps(capabilities, ensure_ascii=False),
            stamp,
            stamp,
        ))
        con.execute("""
            INSERT INTO agent_node_metrics(
                node_id,hostname,os_name,kernel_version,docker_version,
                compose_version,cpu_model,cpu_count,load_1,
                memory_total_bytes,memory_available_bytes,
                disk_total_bytes,disk_free_bytes,temperature_c,
                gpu_present,rdad_present,docker_ok,collected_at,payload_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(node_id) DO UPDATE SET
                hostname=excluded.hostname,
                os_name=excluded.os_name,
                kernel_version=excluded.kernel_version,
                docker_version=excluded.docker_version,
                compose_version=excluded.compose_version,
                cpu_model=excluded.cpu_model,
                cpu_count=excluded.cpu_count,
                load_1=excluded.load_1,
                memory_total_bytes=excluded.memory_total_bytes,
                memory_available_bytes=excluded.memory_available_bytes,
                disk_total_bytes=excluded.disk_total_bytes,
                disk_free_bytes=excluded.disk_free_bytes,
                temperature_c=excluded.temperature_c,
                gpu_present=excluded.gpu_present,
                rdad_present=excluded.rdad_present,
                docker_ok=excluded.docker_ok,
                collected_at=excluded.collected_at,
                payload_json=excluded.payload_json
        """, (
            node_id,
            metrics.get("hostname"),
            metrics.get("os_name"),
            metrics.get("kernel_version"),
            metrics.get("docker_version"),
            metrics.get("compose_version"),
            metrics.get("cpu_model"),
            metrics.get("cpu_count"),
            metrics.get("load_1"),
            metrics.get("memory_total_bytes"),
            metrics.get("memory_available_bytes"),
            metrics.get("disk_total_bytes"),
            metrics.get("disk_free_bytes"),
            metrics.get("temperature_c"),
            int(bool(metrics.get("gpu_present"))),
            int(bool(metrics.get("rdad_present"))),
            int(bool(metrics.get("docker_ok"))),
            stamp,
            json.dumps(metrics, ensure_ascii=False),
        ))
        memory_total = int(metrics.get("memory_total_bytes") or 0)
        memory_available = int(metrics.get("memory_available_bytes") or 0)
        disk_total = int(metrics.get("disk_total_bytes") or 0)
        disk_free = int(metrics.get("disk_free_bytes") or 0)
        con.execute("""
            INSERT INTO node_metrics(
                node_id,collected_at,cpu_percent,load_1,ram_percent,ram_used,ram_total,
                disk_percent,disk_free,disk_read_bps,disk_write_bps,net_rx_bps,net_tx_bps,
                docker_containers,running_containers
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            node_id,stamp,float(metrics.get("cpu_percent") or 0),float(metrics.get("load_1") or 0),
            ((memory_total-memory_available)/memory_total*100) if memory_total else 0,
            max(0,memory_total-memory_available),memory_total,
            ((disk_total-disk_free)/disk_total*100) if disk_total else 0,disk_free,
            float(metrics.get("disk_read_bps") or 0),float(metrics.get("disk_write_bps") or 0),
            float(metrics.get("net_rx_bps") or 0),float(metrics.get("net_tx_bps") or 0),
            int(metrics.get("docker_containers") or 0),int(metrics.get("running_containers") or 0),
        ))
        con.execute("""
            UPDATE nodes
            SET status='online',
                docker_version=?,
                agent_version=?,
                rdad_ok=?,
                gpu_ok=?,
                last_seen=?,
                updated_at=?
            WHERE node_id=?
        """, (
            metrics.get("docker_version"),
            agent_version,
            int(bool(metrics.get("rdad_present"))),
            int(bool(metrics.get("gpu_present"))),
            stamp,
            stamp,
            node_id,
        ))
    return JSONResponse({
        "status": "ok",
        "server_version": VERSION,
        "heartbeat_interval": 60,
    })



@app.post("/api/agent/v1/{node_id}/inventory")
async def agent_inventory(node_id: str, request: Request):
    authenticate_agent(request, node_id)
    payload = await request.json()
    containers = payload.get("containers") or []
    if not isinstance(containers, list):
        raise HTTPException(400, "Inventaire de conteneurs invalide.")
    stamp = now_iso()
    seen: set[str] = set()
    with db_lock, db() as con:
        for item in containers:
            container_id = str(item.get("container_id") or "").strip()
            name = str(item.get("name") or "").strip()
            if not container_id or not name:
                continue
            seen.add(container_id)
            labels = dict(item.get("labels") or {})
            labels["_marinos_service"] = item.get("service") or {}
            appbox_id = appbox_id_for_resource(name, labels)
            con.execute("""
                INSERT INTO containers(
                    container_id,node_id,appbox_id,name,image,image_id,state,status,health,
                    restart_count,ports_json,labels_json,mounts_json,networks_json,
                    created_at,last_seen,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(container_id) DO UPDATE SET
                    node_id=excluded.node_id,appbox_id=excluded.appbox_id,name=excluded.name,
                    image=excluded.image,image_id=excluded.image_id,state=excluded.state,
                    status=excluded.status,health=excluded.health,restart_count=excluded.restart_count,
                    ports_json=excluded.ports_json,labels_json=excluded.labels_json,
                    mounts_json=excluded.mounts_json,networks_json=excluded.networks_json,
                    created_at=excluded.created_at,last_seen=excluded.last_seen,updated_at=excluded.updated_at
            """, (
                container_id,node_id,appbox_id,name,item.get("image"),item.get("image_id"),
                item.get("state") or "unknown",item.get("status"),item.get("health"),
                int(item.get("restart_count") or 0),
                json.dumps(item.get("ports") or [], ensure_ascii=False),
                json.dumps(labels, ensure_ascii=False),
                json.dumps(item.get("mounts") or [], ensure_ascii=False),
                json.dumps(item.get("networks") or [], ensure_ascii=False),
                item.get("created_at"),stamp,stamp,
            ))
        if seen:
            placeholders = ",".join("?" for _ in seen)
            con.execute(f"DELETE FROM containers WHERE node_id=? AND container_id NOT IN ({placeholders})", (node_id, *seen))
        else:
            con.execute("DELETE FROM containers WHERE node_id=?", (node_id,))
    reconciliation = reconcile_node(node_id)
    return JSONResponse({"status": "ok", "node_id": node_id, "containers": len(seen), "collected_at": stamp, "reconciliation": reconciliation})


@app.get("/api/reconciliation")
def api_reconciliation():
    return JSONResponse(reconciliation_snapshot())


@app.post("/api/reconciliation/{node_id}/run")
def api_reconciliation_run(node_id: str):
    return JSONResponse(reconcile_node(node_id))


@app.get("/api/agent/v1/{node_id}/commands")
def agent_poll_commands(node_id: str, request: Request):
    authenticate_agent(request, node_id)
    with db_lock, db() as con:
        row = con.execute("""
            SELECT * FROM agent_commands
            WHERE node_id=? AND status='queued'
            ORDER BY created_at LIMIT 1
        """, (node_id,)).fetchone()
        if not row:
            return JSONResponse({"command": None})
        con.execute("""
            UPDATE agent_commands
            SET status='claimed',claimed_at=?
            WHERE command_id=? AND status='queued'
        """, (now_iso(), row["command_id"]))
    command = dict(row)
    command["payload"] = json.loads(command.pop("payload_json") or "{}")
    return JSONResponse({"command": command})


@app.post("/api/agent/v1/{node_id}/commands/{command_id}/result")
async def agent_command_result(node_id: str, command_id: str, request: Request):
    authenticate_agent(request, node_id)
    payload = await request.json()
    status = payload.get("status")
    if status not in {"success", "failed"}:
        raise HTTPException(400, "Statut de commande invalide.")
    with db_lock, db() as con:
        command = con.execute("""
            SELECT * FROM agent_commands
            WHERE command_id=? AND node_id=?
        """, (command_id, node_id)).fetchone()
        if not command:
            raise HTTPException(404, "Commande introuvable.")
        con.execute("""
            UPDATE agent_commands
            SET status=?,completed_at=?,result_json=?,error_text=?
            WHERE command_id=?
        """, (
            status,
            now_iso(),
            json.dumps(payload.get("result") or {}, ensure_ascii=False),
            payload.get("error"),
            command_id,
        ))
    finalize_reference_discovery_command(command, status, payload.get("result") or {}, payload.get("error"))
    finalize_reference_build_command(command, status, payload.get("result") or {}, payload.get("error"))
    record_event(
        None,
        "agent_command_completed",
        f"Commande {command_id[:8]} sur {node_id} : {status}.",
        "success" if status == "success" else "error",
    )
    return JSONResponse({"status": "ok"})


@app.get("/agents", response_class=HTMLResponse)
def agents_page(request: Request):
    with db() as con:
        commands = [dict(row) for row in con.execute("""
            SELECT c.*,n.name AS node_name
            FROM agent_commands c
            LEFT JOIN nodes n ON n.node_id=c.node_id
            ORDER BY c.created_at DESC LIMIT 100
        """).fetchall()]
    return templates.TemplateResponse(request, "agents.html", {
        "mode": APPBOX_MODE,
        "hostname": HOSTNAME,
        "nodes": list_control_nodes(),
        "commands": commands,
        "active_page": "agents",
    })


@app.get("/distribution", response_class=HTMLResponse)
def distribution_page(request: Request):
    return templates.TemplateResponse(request, "distribution.html", {
        "mode": APPBOX_MODE,
        "hostname": HOSTNAME,
        "matrix": distribution_matrix(),
        "nodes": list_control_nodes(),
        "active_page": "distribution",
    })


@app.get("/deployments", response_class=HTMLResponse)
def deployments_page(request: Request):
    with db() as con:
        deployments = [dict(row) for row in con.execute("""
            SELECT d.*,n.name AS node_name,a.media_type
            FROM control_plane_deployments d
            LEFT JOIN nodes n ON n.node_id=d.node_id
            LEFT JOIN appboxes a ON a.client_id=d.client_id
            ORDER BY d.created_at DESC LIMIT 250
        """).fetchall()]
        decisions = [dict(row) for row in con.execute("""
            SELECT p.*,n.name AS selected_node_name
            FROM placement_decisions p
            LEFT JOIN nodes n ON n.node_id=p.selected_node_id
            ORDER BY p.created_at DESC LIMIT 100
        """).fetchall()]
    return templates.TemplateResponse(request, "deployments.html", {
        "mode": APPBOX_MODE,
        "hostname": HOSTNAME,
        "deployments": deployments,
        "decisions": decisions,
        "active_page": "deployments",
    })


@app.get("/api/placement/preview")
def api_placement_preview(
    mode: str = "manual",
    node_id: str | None = None,
    bare_metal_override: bool = False,
):
    result = evaluate_placement(
        mode,
        node_id,
        allow_bare_metal_override=bare_metal_override,
    )
    return JSONResponse({
        "selected_node_id": result["selected"]["node_id"],
        "selected_node_name": result["selected"]["name"],
        "eligible": [node["node_id"] for node in result["eligible"]],
        "rejected": result["rejected"],
        "reason": result["reason"],
    })


@app.get("/inventory", response_class=HTMLResponse)
def inventory_page(request: Request):
    snapshot = inventory_snapshot()
    return templates.TemplateResponse(request, "inventory.html", {
        "mode": APPBOX_MODE,
        "hostname": HOSTNAME,
        "active_page": "inventory",
        "inventory": snapshot,
    })


@app.get("/api/inventory")
def api_inventory():
    return JSONResponse(inventory_snapshot())


@app.post("/api/inventory/sync")
def api_inventory_sync():
    return JSONResponse(sync_business_inventory())


@app.get("/jobs", response_class=HTMLResponse)
def jobs_page(request: Request):
    with db() as con:
        jobs = [dict(row) for row in con.execute("""
            SELECT * FROM jobs ORDER BY created_at DESC LIMIT 200
        """).fetchall()]
    return templates.TemplateResponse(request, "jobs.html", {
        "mode": APPBOX_MODE,
        "hostname": HOSTNAME,
        "jobs": jobs,
        "active_page": "jobs",
    })


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_detail_page(request: Request, job_id: str):
    with db() as con:
        row = con.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Job introuvable.")
    return templates.TemplateResponse(request, "job_detail.html", {
        "mode": APPBOX_MODE,
        "hostname": HOSTNAME,
        "active_page": "jobs",
        "job": job_dict(row, include_steps=True),
    })


@app.get("/notifications", response_class=HTMLResponse)
def notifications_page(request: Request):
    with db() as con:
        events = [dict(row) for row in con.execute("""
            SELECT event_id,client_id,node_id,event_type,level,message,created_at
            FROM events ORDER BY event_id DESC LIMIT 200
        """).fetchall()]
    return templates.TemplateResponse(request, "notifications.html", {
        "mode": APPBOX_MODE,
        "hostname": HOSTNAME,
        "events": events,
        "active_page": "notifications",
    })


def reference_builders() -> list[dict[str, Any]]:
    with db() as con:
        rows = con.execute("""
            SELECT * FROM reference_builder_registry
            WHERE enabled=1 ORDER BY application,display_name
        """).fetchall()
    return [dict(row) for row in rows]


def reference_capable_nodes(application: str = "plex") -> list[dict[str, Any]]:
    result = []
    for node in list_control_nodes():
        caps = node.get("capabilities") or {}
        builders = caps.get("reference_builders") or []
        declared = application in builders
        result.append({
            "node_id": node["node_id"],
            "name": node.get("name") or node["node_id"],
            "status": node.get("status"),
            "agent_status": node.get("agent_status"),
            "builder_available": declared,
            "builder_foundation": bool(caps.get("reference_builder_foundation", False)),
            "discovery_available": bool(caps.get("reference_discovery", False)),
        })
    return result


def list_reference_builds(limit: int = 50) -> list[dict[str, Any]]:
    with db() as con:
        rows = con.execute("""
            SELECT b.*,n.name AS source_node_name,j.status AS job_status
            FROM reference_builds b
            LEFT JOIN nodes n ON n.node_id=b.source_node_id
            LEFT JOIN jobs j ON j.job_id=b.job_id
            ORDER BY b.created_at DESC LIMIT ?
        """, (max(1,min(200,int(limit))),)).fetchall()
    result=[]
    for row in rows:
        item=dict(row)
        for key in ("source_report_json","preflight_report_json","result_json"):
            try: item[key[:-5]]=json.loads(item.pop(key) or "{}")
            except Exception: item[key[:-5]]={}
        result.append(item)
    return result


def create_reference_build_draft(*, source_node_id: str, display_name: str, description: str = "", application: str = "plex") -> str:
    application=application.strip().lower()
    if application != "plex":
        raise HTTPException(400, "La phase 1.5.0 active uniquement la fondation Plex.")
    name=display_name.strip()
    if not name:
        raise HTTPException(400, "Le nom de la référence est obligatoire.")
    with db() as con:
        node=con.execute("SELECT node_id FROM nodes WHERE node_id=?",(source_node_id,)).fetchone()
    if not node:
        raise HTTPException(404,"Node source introuvable.")
    build_id=f"refbuild-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    stamp=now_iso()
    with db_lock, db() as con:
        con.execute("""
            INSERT INTO reference_builds(
                build_id,application,source_node_id,display_name,description,status,
                current_stage,progress,builder_name,builder_version,manifest_schema,
                requested_by,created_at,updated_at
            ) VALUES(?,?,?,?,?,'draft','foundation',0,'plex','1.0',1,'admin',?,?)
        """,(build_id,application,source_node_id,name,description.strip(),stamp,stamp))
        con.execute("""
            INSERT INTO reference_build_logs(build_id,stage,level,message,details_json,created_at)
            VALUES(?, 'foundation','info', ?, '{}', ?)
        """,(build_id,"Projet de référence créé. Aucune action intrusive n'a été lancée sur le node source.",stamp))
    return build_id



def launch_reference_discovery(build_id: str, source_instance: str = "") -> tuple[str, str]:
    with db() as con:
        build = con.execute("SELECT * FROM reference_builds WHERE build_id=?", (build_id,)).fetchone()
    if not build:
        raise HTTPException(404, "Build de référence introuvable.")
    nodes = {node["node_id"]: node for node in list_control_nodes()}
    node = nodes.get(build["source_node_id"])
    if not node or not node.get("agent_online"):
        raise HTTPException(409, "L’agent du node source est hors ligne.")
    caps = node.get("capabilities") or {}
    if not caps.get("reference_discovery"):
        raise HTTPException(409, "L’agent doit être mis à jour en version 1.5.1 ou ultérieure pour analyser Plex.")
    job_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    stamp = now_iso()
    steps = workflow_definition("reference_discovery")
    with db_lock, db() as con:
        con.execute("""INSERT INTO jobs(job_id,client_id,node_id,action,title,status,progress,detail,created_at,updated_at,started_at,options_json)
                       VALUES(?,NULL,?,'reference_discovery',?,'running',5,?,?,?,?,'{}')""",
                    (job_id, build["source_node_id"], f"Analyse Plex — {build['display_name']}", "Commande de découverte en préparation.", stamp, stamp, stamp))
        con.executemany("""INSERT INTO job_steps(job_id,step_key,title,status,progress,detail,executor,resources_json)
                           VALUES(?,?,?,'pending',0,'','control-plane','{}')""", [(job_id,k,t) for k,t in steps])
        con.execute("UPDATE job_steps SET status='success',progress=100,detail=?,executor=? WHERE job_id=? AND step_key='connecting'",
                    (f"Agent {build['source_node_id']} en ligne.", f"agent-{build['source_node_id']}", job_id))
        con.execute("UPDATE job_steps SET status='running',progress=20,detail=?,executor=? WHERE job_id=? AND step_key='discovering'",
                    ("Commande de découverte distante envoyée.", f"agent-{build['source_node_id']}", job_id))
        con.execute("UPDATE reference_builds SET job_id=?,status='analyzing',current_stage='discovering',progress=10,started_at=COALESCE(started_at,?),updated_at=?,source_instance=? WHERE build_id=?",
                    (job_id, stamp, stamp, source_instance.strip() or None, build_id))
        con.execute("INSERT INTO reference_build_logs(build_id,stage,level,message,details_json,created_at) VALUES(?,'discovering','info',?,'{}',?)",
                    (build_id, "Analyse Plex distante lancée en lecture seule.", stamp))
    command_id = queue_agent_command(build["source_node_id"], "reference_discovery", {"build_id": build_id, "job_id": job_id, "application": "plex", "source_instance": source_instance.strip()})
    with db_lock, db() as con:
        con.execute("UPDATE jobs SET detail=?,updated_at=? WHERE job_id=?", (f"Commande agent {command_id[:8]} en cours.", now_iso(), job_id))
    return job_id, command_id


def finalize_reference_discovery_command(command: sqlite3.Row, status: str, result: dict[str, Any], error: str | None) -> None:
    if command["command_type"] != "reference_discovery":
        return
    try:
        command_payload = json.loads(command["payload_json"] or "{}")
    except Exception:
        command_payload = {}
    build_id = str(command_payload.get("build_id") or "")
    job_id = str(command_payload.get("job_id") or "")
    if not build_id or not job_id:
        return
    stamp = now_iso()
    if status == "success":
        preflight = result.get("preflight") or {}
        score = int(preflight.get("compatibility_score") or 1)
        totals = result.get("totals") or {}
        detail = f"Analyse terminée : {len(result.get('libraries') or [])} bibliothèque(s), {totals.get('movies',0)} film(s), {totals.get('shows',0)} série(s), compatibilité {score}/5."
        with db_lock, db() as con:
            con.execute("UPDATE reference_builds SET status='discovered',current_stage='completed',progress=100,source_report_json=?,preflight_report_json=?,error_text=NULL,completed_at=?,updated_at=? WHERE build_id=?",
                        (json.dumps(result,ensure_ascii=False), json.dumps(preflight,ensure_ascii=False), stamp, stamp, build_id))
            for key in ("connecting","discovering","collecting_metadata","compatibility_check","completed"):
                con.execute("UPDATE job_steps SET status='success',progress=100,detail=?,executor=? WHERE job_id=? AND step_key=?", (detail, f"agent-{command['node_id']}", job_id, key))
            con.execute("UPDATE jobs SET status='success',progress=100,detail=?,updated_at=?,finished_at=? WHERE job_id=?", (detail, stamp, stamp, job_id))
            con.execute("INSERT INTO reference_build_logs(build_id,stage,level,message,details_json,created_at) VALUES(?,'completed','success',?,?,?)",
                        (build_id, detail, json.dumps({"score":score},ensure_ascii=False), stamp))
        try:
            queue_reference_capture(build_id, result)
        except Exception as exc:
            failed_at = now_iso()
            with db_lock, db() as con:
                con.execute("UPDATE reference_builds SET status='build_failed',current_stage='capture',error_text=?,completed_at=?,updated_at=? WHERE build_id=?", (str(exc), failed_at, failed_at, build_id))
                con.execute("INSERT INTO reference_build_logs(build_id,stage,level,message,details_json,created_at) VALUES(?,'capture','error',?,'{}',?)", (build_id, str(exc), failed_at))
    else:
        detail = error or "Échec de la découverte Plex."
        with db_lock, db() as con:
            con.execute("UPDATE reference_builds SET status='discovery_failed',current_stage='discovering',progress=100,error_text=?,completed_at=?,updated_at=? WHERE build_id=?", (detail, stamp, stamp, build_id))
            con.execute("UPDATE job_steps SET status='failed',progress=100,detail=?,executor=? WHERE job_id=? AND step_key='discovering'", (detail, f"agent-{command['node_id']}", job_id))
            con.execute("UPDATE jobs SET status='error',progress=100,detail=?,updated_at=?,finished_at=? WHERE job_id=?", (detail, stamp, stamp, job_id))
            con.execute("INSERT INTO reference_build_logs(build_id,stage,level,message,details_json,created_at) VALUES(?,'discovering','error',?,'{}',?)", (build_id, detail, stamp))

@app.post("/reference-builds/{build_id}/retry")
def retry_reference_build(build_id: str):
    with db() as con:
        build = con.execute("SELECT * FROM reference_builds WHERE build_id=?", (build_id,)).fetchone()
    if not build:
        raise HTTPException(404, "Build de référence introuvable.")
    if build["status"] not in ("build_failed", "discovery_failed"):
        raise HTTPException(409, "Seuls les builds en échec peuvent être relancés.")
    if build["status"] == "discovery_failed":
        start_reference_discovery(build_id, build["source_instance"] or "")
    else:
        try:
            discovery = json.loads(build["source_report_json"] or "{}")
        except Exception:
            discovery = {}
        if not discovery:
            raise HTTPException(409, "Rapport de découverte absent : relancez une analyse complète.")
        with db_lock, db() as con:
            con.execute("UPDATE reference_builds SET error_text=NULL,completed_at=NULL,progress=50,updated_at=? WHERE build_id=?", (now_iso(), build_id))
        queue_reference_capture(build_id, discovery)
    return RedirectResponse("/reference-images", status_code=303)


@app.get("/api/reference-images")
def api_reference_images():
    return JSONResponse({"images": list_reference_images(), "builds": list_reference_builds()})


@app.get("/api/reference-builders")
def api_reference_builders():
    return JSONResponse({"builders": reference_builders(), "nodes": reference_capable_nodes("plex")})


@app.get("/api/reference-builds/{build_id}")
def api_reference_build_detail(build_id: str):
    with db() as con:
        row=con.execute("SELECT * FROM reference_builds WHERE build_id=?",(build_id,)).fetchone()
        logs=[dict(item) for item in con.execute("SELECT * FROM reference_build_logs WHERE build_id=? ORDER BY log_id",(build_id,)).fetchall()]
    if not row: raise HTTPException(404,"Build de référence introuvable.")
    return JSONResponse({"build":dict(row),"logs":logs})


@app.post("/reference-builds/draft")
def create_reference_build_foundation(
    source_node_id: str = Form(...),
    display_name: str = Form(...),
    description: str = Form(""),
    application: str = Form("plex"),
):
    build_id=create_reference_build_draft(source_node_id=source_node_id,display_name=display_name,description=description,application=application)
    record_event(None,"reference_build_draft_created",f"Projet de référence {build_id} créé sans action distante sur {source_node_id}.","success")
    return RedirectResponse("/reference-images",status_code=303)


@app.post("/reference-builds/{build_id}/discover")
def start_reference_discovery(build_id: str, source_instance: str = Form("")):
    launch_reference_discovery(build_id, source_instance)
    record_event(None, "reference_discovery_started", f"Analyse Plex lancée pour {build_id}.", "progress")
    return RedirectResponse("/reference-images", status_code=303)


@app.get("/reference-images", response_class=HTMLResponse)
def reference_images_page(request: Request):
    return templates.TemplateResponse(request, "reference_images.html", {
        "mode": APPBOX_MODE,
        "hostname": HOSTNAME,
        "images": list_reference_images(),
        "versions": list_reference_versions(),
        "builders": reference_builders(),
        "capable_nodes": reference_capable_nodes("plex"),
        "builds": list_reference_builds(),
        "reference_root": str(REFERENCE_ROOT),
        "active_page": "reference_images",
    })


@app.post("/reference-images")
def create_reference_image(
    name: str = Form(...),
    media_type: str = Form(...),
    description: str = Form(""),
    version: str = Form("1"),
    source_path: str = Form(...),
    application_version: str = Form(""),
    expected_paths: str = Form(""),
    catalog_items: int = Form(0),
    checksum: str = Form(""),
    notes: str = Form(""),
    publish: bool = Form(False),
):
    media_type = media_type.strip().lower()
    if media_type not in {"plex", "jellyfin"}:
        raise HTTPException(400, "Type d’image de référence invalide.")
    image_id = slugify_identifier(f"{media_type}-{name}")
    if not image_id:
        raise HTTPException(400, "Nom d’image invalide.")
    stamp = now_iso()
    with db_lock, db() as con:
        con.execute("""
            INSERT INTO reference_images(
                image_id,name,media_type,description,status,
                current_version_id,created_at,updated_at
            ) VALUES(?,?,?,?,? ,NULL,?,?)
        """, (
            image_id,
            name.strip(),
            media_type,
            description.strip(),
            "draft",
            stamp,
            stamp,
        ))
    paths = [line.strip() for line in expected_paths.splitlines() if line.strip()]
    version_id = register_reference_version(
        image_id=image_id,
        image_name=name.strip(),
        media_type=media_type,
        version=version.strip(),
        source_path=source_path.strip(),
        application_version=application_version,
        expected_paths=paths,
        catalog_items=catalog_items,
        checksum=checksum,
        notes=notes,
        publish=publish,
    )
    record_event(
        None,
        "reference_image_created",
        f"Image de référence {name} créée avec la version {version_id}.",
        "success",
    )
    return RedirectResponse("/reference-images", status_code=303)


@app.post("/reference-images/{image_id}/versions")
def create_reference_image_version(
    image_id: str,
    version: str = Form(...),
    source_path: str = Form(...),
    application_version: str = Form(""),
    expected_paths: str = Form(""),
    catalog_items: int = Form(0),
    checksum: str = Form(""),
    notes: str = Form(""),
    publish: bool = Form(False),
):
    with db() as con:
        image = con.execute(
            "SELECT * FROM reference_images WHERE image_id=?",
            (image_id,),
        ).fetchone()
    if not image:
        raise HTTPException(404, "Image de référence introuvable.")
    paths = [line.strip() for line in expected_paths.splitlines() if line.strip()]
    version_id = register_reference_version(
        image_id=image_id,
        image_name=image["name"],
        media_type=image["media_type"],
        version=version.strip(),
        source_path=source_path.strip(),
        application_version=application_version,
        expected_paths=paths,
        catalog_items=catalog_items,
        checksum=checksum,
        notes=notes,
        publish=publish,
    )
    record_event(
        None,
        "reference_version_created",
        f"Version {version_id} ajoutée.",
        "success",
    )
    return RedirectResponse("/reference-images", status_code=303)


@app.post("/reference-images/{image_id}/publish/{version_id}")
def publish_reference_image_version(image_id: str, version_id: str):
    stamp = now_iso()
    with db_lock, db() as con:
        version = con.execute("""
            SELECT v.*,s.source_path
            FROM reference_image_versions v
            JOIN catalog_snapshots s ON s.snapshot_id=v.snapshot_id
            WHERE v.version_id=? AND v.image_id=?
        """, (version_id, image_id)).fetchone()
        if not version:
            raise HTTPException(404, "Version introuvable.")
        if not version["source_path"] or not Path(version["source_path"]).exists():
            raise HTTPException(409, "La source de cette version est indisponible.")
        con.execute("""
            UPDATE reference_image_versions
            SET state='published',published_at=?
            WHERE version_id=?
        """, (stamp, version_id))
        con.execute("""
            UPDATE reference_images
            SET current_version_id=?,status='published',updated_at=?
            WHERE image_id=?
        """, (version_id, stamp, image_id))
    record_event(
        None,
        "reference_version_published",
        f"{image_id} utilise maintenant {version_id}.",
        "success",
    )
    return RedirectResponse("/reference-images", status_code=303)




@app.put("/api/agent/v1/{node_id}/reference-builds/{build_id}/archive")
async def upload_reference_build_archive(node_id: str, build_id: str, request: Request):
    authenticate_agent(request, node_id)
    with db() as con:
        build = con.execute("SELECT * FROM reference_builds WHERE build_id=? AND source_node_id=?", (build_id, node_id)).fetchone()
    if not build:
        raise HTTPException(404, "Build de référence introuvable pour ce node.")
    expected = str(request.headers.get("X-Reference-SHA256") or "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise HTTPException(400, "Checksum SHA256 absent ou invalide.")
    storage = _reference_build_storage(build_id)
    final = storage / "reference.tar.gz"
    temporary = storage / "reference.tar.gz.uploading"
    temporary.unlink(missing_ok=True)
    digest = hashlib.sha256(); size = 0
    try:
        with temporary.open("wb") as handle:
            async for chunk in request.stream():
                if not chunk:
                    continue
                handle.write(chunk); digest.update(chunk); size += len(chunk)
                if size > 500 * 1024 * 1024 * 1024:
                    raise HTTPException(413, "Archive de référence trop volumineuse.")
        actual = digest.hexdigest()
        if not secrets.compare_digest(actual, expected):
            raise HTTPException(409, "Checksum de l’archive téléversée invalide.")
        os.replace(temporary, final)
    finally:
        temporary.unlink(missing_ok=True)
    return JSONResponse({"status":"stored","archive_path":str(final),"sha256":expected,"compressed_size_bytes":size})

@app.get("/api/agent/v1/{node_id}/reference-deployments/{version_id}/archive")
def download_reference_deployment_archive(request: Request, node_id: str, version_id: str):
    authenticate_agent(request, node_id)
    archive, checksum = reference_deployment_archive(version_id)
    return FileResponse(
        archive,
        media_type="application/gzip",
        filename=f"{slugify_identifier(version_id)}.tar.gz",
        headers={"X-Reference-SHA256": checksum},
    )

@app.get("/api/deployment-images/{media_type}")
def api_deployment_images(media_type: str):
    if media_type not in {"plex", "jellyfin"}:
        raise HTTPException(400, "Type d’AppBox invalide.")
    return JSONResponse({"deployment_images": deployment_images(media_type)})


@app.get("/api/profiles/{media_type}")
def api_profiles_for_type(media_type: str):
    if media_type not in {"plex", "jellyfin"}:
        raise HTTPException(400, "Type invalide.")
    profiles = [
        item for item in list_profiles()
        if item["media_type"] == media_type
    ]
    return JSONResponse({"profiles": profiles})


@app.get("/storage", response_class=HTMLResponse)
def storage_page(request: Request):
    return templates.TemplateResponse(request, "storage.html", {
        "mode": APPBOX_MODE,
        "hostname": HOSTNAME,
        "mounts": list_storage_mounts(),
        "groups": list_mount_groups(),
        "snapshots": list_snapshots(),
        "reference_images": list_reference_images(),
        "reference_versions": list_reference_versions(),
        "profiles": list_profiles(enabled_only=False),
        "active_page": "storage",
    })


@app.post("/storage/mounts")
def create_storage_mount(
    name: str = Form(...),
    host_path: str = Form(...),
    container_path: str = Form(...),
    media_types: list[str] = Form(...),
    read_only: bool = Form(False),
    required: bool = Form(False),
    propagation: str = Form("rprivate"),
    description: str = Form(""),
):
    mount_id = slugify_identifier(name)
    if not mount_id or not host_path.startswith("/") or not container_path.startswith("/"):
        raise HTTPException(400, "Nom ou chemins de montage invalides.")
    if propagation not in {"rprivate", "rshared", "rslave"}:
        raise HTTPException(400, "Propagation invalide.")
    allowed = sorted(set(media_types) & {"plex", "jellyfin"})
    if not allowed:
        raise HTTPException(400, "Sélectionne au moins un type de média.")
    stamp = now_iso()
    with db_lock, db() as con:
        con.execute("""
            INSERT INTO storage_mounts(
                mount_id,name,node_id,host_path,container_path,read_only,
                propagation,required,media_types_json,enabled,description,
                created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,1,?,?,?)
        """, (
            mount_id,name.strip(),HOSTNAME,host_path.strip(),container_path.strip(),
            int(read_only),propagation,int(required),json.dumps(allowed),
            description.strip(),stamp,stamp,
        ))
    record_event(None, "storage_mount_created", f"Montage {name} ajouté.", "success")
    return RedirectResponse("/storage", status_code=303)


@app.post("/storage/groups")
def create_mount_group(
    name: str = Form(...),
    mount_ids: list[str] = Form(...),
    description: str = Form(""),
):
    group_id = slugify_identifier(name)
    if not group_id:
        raise HTTPException(400, "Nom de groupe invalide.")
    stamp = now_iso()
    with db_lock, db() as con:
        con.execute("""
            INSERT INTO mount_groups(
                group_id,name,description,is_default,enabled,created_at,updated_at
            ) VALUES(?,?,?,0,1,?,?)
        """, (group_id,name.strip(),description.strip(),stamp,stamp))
        con.executemany("""
            INSERT INTO mount_group_members(group_id,mount_id,position)
            VALUES(?,?,?)
        """, [(group_id,mount_id,index * 10) for index,mount_id in enumerate(mount_ids,1)])
    record_event(None, "mount_group_created", f"Groupe {name} ajouté.", "success")
    return RedirectResponse("/storage", status_code=303)


@app.post("/storage/snapshots")
def create_snapshot(
    name: str = Form(...),
    media_type: str = Form(...),
    version: str = Form("1"),
    source_path: str = Form(...),
    expected_paths: str = Form(""),
    notes: str = Form(""),
):
    snapshot_id = slugify_identifier(f"{media_type}-{name}-{version}")
    if media_type not in {"plex", "jellyfin"}:
        raise HTTPException(400, "Type de snapshot invalide.")
    paths = [line.strip() for line in expected_paths.splitlines() if line.strip()]
    source = Path(source_path.strip())
    status = "ready" if source.exists() else "missing"
    size = 0
    if source.exists():
        try:
            size = sum(item.stat().st_size for item in source.rglob("*") if item.is_file())
        except Exception:
            size = 0
    stamp = now_iso()
    with db_lock, db() as con:
        con.execute("""
            INSERT INTO catalog_snapshots(
                snapshot_id,name,media_type,version,source_path,size_bytes,status,
                expected_paths_json,notes,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """, (
            snapshot_id,name.strip(),media_type,version.strip(),str(source),
            size,status,json.dumps(paths),notes.strip(),stamp,stamp,
        ))
    record_event(None, "snapshot_registered", f"Snapshot {name} {version} enregistré ({status}).", "success" if status=="ready" else "warning")
    return RedirectResponse("/storage", status_code=303)


@app.post("/storage/profiles/{profile_id}")
def update_profile(
    profile_id: str,
    name: str = Form(...),
    media_type: str = Form(...),
    mount_group_id: str = Form(...),
    snapshot_id: str = Form(""),
    reference_version_id: str = Form(""),
    acceleration_mode: str = Form("auto"),
    enabled: bool = Form(False),
):
    snapshot_id = snapshot_id.strip() or None
    reference_version_id = reference_version_id.strip() or None
    reference_image_id = None
    if reference_version_id:
        reference = get_reference_version(reference_version_id)
        if not reference or reference["media_type"] != media_type:
            raise HTTPException(400, "Image de référence incompatible.")
        snapshot_id = reference["snapshot_id"]
        reference_image_id = reference["image_id"]
    if acceleration_mode not in {"auto", "disabled"}:
        raise HTTPException(400, "Mode d’accélération invalide.")
    if placement_mode not in {"manual", "automatic"}:
        raise HTTPException(400, "Mode de placement invalide.")
    if media_type not in {"plex", "jellyfin"}:
        raise HTTPException(400, "Type de profil invalide.")
    if mount_group_id not in {g["group_id"] for g in list_mount_groups()}:
        raise HTTPException(400, "Groupe de montages invalide.")
    if snapshot_id:
        snapshots = {s["snapshot_id"]: s for s in list_snapshots()}
        snapshot = snapshots.get(snapshot_id)
        if not snapshot or snapshot["media_type"] != media_type:
            raise HTTPException(400, "Catalogue incompatible avec le type du profil.")
    with db_lock, db() as con:
        if not con.execute(
            "SELECT 1 FROM provisioning_profiles WHERE profile_id=?",
            (profile_id,),
        ).fetchone():
            raise HTTPException(404, "Profil introuvable.")
        con.execute("""
            UPDATE provisioning_profiles
            SET name=?,media_type=?,snapshot_id=?,mount_group_id=?,
                is_blank=?,enabled=?,reference_image_id=?,
                reference_version_id=?,acceleration_mode=?,updated_at=?
            WHERE profile_id=?
        """, (
            name.strip(),media_type,snapshot_id,mount_group_id,
            int(snapshot_id is None),int(enabled),reference_image_id,
            reference_version_id,acceleration_mode,now_iso(),profile_id,
        ))
    record_event(None, "profile_updated", f"Profil {profile_id} modifié.", "success")
    return RedirectResponse("/storage", status_code=303)


@app.post("/storage/profiles")
def create_profile(
    name: str = Form(...),
    media_type: str = Form(...),
    mount_group_id: str = Form(...),
    snapshot_id: str = Form(""),
    reference_version_id: str = Form(""),
    acceleration_mode: str = Form("auto"),
    storage_mode: str = Form("independent"),
):
    profile_id = slugify_identifier(name)
    snapshot_id = snapshot_id.strip() or None
    reference_version_id = reference_version_id.strip() or None
    reference_image_id = None
    if reference_version_id:
        reference = get_reference_version(reference_version_id)
        if not reference or reference["media_type"] != media_type:
            raise HTTPException(400, "Image de référence incompatible.")
        snapshot_id = reference["snapshot_id"]
        reference_image_id = reference["image_id"]
    if acceleration_mode not in {"auto", "disabled"}:
        raise HTTPException(400, "Mode d’accélération invalide.")
    if media_type not in {"plex","jellyfin"} or storage_mode not in {"independent"}:
        raise HTTPException(400, "Profil invalide.")
    stamp = now_iso()
    with db_lock, db() as con:
        con.execute("""
            INSERT INTO provisioning_profiles(
                profile_id,name,media_type,snapshot_id,mount_group_id,
                storage_mode,is_blank,enabled,created_at,updated_at,
                reference_image_id,reference_version_id,acceleration_mode
            ) VALUES(?,?,?,?,?,?,?,1,?,?,?,?,?)
        """, (
            profile_id,name.strip(),media_type,snapshot_id,mount_group_id,
            storage_mode,int(snapshot_id is None),stamp,stamp,
            reference_image_id,reference_version_id,acceleration_mode,
        ))
    record_event(None, "profile_created", f"Profil {name} ajouté.", "success")
    return RedirectResponse("/storage", status_code=303)


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    return templates.TemplateResponse(request, "settings.html", {
        "mode": APPBOX_MODE,
        "hostname": HOSTNAME,
        "node": node_payload(),
        "placement": placement_config(),
        "tags": list_node_tags(),
        "nodes": list_control_nodes(),
        "active_page": "settings",
    })


@app.get("/changelog", response_class=HTMLResponse)
def changelog_page(request: Request):
    return templates.TemplateResponse(request, "changelog.html", {
        "mode": APPBOX_MODE,
        "hostname": HOSTNAME,
        "active_page": "changelog",
        "changelog": CHANGELOG,
        "version": VERSION,
    })


@app.get("/api/nodes/{node_id}/logs")
def api_node_logs(node_id: str, source: str = "docker", lines: int = 250):
    if node_id != HOSTNAME:
        raise HTTPException(404, "Node introuvable.")
    lines = max(20, min(1000, lines))
    if source == "provisioner":
        ok, output = run_command(
            ["docker", "logs", "--timestamps", "--tail", str(lines), "appbox-manager-artemis"],
            timeout=30,
        )
        label = "Provisioner AppBox Manager"
    elif source == "docker":
        since = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        until = datetime.now(timezone.utc).isoformat()
        ok_events, events = run_command(
            [
                "docker", "events",
                "--since", since,
                "--until", until,
                "--format", "{{.Time}} | {{.Type}} | {{.Action}} | {{.Actor.Attributes.name}}",
            ],
            timeout=30,
        )
        ok_ps, ps_output = run_command(
            [
                "docker", "ps", "-a",
                "--format", "{{.Names}} | {{.Status}} | {{.Image}}",
            ],
            timeout=20,
        )
        output = (
            "=== CONTENEURS ===\n"
            + (ps_output or "Aucun conteneur détecté.")
            + "\n\n=== ÉVÉNEMENTS DOCKER (2 dernières heures) ===\n"
            + (events or "Aucun événement Docker récent.")
        )
        ok = ok_events or ok_ps
        label = "Docker"
    else:
        raise HTTPException(400, "Source de logs inconnue.")
    return JSONResponse({
        "node_id": node_id,
        "source": source,
        "label": label,
        "ok": ok,
        "logs": output[-100000:],
        "generated_at": now_iso(),
    })


@app.get("/nodes/{node_id}", response_class=HTMLResponse)
def node_detail(request: Request, node_id: str):
    nodes = {node["node_id"]: node for node in list_control_nodes()}
    node = nodes.get(node_id)
    if not node:
        raise HTTPException(404, "Node introuvable.")
    if node_id == HOSTNAME:
        payload = node_payload()
        payload["tags"] = node["tags"]
        payload["agent_status"] = node.get("agent_status")
        payload["is_local"] = True
    else:
        payload = dict(node)
        memory_total = int(payload.get("memory_total_bytes") or 0)
        memory_available = int(payload.get("memory_available_bytes") or 0)
        disk_total = int(payload.get("disk_total_bytes") or 0)
        disk_free = int(payload.get("disk_free_bytes") or 0)
        payload["metrics"] = {
            "cpu_percent": 0,
            "load_1": float(payload.get("load_1") or 0),
            "ram_percent": (
                ((memory_total - memory_available) / memory_total) * 100
                if memory_total else 0
            ),
            "ram_used": max(0, memory_total - memory_available),
            "ram_total": memory_total,
            "disk_percent": (
                ((disk_total - disk_free) / disk_total) * 100
                if disk_total else 0
            ),
            "disk_free": disk_free,
            "disk_read_bps": 0,
            "disk_write_bps": 0,
            "net_rx_bps": 0,
            "net_tx_bps": 0,
            "collected_at": payload.get("metrics_collected_at"),
        }
        payload["running_jobs"] = 0
        payload["queued_jobs"] = 0
        payload["is_local"] = False
    return templates.TemplateResponse(request, "node.html", {
        "mode": APPBOX_MODE,
        "hostname": HOSTNAME,
        "node": payload,
        "appboxes": [
            item for item in list_appboxes()
            if item["node_id"] == node_id
        ],
        "queue": queued_jobs(30) if node_id == HOSTNAME else [],
        "active_page": "nodes",
    })


@app.post("/appboxes")
def create_appbox(
    client_id: str = Form(...),
    media_type: str = Form("plex"),
    profile_id: str = Form(""),
    deployment_image_id: str = Form(""),
    mount_group_id: str = Form("rdad-standard"),
    snapshot_id: str = Form(""),
    reference_version_id: str = Form(""),
    port_mode: str = Form("automatic"),
    media_port_requested: str = Form(""),
    acceleration_mode: str = Form("auto"),
    placement_mode: str = Form("manual"),
    target_node_id: str = Form(""),
    bare_metal_override: bool = Form(False),
    with_tautulli: bool = Form(False),
    deploy_now: bool = Form(False),
):
    client_id = client_id.strip().lower()
    media_type = media_type.strip().lower()
    profile_id = profile_id.strip()
    deployment_image_id = deployment_image_id.strip()
    snapshot_id = snapshot_id.strip() or None
    reference_version_id = reference_version_id.strip() or None
    reference_image_id = None
    mount_group_id = mount_group_id.strip() or "rdad-standard"
    port_mode = port_mode.strip().lower()
    acceleration_mode = acceleration_mode.strip().lower()
    placement_mode = placement_mode.strip().lower()
    target_node_id = target_node_id.strip() or HOSTNAME

    if not CLIENT_RE.fullmatch(client_id):
        raise HTTPException(400, "Identifiant invalide.")
    if media_type not in {"plex", "jellyfin"}:
        raise HTTPException(400, "Type d’AppBox invalide.")
    if port_mode not in {"automatic", "manual"}:
        raise HTTPException(400, "Mode de port invalide.")
    if acceleration_mode not in {"auto", "disabled"}:
        raise HTTPException(400, "Mode d’accélération invalide.")
    if media_type == "jellyfin":
        with_tautulli = False

    if deployment_image_id:
        reference_image_id, selected_reference_version = parse_deployment_image(deployment_image_id, media_type)
        if selected_reference_version:
            reference_version_id = selected_reference_version
        else:
            snapshot_id = None
            reference_version_id = None

    profiles = {item["profile_id"]: item for item in list_profiles()}
    if profile_id and not deployment_image_id:
        profile = profiles.get(profile_id)
        if not profile or profile["media_type"] != media_type:
            raise HTTPException(400, "Profil de provisioning incompatible.")
        mount_group_id = profile["mount_group_id"] or mount_group_id
        snapshot_id = profile["snapshot_id"] or snapshot_id
        reference_image_id = profile.get("reference_image_id")
        reference_version_id = profile.get("reference_version_id")
        acceleration_mode = profile.get("acceleration_mode") or acceleration_mode
        storage_mode = profile["storage_mode"]
    else:
        storage_mode = "independent"

    if reference_version_id:
        reference = get_reference_version(reference_version_id)
        if not reference or reference["media_type"] != media_type:
            raise HTTPException(400, "Image de référence incompatible avec l’AppBox.")
        snapshot_id = reference["snapshot_id"]
        reference_image_id = reference["image_id"]

    placement_result = evaluate_placement(
        placement_mode,
        target_node_id,
        allow_bare_metal_override=bare_metal_override,
    )
    selected_node = placement_result["selected"]
    selected_node_id = selected_node["node_id"]
    if selected_node_id != HOSTNAME and not selected_node.get("actionable"):
        raise HTTPException(
            409,
            "Le node sélectionné n’est pas prêt : agent hors ligne ou exécuteur de déploiement indisponible.",
        )

    mounts = mounts_for_group(mount_group_id, media_type)
    if selected_node_id == HOSTNAME:
        mount_errors = validate_mounts(mounts)
        if mount_errors:
            raise HTTPException(409, " ; ".join(mount_errors))

    with db_lock, db() as con:
        if con.execute("SELECT 1 FROM appboxes WHERE client_id=?", (client_id,)).fetchone():
            raise HTTPException(409, "Cette AppBox existe déjà.")
        reserved_media = {
            row[0] for row in con.execute(
                "SELECT plex_port FROM appboxes WHERE node_id=? AND plex_port IS NOT NULL",
                (selected_node_id,),
            )
        }
        reserved_tautulli = {
            row[0] for row in con.execute(
                "SELECT tautulli_port FROM appboxes WHERE node_id=? AND tautulli_port IS NOT NULL",
                (selected_node_id,),
            )
        }
        media_port = choose_media_port(media_type, media_port_requested, port_mode, reserved_media)
        tautulli_port = reserve_port(TAUTULLI_RANGE, reserved_tautulli) if with_tautulli else None

        appbox_dir = BASE_DIR / client_id
        appbox_dir.mkdir(parents=True, exist_ok=False)
        try:
            if media_type == "plex":
                (appbox_dir / "plex-config").mkdir()
            else:
                (appbox_dir / "jellyfin-config").mkdir()
                (appbox_dir / "jellyfin-cache").mkdir()
            if with_tautulli:
                (appbox_dir / "tautulli-config").mkdir()

            if selected_node_id == HOSTNAME:
                provision_snapshot(snapshot_id, media_type, appbox_dir)
                if reference_version_id and media_type == "plex":
                    sanitize_plex_clone(appbox_dir / "plex-config")
            elif snapshot_id and not reference_version_id:
                # Legacy snapshots are local-only. Reference images are transferred
                # directly from the central library by the target node agent.
                raise HTTPException(409, "Ce snapshot historique ne peut être déployé que sur le node local.")
            (appbox_dir / "compose.yml").write_text(
                compose_for(
                    client_id, media_type, media_port, tautulli_port,
                    mounts, acceleration_mode, selected_node_id,
                ),
                encoding="utf-8",
            )
            (appbox_dir / ".env").write_text(
                deployment_env_for({
                    "client_id": client_id,
                    "node_id": selected_node_id,
                    "type": media_type,
                }),
                encoding="utf-8",
            )
        except Exception:
            shutil.rmtree(appbox_dir, ignore_errors=True)
            raise

        containers = [f"plex-appb-{client_id.removeprefix('ab')}" if media_type == "plex" else f"jellyfin-{client_id}"]
        if with_tautulli:
            containers.append(f"tautulli-{client_id}")
        stamp = now_iso()
        con.execute("""
            INSERT INTO appboxes(
                client_id,node_id,media_type,with_tautulli,plex_port,
                tautulli_port,status,path,containers_json,created_at,updated_at,
                profile_id,snapshot_id,mount_group_id,storage_mode,port_mode,
                reference_image_id,reference_version_id,acceleration_mode,
                placement_mode,requested_node_id,selected_node_id,placement_reason
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            client_id,selected_node_id,media_type,int(with_tautulli),media_port,
            tautulli_port,"generated",str(appbox_dir),json.dumps(containers),
            stamp,stamp,profile_id or None,snapshot_id,mount_group_id,
            storage_mode,port_mode,reference_image_id,
            reference_version_id,acceleration_mode,placement_mode,
            target_node_id,selected_node_id,placement_result["reason"],
        ))
        con.executemany("""
            INSERT INTO appbox_mounts(
                client_id,mount_id,host_path,container_path,read_only,propagation
            ) VALUES(?,?,?,?,?,?)
        """, [
            (
                client_id,mount["mount_id"],mount["host_path"],
                mount["container_path"],int(mount["read_only"]),mount["propagation"],
            )
            for mount in mounts
        ])
        if snapshot_id:
            con.execute("""
                INSERT INTO snapshot_deployments(
                    client_id,snapshot_id,status,detail,deployed_at
                ) VALUES(?,?,?,'Snapshot copié vers la configuration AppBox.',?)
            """, (client_id,snapshot_id,"prepared",stamp))

    placement_decision_id = record_placement_decision(
        client_id,
        placement_mode,
        target_node_id,
        placement_result,
    )
    deployment_id = str(uuid.uuid4())
    with db_lock, db() as con:
        con.execute("""
            INSERT INTO control_plane_deployments(
                deployment_id,client_id,node_id,placement_decision_id,
                reference_version_id,status,current_step,progress,detail,
                created_at,updated_at
            ) VALUES(?,?,?,?,?,'prepared','compose_ready',25,?,?,?)
        """, (
            deployment_id,
            client_id,
            selected_node_id,
            placement_decision_id,
            reference_version_id,
            placement_result["reason"],
            now_iso(),
            now_iso(),
        ))

    source_label = "Plex vierge" if not reference_version_id else f"image de déploiement {reference_version_id}"
    record_event(
        client_id,
        "created",
        f"AppBox {media_type.upper()} créée sur {selected_node_id} depuis {source_label}, "
        f"groupe {mount_group_id}, port {media_port}. {placement_result['reason']}",
        "success",
    )
    sync_port_reservations()
    if deploy_now:
        create_job(client_id, "deploy", "Déploiement de l’AppBox", "Opération placée dans la file globale.")
    return RedirectResponse(f"/appboxes/{client_id}", status_code=303)


@app.get("/appboxes/{client_id}", response_class=HTMLResponse)
def appbox_detail(request: Request, client_id: str):
    item = get_appbox(client_id)
    if not item:
        raise HTTPException(404, "AppBox introuvable.")
    enriched = enrich_item(item)
    compose_path = Path(item["path"]) / "compose.yml"
    compose = compose_path.read_text(encoding="utf-8") if compose_path.exists() else ""
    with db() as con:
        selected_mounts = [
            dict(row) for row in con.execute("""
                SELECT * FROM appbox_mounts WHERE client_id=? ORDER BY container_path
            """, (client_id,)).fetchall()
        ]
        snapshot = None
        if item.get("snapshot_id"):
            row = con.execute("SELECT * FROM catalog_snapshots WHERE snapshot_id=?", (item["snapshot_id"],)).fetchone()
            snapshot = dict(row) if row else None
    return templates.TemplateResponse(request, "detail.html", {
        "item": enriched,
        "selected_mounts": selected_mounts,
        "snapshot": snapshot,
        "compose": compose,
        "events": list_events(client_id),
        "jobs": latest_jobs_for(client_id),
        "mode": APPBOX_MODE,
        "hostname": HOSTNAME,
        "active_page": "appboxes",
    })


def enqueue_action(request: Request, client_id: str, action: str):
    item = get_appbox(client_id)
    if not item:
        raise HTTPException(404, "AppBox introuvable.")
    if active_job_for(client_id):
        raise HTTPException(409, "Une opération est déjà en attente ou en cours pour cette AppBox.")
    runtime = container_runtime(str(item.get("node_id") or HOSTNAME), item["containers"][0])
    titles = {
        "deploy": "Démarrage de l’AppBox" if runtime.get("exists") else "Déploiement de l’AppBox",
        "start": "Démarrage de l’AppBox",
        "stop": "Arrêt de l’AppBox",
        "restart": "Redémarrage de l’AppBox",
        "recreate": "Recréation de l’AppBox",
        "delete": "Suppression de l’AppBox",
    }
    desired = "stopped" if action == "stop" else "running" if action in {"deploy", "start", "restart", "recreate"} else "deleted" if action == "delete" else None
    if desired:
        with db_lock, db() as con:
            con.execute("UPDATE appboxes SET desired_state=?,updated_at=? WHERE client_id=?", (desired, now_iso(), client_id))
    job_id = create_job(client_id, action, titles[action], f"Opération placée dans la file du node {item['node_id']}.", node_id=str(item["node_id"]))
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JSONResponse({"job_id": job_id, "client_id": client_id, "action": action})
    return RedirectResponse(f"/appboxes/{client_id}?job={job_id}", status_code=303)


@app.post("/appboxes/{client_id}/deploy")
def deploy_appbox(request: Request, client_id: str):
    return enqueue_action(request, client_id, "deploy")


@app.post("/appboxes/{client_id}/start")
def start_appbox(request: Request, client_id: str):
    return enqueue_action(request, client_id, "start")


@app.post("/appboxes/{client_id}/stop")
def stop_appbox(request: Request, client_id: str):
    return enqueue_action(request, client_id, "stop")


@app.post("/appboxes/{client_id}/restart")
def restart_appbox(request: Request, client_id: str):
    return enqueue_action(request, client_id, "restart")


@app.post("/appboxes/{client_id}/recreate")
def recreate_appbox(request: Request, client_id: str):
    return enqueue_action(request, client_id, "recreate")


@app.post("/appboxes/{client_id}/delete")
def delete_appbox(
    request: Request,
    client_id: str,
    deletion_mode: str = Form("delete"),
    confirmation: str = Form(""),
):
    item = get_appbox(client_id)
    if not item:
        raise HTTPException(404, "AppBox introuvable.")
    deletion_mode = deletion_mode.strip().lower()
    if deletion_mode not in {"archive", "delete", "purge"}:
        raise HTTPException(400, "Mode de suppression invalide.")
    if active_job_for(client_id):
        raise HTTPException(409, "Une opération est déjà en attente ou en cours pour cette AppBox.")
    protected = str(item.get("protection_level") or "standard").lower() == "production"
    if (protected or deletion_mode == "purge") and confirmation.strip().upper() != "SUPPRIMER":
        raise HTTPException(400, "Confirmation renforcée requise : saisissez SUPPRIMER.")
    with db_lock, db() as con:
        con.execute("UPDATE appboxes SET desired_state='deleted',updated_at=? WHERE client_id=?", (now_iso(), client_id))
    title = "Archivage de l’AppBox" if deletion_mode == "archive" else "Purge complète de l’AppBox" if deletion_mode == "purge" else "Suppression de l’AppBox"
    job_id = create_job(client_id, "delete", title, f"Mode sécurisé : {deletion_mode} · node {item['node_id']}", node_id=str(item["node_id"]), options={"deletion_mode": deletion_mode})
    record_audit("DELETE_APPBOX", client_id, str(item["node_id"]), deletion_mode, "QUEUED", "Suppression sécurisée placée dans la file.")
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JSONResponse({"job_id": job_id, "client_id": client_id, "action": "delete", "deletion_mode": deletion_mode})
    return RedirectResponse(f"/appboxes/{client_id}?job={job_id}", status_code=303)


@app.post("/appboxes/{client_id}/claim")
def claim_appbox(client_id: str, claim_code: str = Form(...)):
    item = get_appbox(client_id)
    if not item:
        raise HTTPException(404, "AppBox introuvable.")
    if item.get("type") != "plex":
        raise HTTPException(400, "Le Claim Plex ne s’applique pas à Jellyfin.")
    claim_code = claim_code.strip()
    if not CLAIM_RE.fullmatch(claim_code):
        raise HTTPException(400, "Code Claim invalide.")

    node_id = str(item.get("node_id") or HOSTNAME)
    nodes = {node["node_id"]: node for node in list_control_nodes()}
    node = nodes.get(node_id)
    if not node or not node.get("agent_online"):
        raise HTTPException(409, f"Agent du node {node_id} hors ligne.")
    if not node.get("capabilities", {}).get("deployment_executor"):
        raise HTTPException(409, f"Agent {node_id} sans capacité deployment_executor.")

    command_id = queue_agent_command(node_id, "appbox_action", {
        "client_id": client_id,
        "action": "claim",
        "claim_code": claim_code,
        "containers": item.get("containers") or [],
    })
    try:
        ok, result, error = wait_agent_command(command_id, timeout=360)
    finally:
        # Le claim est un secret à usage court : le supprimer de la file persistante.
        with db_lock, db() as con:
            con.execute(
                "UPDATE agent_commands SET payload_json=? WHERE command_id=?",
                (json.dumps({"client_id": client_id, "action": "claim", "claim_code": "[REDACTED]"}), command_id),
            )
    detail = result.get("output") or error or "Claim Plex terminé."
    record_event(client_id, "claim", detail, "success" if ok else "error")
    if not ok:
        raise HTTPException(500, detail)
    return RedirectResponse(f"/appboxes/{client_id}", status_code=303)


@app.get("/api/appboxes/{client_id}/status")
def api_appbox_status(client_id: str):
    item = get_appbox(client_id)
    if not item:
        raise HTTPException(404, "AppBox introuvable.")
    return JSONResponse(appbox_status_payload(item))


@app.get("/api/appboxes/{client_id}/jobs")
def api_appbox_jobs(client_id: str):
    if not get_appbox(client_id):
        raise HTTPException(404, "AppBox introuvable.")
    return JSONResponse({"jobs": latest_jobs_for(client_id, 20)})


@app.get("/api/jobs/{job_id}")
def api_job(job_id: str):
    with db() as con:
        row = con.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Job introuvable.")
    return JSONResponse(job_dict(row, include_steps=True))


@app.get("/api/jobs/{job_id}/export.json")
def api_job_export_json(job_id: str):
    with db() as con:
        row = con.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Job introuvable.")
    payload = job_dict(row, include_steps=True)
    return JSONResponse(
        payload,
        headers={"Content-Disposition": f'attachment; filename="workflow-{job_id}.json"'},
    )


@app.get("/api/jobs/{job_id}/export.txt")
def api_job_export_text(job_id: str):
    with db() as con:
        row = con.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Job introuvable.")
    job = job_dict(row, include_steps=True)
    lines = [
        "MARINOS APPBOX MANAGER — WORKFLOW EXPORT",
        "=" * 72,
        f"Job       : {job['job_id']}",
        f"AppBox    : {str(job.get('client_id') or '—').upper()}",
        f"Node      : {str(job.get('node_id') or '—').upper()}",
        f"Action    : {str(job.get('action') or '—').upper()}",
        f"Statut    : {str(job.get('status') or '—').upper()}",
        f"Progress  : {job.get('progress', 0)} %",
        f"Créé      : {job.get('created_at') or '—'}",
        f"Début     : {job.get('started_at') or '—'}",
        f"Fin       : {job.get('finished_at') or '—'}",
        f"Durée     : {job.get('duration_seconds') if job.get('duration_seconds') is not None else '—'} s",
        "",
        "ÉTAPES",
        "-" * 72,
    ]
    for index, step in enumerate(job["steps"], 1):
        lines.extend([
            f"{index:02d}. {step['title']}",
            f"    Clé       : {step['step_key']}",
            f"    Statut    : {step['status'].upper()}",
            f"    Exécuteur : {step.get('executor') or 'control-plane'}",
            f"    Début     : {step.get('started_at') or '—'}",
            f"    Fin       : {step.get('finished_at') or '—'}",
            f"    Durée     : {step.get('duration_seconds') if step.get('duration_seconds') is not None else '—'} s",
            f"    Ressources: {json.dumps(step.get('resources') or {}, ensure_ascii=False)}",
            "    Journal   :",
            *[f"      {line}" for line in (step.get("detail") or "Aucun journal.").splitlines()],
            "",
        ])
    lines.extend(["JOURNAL GLOBAL", "-" * 72, job.get("detail") or "Aucun journal global."])
    return PlainTextResponse(
        "\n".join(lines),
        headers={"Content-Disposition": f'attachment; filename="workflow-{job_id}.txt"'},
    )


@app.get("/api/queue")
def api_queue():
    with db() as con:
        rows = con.execute("""
            SELECT * FROM jobs WHERE status IN ('queued','running')
            ORDER BY CASE status WHEN 'running' THEN 0 ELSE 1 END, created_at
        """).fetchall()
    return JSONResponse({"jobs": [job_dict(row) for row in rows]})


@app.get("/api/nodes/{node_id}/status")
def api_node_status(node_id: str):
    if node_id == HOSTNAME:
        return JSONResponse(node_payload())
    nodes = {node["node_id"]: node for node in list_control_nodes()}
    node = nodes.get(node_id)
    if not node:
        raise HTTPException(404, "Node introuvable.")
    total = int(node.get("memory_total_bytes") or 0)
    available = int(node.get("memory_available_bytes") or 0)
    disk_total = int(node.get("disk_total_bytes") or 0)
    disk_free = int(node.get("disk_free_bytes") or 0)
    payload = json.loads(node.get("metrics_payload_json") or "{}") if node.get("metrics_payload_json") else {}
    metrics = {
        "cpu_percent": float(payload.get("cpu_percent") or 0),
        "ram_percent": ((total-available)/total*100) if total else 0,
        "disk_percent": ((disk_total-disk_free)/disk_total*100) if disk_total else 0,
        "disk_read_bps": float(payload.get("disk_read_bps") or 0),
        "disk_write_bps": float(payload.get("disk_write_bps") or 0),
        "net_rx_bps": float(payload.get("net_rx_bps") or 0),
        "net_tx_bps": float(payload.get("net_tx_bps") or 0),
        "docker_containers": int(payload.get("docker_containers") or 0),
        "running_containers": int(payload.get("running_containers") or 0),
    }
    return JSONResponse({"metrics": metrics, "running_jobs": 0, "queued_jobs": 0})


@app.get("/api/nodes/{node_id}/metrics")
def api_node_metrics(node_id: str, hours: int = 1):
    nodes = {node["node_id"] for node in list_control_nodes()}
    if node_id not in nodes:
        raise HTTPException(404, "Node introuvable.")
    hours = max(1, min(720, hours))
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with db() as con:
        rows = con.execute("""
            SELECT * FROM node_metrics
            WHERE node_id=? AND collected_at>=?
            ORDER BY metric_id
        """, (node_id, cutoff)).fetchall()
    return JSONResponse({"node_id": node_id, "metrics": [dict(row) for row in rows]})


@app.get("/health")
def health():
    return {
        "status": "ok",
        "version": VERSION,
        "mode": APPBOX_MODE,
        "host": HOSTNAME,
        "database": DB_FILE.exists(),
        "queue_worker": not worker_stop.is_set(),
        "inventory": True,
        "business_inventory": True,
        "jellyfin_appboxes": True,
        "resource_manager": True,
        "volume_mounts": True,
        "reference_snapshots": True,
        "manual_media_ports": True,
        "storage_profiles_ui": True,
        "plex_claim_username": True,
        "reference_images": True,
        "reference_deployment": True,
        "reference_image_versions": True,
        "reference_build_engine": True,
        "reference_builder_registry": True,
        "reference_builder_capabilities": True,
        "reference_build_jobs": True,
        "reference_discovery": True,
        "reference_discovery_read_only": True,
        "reference_build_intrusive_actions": True,
        "advanced_appbox_options": True,
        "control_plane_foundation": True,
        "node_tags": True,
        "manual_node_placement": True,
        "automatic_node_placement": True,
        "bare_metal_exclusion": True,
        "agent_registry": True,
        "agent_api_v1": True,
        "agent_heartbeat": True,
        "agent_inventory": True,
        "agent_command_queue": True,
        "node_editing": True,
        "node_deletion": True,
        "agent_metrics_isolated": True,
        "agent_token_ui": True,
        "agent_heartbeat_storage_fix": True,
        "remote_node_detail": True,
        "settings_ui_deduplicated": True,
        "agent_token_rotation_ui": True,
        "agent_self_service_installer": True,
        "agent_download_archive": True,
        "unified_agent_install": True,
        "remote_deployment_executor": True,
        "distributed_runtime_inventory": True,
        "remote_runtime_source_of_truth": True,
        "reconciliation_engine": True,
        "desired_observed_state": True,
        "drift_detection": True,
        "orphan_detection": True,
        "distribution_foundation": True,
        "rdad_mount_visible": Path("/mnt/decypharr-poc/.mnt").exists(),
    }
