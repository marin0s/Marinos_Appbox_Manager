#!/usr/bin/env bash
set -Eeuo pipefail

TARGET="/opt/appbox-manager-poc"

echo "[1/5] Vérification des prérequis"
command -v docker >/dev/null || { echo "Docker manquant"; exit 1; }
docker compose version >/dev/null || { echo "Plugin Docker Compose manquant"; exit 1; }

echo "[2/5] Création de ${TARGET}"
mkdir -p "${TARGET}"
cp -a . "${TARGET}/"
cd "${TARGET}"

echo "[3/5] Initialisation"
[ -f .env ] || cp .env.example .env
mkdir -p data generated

echo "[4/5] Construction et démarrage"
docker compose up -d --build

echo "[5/5] État"
docker compose ps
echo
echo "Interface : http://$(hostname -I | awk '{print $1}'):8090"
echo "Mode initial : MOCK"
