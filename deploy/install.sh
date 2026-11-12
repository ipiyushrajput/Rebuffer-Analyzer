#!/usr/bin/env bash
#
# Install or upgrade StreamLens on an Ubuntu host without Docker.
#
# The UI is served under /streamlens/ (the API at /streamlens/api/, WebSockets at
# /streamlens/ws/), so StreamLens never claims `/` on a host that serves other things.
#
# One machine (the default):
#   sudo deploy/install.sh
# Two machines:
#   sudo deploy/install.sh --role backend                          # on the backend machine
#   sudo deploy/install.sh --role frontend --backend 10.0.0.5:8010  # on the frontend machine
# Inside a site that already owns `/` on the frontend machine (e.g. https://dev.samsungtv):
#   sudo deploy/install.sh --role frontend --backend 10.0.0.5:8010 --snippet-only
#   then add `include /etc/nginx/snippets/streamlens.conf;` inside that site's server block.
#
# Options:
#   --role all|backend|frontend   what this machine runs (default all)
#   --backend HOST[:PORT]         the backend as the frontend reaches it (frontend role;
#                                 default 127.0.0.1:RBA_PORT)
#   --base-path /streamlens       the path the UI is served under (default /streamlens)
#   --snippet-only                install the nginx locations only, not the :8080 site
#
# Backend on 8010, frontend site on 8080. Port 8001 belongs to the existing Metanalyser
# backend and is never taken. On a first install, if a port this role binds is already bound,
# this script stops and tells you which one, so the free port can be chosen deliberately and
# recorded in README.md and .env. Run deploy/prereqs-ubuntu.sh first on a fresh host.
#
# Re-running it upgrades in place: backend/.env, the venv's data directory and the database
# are kept, the code and the frontend bundle are replaced, and the backend is restarted.
set -euo pipefail

PREFIX="${PREFIX:-/opt/rba}"
DATA_DIR="${DATA_DIR:-/var/lib/rba}"
BACKEND_PORT="${RBA_PORT:-8010}"
FRONTEND_PORT="${RBA_FRONTEND_PORT:-8080}"
ROLE="${STREAMLENS_ROLE:-all}"
BACKEND="${STREAMLENS_BACKEND:-}"
BASE_PATH="${STREAMLENS_BASE_PATH:-/streamlens}"
SNIPPET_ONLY=0
RESERVED_PORT=8001
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT=/etc/systemd/system/rba-backend.service
SITE=/etc/nginx/conf.d/rba.conf
SNIPPET=/etc/nginx/snippets/streamlens.conf
WEBROOT="$PREFIX/www"
# Playwright keeps Chromium here rather than under root's home, which the service cannot read.
BROWSERS="$PREFIX/browsers"

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --role) ROLE="${2:-}"; shift 2 ;;
    --role=*) ROLE="${1#*=}"; shift ;;
    --backend) BACKEND="${2:-}"; shift 2 ;;
    --backend=*) BACKEND="${1#*=}"; shift ;;
    --base-path) BASE_PATH="${2:-}"; shift 2 ;;
    --base-path=*) BASE_PATH="${1#*=}"; shift ;;
    --snippet-only) SNIPPET_ONLY=1; shift ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) fail "unknown option: $1 (see --help)" ;;
  esac
done

[ "$(id -u)" -eq 0 ] || fail "Run this as root (sudo)."

case "$ROLE" in
  all|backend|frontend) ;;
  *) fail "--role is all, backend or frontend, not '$ROLE'." ;;
esac
WITH_BACKEND=0; WITH_FRONTEND=0
[ "$ROLE" != frontend ] && WITH_BACKEND=1
[ "$ROLE" != backend ] && WITH_FRONTEND=1

# /streamlens, /streamlens/ and streamlens all mean /streamlens.
BASE_PATH="/$(printf '%s' "$BASE_PATH" | sed -E 's|^/+||; s|/+$||')"
[ "$BASE_PATH" != "/" ] || fail "--base-path must be a path such as /streamlens; StreamLens does not take /."
printf '%s' "$BASE_PATH" | grep -Eq '^(/[A-Za-z0-9._-]+)+$' ||
  fail "--base-path '$BASE_PATH' may contain only letters, digits, '.', '_', '-' and '/'."

# The backend as nginx reaches it: this machine, unless the backend is elsewhere.
if [ -z "$BACKEND" ]; then
  [ "$ROLE" != frontend ] || fail "--role frontend needs --backend HOST:PORT, the backend machine's address (e.g. 10.0.0.5:8010)."
  BACKEND="127.0.0.1:${BACKEND_PORT}"
