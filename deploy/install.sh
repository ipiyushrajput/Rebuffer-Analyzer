#!/usr/bin/env bash
#
# Install RBA on 107.109.131.68 without Docker.
#
# Backend on 8010, frontend on 8080. Port 8001 belongs to the existing Metanalyser backend
# and is never taken. If either port is already bound, this script stops and tells you which
# one, so the free port can be chosen deliberately and recorded in README.md and .env.
#
#   sudo deploy/install.sh
#
set -euo pipefail

PREFIX="${PREFIX:-/opt/rba}"
DATA_DIR="${DATA_DIR:-/var/lib/rba}"
BACKEND_PORT="${RBA_PORT:-8010}"
FRONTEND_PORT="${RBA_FRONTEND_PORT:-8080}"
RESERVED_PORT=8001
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || fail "Run this as root."

# --- port check -------------------------------------------------------------
port_in_use() { ss -ltn "( sport = :$1 )" 2>/dev/null | tail -n +2 | grep -q . ; }

log "Checking ports"
if [ "$BACKEND_PORT" = "$RESERVED_PORT" ] || [ "$FRONTEND_PORT" = "$RESERVED_PORT" ]; then
  fail "Port $RESERVED_PORT belongs to the Metanalyser backend and is never used by RBA."
fi
for port in "$BACKEND_PORT" "$FRONTEND_PORT"; do
  if port_in_use "$port"; then
    ss -ltnp "( sport = :$port )" || true
    fail "Port $port is already bound. Choose the next free port, then set RBA_PORT or RBA_FRONTEND_PORT and record it in README.md and backend/.env."
  fi
done
log "Ports $BACKEND_PORT and $FRONTEND_PORT are free"

# --- prerequisites ----------------------------------------------------------
for binary in python3 node npm nginx ss; do
  command -v "$binary" >/dev/null || fail "$binary is not installed."
done
command -v ffmpeg >/dev/null || echo "note: ffmpeg is absent; the decode-error and quality detectors will not run."

id -u rba >/dev/null 2>&1 || useradd --system --home "$PREFIX" --shell /usr/sbin/nologin rba

# --- backend ----------------------------------------------------------------
log "Installing the backend to $PREFIX/backend"
mkdir -p "$PREFIX/backend" "$DATA_DIR/reports" "$DATA_DIR/evidence"
rsync -a --delete --exclude '.venv' --exclude '__pycache__' --exclude 'var' \
  "$REPO_ROOT/backend/" "$PREFIX/backend/"

python3 -m venv "$PREFIX/backend/.venv"
"$PREFIX/backend/.venv/bin/pip" install --upgrade pip >/dev/null
"$PREFIX/backend/.venv/bin/pip" install "$PREFIX/backend[mysql]"
"$PREFIX/backend/.venv/bin/python" -m playwright install chromium || \
  echo "note: the Playwright browser did not install; PDF export will be unavailable and HTML export still works."

if [ ! -f "$PREFIX/backend/.env" ]; then
  cp "$REPO_ROOT/backend/.env.example" "$PREFIX/backend/.env"
  echo "note: fill in $PREFIX/backend/.env with the database settings before starting."
fi
chmod 600 "$PREFIX/backend/.env"
chown -R rba:rba "$PREFIX/backend" "$DATA_DIR"

log "Applying database migrations"
sudo -u rba "$PREFIX/backend/.venv/bin/alembic" -c "$PREFIX/backend/alembic.ini" upgrade head || \
  echo "note: migrations did not run; the backend creates its tables on first start."

# --- frontend ---------------------------------------------------------------
log "Building the frontend"
rsync -a --delete --exclude 'node_modules' --exclude 'dist' \
  "$REPO_ROOT/frontend/" "$PREFIX/frontend/"
(
  cd "$PREFIX/frontend"
  VITE_API_BASE="http://107.109.131.68:${FRONTEND_PORT}/api" \
  VITE_WS_BASE="ws://107.109.131.68:${FRONTEND_PORT}/ws" \
  npm ci && npm run build
)
chown -R rba:rba "$PREFIX/frontend"

# --- services ---------------------------------------------------------------
log "Installing the service units"
install -m 644 "$REPO_ROOT/deploy/rba-backend.service" /etc/systemd/system/rba-backend.service
sed -e "s|listen 8080;|listen ${FRONTEND_PORT};|" \
    -e "s|http://127.0.0.1:8010|http://127.0.0.1:${BACKEND_PORT}|g" \
    "$REPO_ROOT/deploy/rba-frontend.nginx.conf" > /etc/nginx/conf.d/rba.conf

systemctl daemon-reload
systemctl enable --now rba-backend
nginx -t && systemctl reload nginx

log "Done"
echo "  Frontend : http://107.109.131.68:${FRONTEND_PORT}"
echo "  Backend  : http://107.109.131.68:${BACKEND_PORT}/api/health"
echo "  Logs     : journalctl -u rba-backend -f"
