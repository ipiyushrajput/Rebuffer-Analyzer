#!/usr/bin/env bash
#
# Install or upgrade StreamLens on an Ubuntu host without Docker.
#
# Backend on 8010, frontend on 8080. Port 8001 belongs to the existing Metanalyser backend
# and is never taken. On a first install, if either port is already bound, this script stops
# and tells you which one, so the free port can be chosen deliberately and recorded in
# README.md and .env. Run deploy/prereqs-ubuntu.sh first on a fresh host.
#
#   sudo deploy/install.sh          # first install, and every upgrade after a git pull
#
# Re-running it upgrades in place: backend/.env, the venv's data directory and the database
# are kept, the code and the frontend bundle are replaced, and the backend is restarted.
set -euo pipefail

PREFIX="${PREFIX:-/opt/rba}"
DATA_DIR="${DATA_DIR:-/var/lib/rba}"
BACKEND_PORT="${RBA_PORT:-8010}"
FRONTEND_PORT="${RBA_FRONTEND_PORT:-8080}"
RESERVED_PORT=8001
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT=/etc/systemd/system/rba-backend.service
SITE=/etc/nginx/conf.d/rba.conf
# Playwright keeps Chromium here rather than under root's home, which the service cannot read.
BROWSERS="$PREFIX/browsers"

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || fail "Run this as root (sudo)."

UPGRADE=0
[ -f "$UNIT" ] && [ -f "$SITE" ] && UPGRADE=1

# --- port check -------------------------------------------------------------
port_in_use() { ss -ltn "( sport = :$1 )" 2>/dev/null | tail -n +2 | grep -q . ; }

log "Checking ports"
if [ "$BACKEND_PORT" = "$RESERVED_PORT" ] || [ "$FRONTEND_PORT" = "$RESERVED_PORT" ]; then
  fail "Port $RESERVED_PORT belongs to the Metanalyser backend and is never used by StreamLens."
fi
if [ "$UPGRADE" -eq 1 ]; then
  log "Upgrade: ports $BACKEND_PORT and $FRONTEND_PORT belong to the installed StreamLens"
else
  for port in "$BACKEND_PORT" "$FRONTEND_PORT"; do
    if port_in_use "$port"; then
      ss -ltnp "( sport = :$port )" || true
      fail "Port $port is already bound. Choose the next free port, then set RBA_PORT or RBA_FRONTEND_PORT and record it in README.md and backend/.env."
    fi
  done
  log "Ports $BACKEND_PORT and $FRONTEND_PORT are free"
fi

# --- prerequisites ----------------------------------------------------------
for binary in node npm nginx ss rsync curl; do
  command -v "$binary" >/dev/null || fail "$binary is not installed. Run deploy/prereqs-ubuntu.sh first."
done

# The newest Python 3.11+ on the host; Ubuntu 22.04's python3 is 3.10.
PYTHON="${PYTHON:-}"
if [ -z "$PYTHON" ]; then
  for candidate in python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null &&
       "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
      PYTHON="$candidate"
      break
    fi
  done
fi
[ -n "$PYTHON" ] || fail "Python 3.11 or newer is not installed. Run deploy/prereqs-ubuntu.sh first."
"$PYTHON" -c 'import venv, ensurepip' 2>/dev/null ||
  fail "$PYTHON has no venv module. Install ${PYTHON}-venv (apt-get install ${PYTHON##*/}-venv)."

NODE_MAJOR="$(node --version | sed -E 's/^v([0-9]+).*/\1/')"
[ "$NODE_MAJOR" -ge 20 ] || fail "Node $(node --version) is too old; 20 or newer is needed. Run deploy/prereqs-ubuntu.sh first."

command -v ffmpeg >/dev/null || echo "note: ffmpeg is absent; the decode-error and quality detectors will not run."
log "Using $("$PYTHON" -V) and Node $(node --version)"

id -u rba >/dev/null 2>&1 || useradd --system --home "$PREFIX" --shell /usr/sbin/nologin rba

# --- backend ----------------------------------------------------------------
log "Installing the backend to $PREFIX/backend"
mkdir -p "$PREFIX/backend" "$BROWSERS" "$DATA_DIR/reports" "$DATA_DIR/evidence" \
  "$DATA_DIR/recordings" "$DATA_DIR/storage"