fi
BACKEND="${BACKEND#http://}"; BACKEND="${BACKEND%/}"
case "$BACKEND" in *:*) ;; *) BACKEND="${BACKEND}:8010" ;; esac
[ "${BACKEND##*:}" != "$RESERVED_PORT" ] || fail "Port $RESERVED_PORT belongs to the Metanalyser backend and is never used by StreamLens."

case "$ROLE" in
  all) log "Role: all · UI under ${BASE_PATH}/ · backend on this machine, port $BACKEND_PORT" ;;
  backend) log "Role: backend · port $BACKEND_PORT" ;;
  frontend) log "Role: frontend · UI under ${BASE_PATH}/ · backend at $BACKEND" ;;
esac

# --- port check -------------------------------------------------------------
port_in_use() { ss -ltn "( sport = :$1 )" 2>/dev/null | tail -n +2 | grep -q . ; }

ports=()
if [ "$WITH_BACKEND" -eq 1 ]; then
  [ -f "$UNIT" ] || ports+=("$BACKEND_PORT")
  [ "$BACKEND_PORT" != "$RESERVED_PORT" ] || fail "Port $RESERVED_PORT belongs to the Metanalyser backend and is never used by StreamLens."
fi
if [ "$WITH_FRONTEND" -eq 1 ] && [ "$SNIPPET_ONLY" -eq 0 ]; then
  [ -f "$SITE" ] || ports+=("$FRONTEND_PORT")
  [ "$FRONTEND_PORT" != "$RESERVED_PORT" ] || fail "Port $RESERVED_PORT belongs to the Metanalyser backend and is never used by StreamLens."
fi
log "Checking ports"
for port in "${ports[@]}"; do
  if port_in_use "$port"; then
    ss -ltnp "( sport = :$port )" || true
    fail "Port $port is already bound. Choose the next free port, then set RBA_PORT or RBA_FRONTEND_PORT and record it in README.md and backend/.env."
  fi
done
if [ "${#ports[@]}" -eq 0 ]; then
  log "Upgrade: the ports belong to the installed StreamLens"
else
  log "Port(s) ${ports[*]} free"
fi

# --- prerequisites ----------------------------------------------------------
need=(ss rsync curl)
[ "$WITH_FRONTEND" -eq 1 ] && need+=(node npm nginx)
for binary in "${need[@]}"; do
  command -v "$binary" >/dev/null || fail "$binary is not installed. Run deploy/prereqs-ubuntu.sh first."
done

if [ "$WITH_BACKEND" -eq 1 ]; then
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
  command -v ffmpeg >/dev/null || echo "note: ffmpeg is absent; the decode-error and quality detectors will not run."
  log "Using $("$PYTHON" -V)"
fi
if [ "$WITH_FRONTEND" -eq 1 ]; then
  NODE_MAJOR="$(node --version | sed -E 's/^v([0-9]+).*/\1/')"
  [ "$NODE_MAJOR" -ge 20 ] || fail "Node $(node --version) is too old; 20 or newer is needed. Run deploy/prereqs-ubuntu.sh first."
  log "Using Node $(node --version)"
fi

# --- backend ----------------------------------------------------------------
if [ "$WITH_BACKEND" -eq 1 ]; then
  id -u rba >/dev/null 2>&1 || useradd --system --home "$PREFIX" --shell /usr/sbin/nologin rba

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

  log "Installing the backend service"
  sed -e "s|/opt/rba|${PREFIX}|g" -e "s|/var/lib/rba|${DATA_DIR}|g" \
    "$REPO_ROOT/deploy/rba-backend.service" > "$UNIT"
  chmod 644 "$UNIT"
  systemctl daemon-reload
  systemctl enable rba-backend >/dev/null
  systemctl restart rba-backend
fi

