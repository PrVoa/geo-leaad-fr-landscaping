#!/usr/bin/env bash
# git_sync.sh — Synchronisation automatique GitHub → Serveur toutes les 5 minutes
#
# - Pull les changements depuis origin/main
# - Redémarre geoleaad-api si des fichiers API ont changé
# - NE redémarre JAMAIS le scraper automatiquement
# - Log tout dans logs/git_sync.log

set -euo pipefail

# ── Config ──────────────────────────────────────────────────────────────────
ROOT="$(cd "$(dirname "$0")" && pwd)"
LOG_FILE="$ROOT/logs/git_sync.log"
API_SERVICE="geoleaad-api"
INTERVAL=300   # 5 minutes

# Fichiers/dossiers qui déclenchent un redémarrage de l'API
API_TRIGGERS=("api/" "requirements.txt" ".env")

# ── Helpers ─────────────────────────────────────────────────────────────────
log() {
  local ts
  ts="$(date '+%Y-%m-%d %H:%M:%S')"
  echo "[$ts] $*" | tee -a "$LOG_FILE"
}

restart_api() {
  log "→ Redémarrage de $API_SERVICE…"
  if systemctl restart "$API_SERVICE" 2>&1 | tee -a "$LOG_FILE"; then
    log "✓ $API_SERVICE redémarré avec succès"
  else
    log "✗ Échec du redémarrage de $API_SERVICE"
  fi
}

# ── Init ────────────────────────────────────────────────────────────────────
mkdir -p "$ROOT/logs"
log "========================================"
log "  git_sync démarré (intervalle: ${INTERVAL}s)"
log "========================================"

cd "$ROOT"

# ── Boucle principale ────────────────────────────────────────────────────────
while true; do
  # 1. Récupérer les infos distantes sans merger
  if ! git fetch origin main 2>&1 | tee -a "$LOG_FILE"; then
    log "✗ git fetch échoué — vérifier la connexion réseau ou le token"
    sleep "$INTERVAL"
    continue
  fi

  # 2. Vérifier s'il y a des nouveaux commits
  LOCAL=$(git rev-parse HEAD)
  REMOTE=$(git rev-parse origin/main)

  if [ "$LOCAL" = "$REMOTE" ]; then
    log "— Aucun changement ($(git rev-parse --short HEAD))"
    sleep "$INTERVAL"
    continue
  fi

  # 3. Lister les fichiers modifiés AVANT le pull
  CHANGED_FILES=$(git diff --name-only HEAD origin/main)
  log "Nouveaux commits détectés — fichiers modifiés :"
  echo "$CHANGED_FILES" | while read -r f; do log "  • $f"; done

  # 4. Pull
  if ! git pull origin main 2>&1 | tee -a "$LOG_FILE"; then
    log "✗ git pull échoué"
    sleep "$INTERVAL"
    continue
  fi
  log "✓ Pull effectué → $(git rev-parse --short HEAD)"

  # 5. Analyser les fichiers changés
  NEED_API_RESTART=false
  SCHEDULER_UPDATED=false

  while IFS= read -r file; do
    # Vérifier si le scraper a changé
    if [[ "$file" == "scripts/scheduler.py" ]]; then
      SCHEDULER_UPDATED=true
    fi

    # Vérifier si un fichier API a changé
    for trigger in "${API_TRIGGERS[@]}"; do
      if [[ "$file" == ${trigger}* || "$file" == "$trigger" ]]; then
        NEED_API_RESTART=true
        break
      fi
    done
  done <<< "$CHANGED_FILES"

  # 6. Avertissement scraper (jamais de redémarrage auto)
  if [ "$SCHEDULER_UPDATED" = true ]; then
    log "⚠️  scheduler.py mis à jour — redémarrer manuellement si besoin"
  fi

  # 7. Redémarrage API si nécessaire
  if [ "$NEED_API_RESTART" = true ]; then
    restart_api
  else
    log "— Pas de changement dans l'API, aucun redémarrage nécessaire"
  fi

  sleep "$INTERVAL"
done