# .env is the host's own and is never replaced by the checkout's.
rsync -a --delete --exclude '.venv' --exclude '__pycache__' --exclude 'var' --exclude '.env' \
  --exclude '.pytest_cache' --exclude '.mypy_cache' --exclude '.ruff_cache' \
  "$REPO_ROOT/backend/" "$PREFIX/backend/"

[ -x "$PREFIX/backend/.venv/bin/python" ] || "$PYTHON" -m venv "$PREFIX/backend/.venv"
"$PREFIX/backend/.venv/bin/python" -m pip install --upgrade pip >/dev/null
"$PREFIX/backend/.venv/bin/python" -m pip install "$PREFIX/backend[mysql]"
PLAYWRIGHT_BROWSERS_PATH="$BROWSERS" \
  "$PREFIX/backend/.venv/bin/python" -m playwright install --with-deps chromium || \
  echo "note: the Playwright browser did not install; PDF export will be unavailable and HTML export still works."

if [ ! -f "$PREFIX/backend/.env" ]; then
  # Data directories go under $DATA_DIR, the one path the service may write.
  sed -e "s|=\./var|=${DATA_DIR}|" -e "s|^RBA_PORT=.*|RBA_PORT=${BACKEND_PORT}|" \
    "$REPO_ROOT/backend/.env.example" > "$PREFIX/backend/.env"
  echo "note: fill in $PREFIX/backend/.env with the database settings, then re-run this script."
fi
chmod 600 "$PREFIX/backend/.env"
chown -R rba:rba "$PREFIX/backend" "$BROWSERS" "$DATA_DIR"

log "Applying database migrations"
(cd "$PREFIX/backend" && runuser -u rba -- "$PREFIX/backend/.venv/bin/alembic" upgrade head) || \
  echo "note: migrations did not run; check the DB_* settings in $PREFIX/backend/.env."

# --- frontend ---------------------------------------------------------------
# Built without VITE_API_BASE / VITE_WS_BASE unless they are set in the environment: the page
# then calls /api and /ws on whatever origin served it — the IP, a hostname, or the load
# balancer's HTTPS name — through the nginx site below.
log "Building the frontend"
mkdir -p "$PREFIX/frontend"
rsync -a --delete --exclude 'node_modules' --exclude 'dist' \
  "$REPO_ROOT/frontend/" "$PREFIX/frontend/"
(
  cd "$PREFIX/frontend"
  npm ci --no-audit --no-fund && npm run build
)
chown -R rba:rba "$PREFIX/frontend"

# --- services ---------------------------------------------------------------
log "Installing the service units"
sed -e "s|/opt/rba|${PREFIX}|g" -e "s|/var/lib/rba|${DATA_DIR}|g" \
  "$REPO_ROOT/deploy/rba-backend.service" > "$UNIT"
chmod 644 "$UNIT"
sed -e "s|listen 8080;|listen ${FRONTEND_PORT};|" \
    -e "s|http://127.0.0.1:8010|http://127.0.0.1:${BACKEND_PORT}|g" \
    -e "s|/opt/rba|${PREFIX}|g" \
    "$REPO_ROOT/deploy/rba-frontend.nginx.conf" > "$SITE"

systemctl daemon-reload
systemctl enable rba-backend >/dev/null
systemctl restart rba-backend
nginx -t || fail "nginx rejected its configuration; $SITE is installed, see the error above."
systemctl reload nginx

log "Waiting for the backend"
status="not answering"
for _ in $(seq 1 30); do
  if body="$(curl -fsS "http://127.0.0.1:${BACKEND_PORT}/api/health" 2>/dev/null)"; then
    status="$(printf '%s' "$body" | "$PYTHON" -c 'import json,sys; d=json.load(sys.stdin); print(" - ".join([d.get("status", "unknown"), *d.get("degraded", [])]))')"
    break
  fi
  sleep 2
done

page="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${FRONTEND_PORT}/" || true)"

log "Done"
echo "  Health   : $status"
echo "  nginx    : HTTP $page on port ${FRONTEND_PORT}"
echo "  Frontend : http://$(hostname -I 2>/dev/null | awk '{print $1}'):${FRONTEND_PORT}"
echo "  Backend  : http://127.0.0.1:${BACKEND_PORT}/api/health"
echo "  Logs     : journalctl -u rba-backend -f"