# --- frontend ---------------------------------------------------------------
if [ "$WITH_FRONTEND" -eq 1 ]; then
  # Built for BASE_PATH and without VITE_API_BASE / VITE_WS_BASE unless they are set in the
  # environment: the page then calls BASE_PATH/api and BASE_PATH/ws on whatever origin served
  # it — the IP, a hostname, or the load balancer's HTTPS name — through nginx.
  log "Building the frontend for ${BASE_PATH}/"
  mkdir -p "$PREFIX/frontend" "$WEBROOT${BASE_PATH}"
  rsync -a --delete --exclude 'node_modules' --exclude 'dist' \
    "$REPO_ROOT/frontend/" "$PREFIX/frontend/"
  (
    cd "$PREFIX/frontend"
    npm ci --no-audit --no-fund && VITE_BASE_PATH="${BASE_PATH}/" npm run build
  )
  rsync -a --delete "$PREFIX/frontend/dist/" "$WEBROOT${BASE_PATH}/"
  chmod -R a+rX "$WEBROOT"

  if [ "$WITH_BACKEND" -eq 0 ]; then
    if curl -fsS -o /dev/null --max-time 5 "http://${BACKEND}/api/health"; then
      log "The backend at $BACKEND answers"
    else
      echo "note: the backend at $BACKEND does not answer from this machine yet. The site is installed; ask for TCP ${BACKEND##*:} from this machine to the backend to be opened."
    fi
  fi

  log "Installing the nginx locations"
  mkdir -p "$(dirname "$SNIPPET")"
  sed -e "s|\${STREAMLENS_BASE_PATH}|${BASE_PATH}|g" \
      -e "s|\${STREAMLENS_BACKEND}|${BACKEND}|g" \
      -e "s|\${STREAMLENS_WEBROOT}|${WEBROOT}|g" \
      "$REPO_ROOT/frontend/nginx/streamlens-locations.conf.template" > "$SNIPPET"
  if [ "$SNIPPET_ONLY" -eq 0 ]; then
    sed -e "s|listen 8080;|listen ${FRONTEND_PORT};|" \
        -e "s|return 302 /streamlens/;|return 302 ${BASE_PATH}/;|" \
        "$REPO_ROOT/deploy/rba-frontend.nginx.conf" > "$SITE"
  elif ! grep -rqs "include $SNIPPET;" /etc/nginx/conf.d /etc/nginx/sites-enabled; then
    echo "note: add this line inside the server block that should serve StreamLens, then run: sudo nginx -t && sudo systemctl reload nginx"
    echo "        include $SNIPPET;"
  fi
  nginx -t || fail "nginx rejected its configuration; $SNIPPET is installed, see the error above."
  systemctl reload nginx
  # A reload returns before the new workers take over; until they do, the old configuration
  # answers, and a check made now would describe it rather than this one.
  sleep 3
fi

# --- result -----------------------------------------------------------------
health() {
  python3 -c 'import json,sys; d=json.load(sys.stdin); print(" - ".join([d.get("status", "unknown"), *d.get("degraded", [])]))' 2>/dev/null || echo "unreadable"
}

status="not answering"
if [ "$WITH_BACKEND" -eq 1 ]; then
  log "Waiting for the backend"
  for _ in $(seq 1 30); do
    if body="$(curl -fsS "http://127.0.0.1:${BACKEND_PORT}/api/health" 2>/dev/null)"; then
      status="$(printf '%s' "$body" | health)"
      break
    fi
    sleep 2
  done
fi

log "Done"
host="$(hostname -I 2>/dev/null | awk '{print $1}')"
if [ "$WITH_BACKEND" -eq 1 ]; then
  echo "  Backend  : http://${host}:${BACKEND_PORT}/api/health  ($status)"
  echo "  Logs     : journalctl -u rba-backend -f"
fi
if [ "$WITH_FRONTEND" -eq 1 ] && [ "$SNIPPET_ONLY" -eq 0 ]; then
  page="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${FRONTEND_PORT}${BASE_PATH}/" || true)"
  api_body="$(mktemp)"
  api_code="$(curl -s -o "$api_body" -w '%{http_code}' "http://127.0.0.1:${FRONTEND_PORT}${BASE_PATH}/api/health" || true)"
  case "$api_code" in
    200) through="$(health < "$api_body")" ;;
    502|504) through="HTTP $api_code: nginx cannot reach the backend at $BACKEND" ;;
    *) through="HTTP ${api_code:-none}" ;;
  esac
  rm -f "$api_body"
  echo "  UI       : http://${host}:${FRONTEND_PORT}${BASE_PATH}/  (HTTP $page)"
  echo "  API      : http://${host}:${FRONTEND_PORT}${BASE_PATH}/api/health  ($through)"
elif [ "$WITH_FRONTEND" -eq 1 ]; then
  echo "  UI       : ${BASE_PATH}/ on the site whose server block includes $SNIPPET"
fi
