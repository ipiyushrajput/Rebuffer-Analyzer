#!/usr/bin/env bash
#
# Install the system packages StreamLens needs on Ubuntu 22.04 or 24.04, before
# deploy/install.sh. Safe to re-run: anything already present at a usable version is kept.
#
#   sudo deploy/prereqs-ubuntu.sh                 # Python 3.11+, Node 22, nginx, ffmpeg, ...
#   sudo deploy/prereqs-ubuntu.sh --with-bento4   # also mp4decrypt, for cbcs DRM channels
#
# Python: 24.04 ships 3.12 and is used as it is. 22.04 ships 3.10, which is too old, so
# 3.12 comes from the deadsnakes PPA and install.sh picks it up as python3.12.
# Node: Ubuntu's own nodejs is 12 (22.04) or 18 (24.04); the frontend build needs 20+, so
# Node 22 comes from NodeSource.
# Chromium's libraries for PDF export are installed by install.sh, through Playwright.
set -euo pipefail

WITH_BENTO4=0
BENTO4_VERSION="${BENTO4_VERSION:-1-6-0-641}"
for arg in "$@"; do
  case "$arg" in
    --with-bento4) WITH_BENTO4=1 ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || fail "Run this as root (sudo)."
# shellcheck disable=SC1091
. /etc/os-release
[ "${ID:-}" = "ubuntu" ] || echo "note: written for Ubuntu; this is ${PRETTY_NAME:-unknown}."

export DEBIAN_FRONTEND=noninteractive

log "Base packages"
apt-get update -q
apt-get install -y -q ca-certificates curl gnupg git rsync iproute2 unzip \
  build-essential nginx ffmpeg mysql-client

# --- Python 3.11+ -----------------------------------------------------------
python_ok() { "$1" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; }

if command -v python3 >/dev/null && python_ok python3; then
  log "Python $(python3 -V 2>&1 | cut -d' ' -f2) is new enough"
  apt-get install -y -q python3-venv python3-dev
else
  log "Python 3.12 from the deadsnakes PPA (this release ships $(python3 -V 2>&1 || echo none))"
  apt-get install -y -q software-properties-common
  add-apt-repository -y ppa:deadsnakes/ppa
  apt-get update -q
  apt-get install -y -q python3.12 python3.12-venv python3.12-dev
fi

# --- Node 20+ ---------------------------------------------------------------
node_major() { node --version 2>/dev/null | sed -E 's/^v([0-9]+).*/\1/'; }

if [ "$(node_major || echo 0)" -ge 20 ] 2>/dev/null; then
  log "Node $(node --version) is new enough"
else
  log "Node 22 from NodeSource"
  curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
  apt-get install -y -q nodejs
fi

# --- Bento4 mp4decrypt (optional) ---------------------------------------------
# Not packaged by Ubuntu. Only cbcs-protected channels need it; cenc is decrypted in process.
if [ "$WITH_BENTO4" -eq 1 ]; then
  if command -v mp4decrypt >/dev/null; then
    log "mp4decrypt is already installed"
  else
    log "Bento4 $BENTO4_VERSION"
    workdir="$(mktemp -d)"
    curl -fsSL -o "$workdir/bento4.zip" \
      "https://www.bok.net/Bento4/binaries/Bento4-SDK-${BENTO4_VERSION}.x86_64-unknown-linux.zip"
    unzip -q "$workdir/bento4.zip" -d "$workdir"
    install -m 755 "$workdir"/Bento4-SDK-*/bin/mp4decrypt /usr/local/bin/mp4decrypt
    rm -rf "$workdir"
  fi
fi

log "Done"
for tool in python3 python3.12 node npm nginx ffmpeg rsync mysql mp4decrypt; do
  if command -v "$tool" >/dev/null; then
    printf '  %-11s %s\n' "$tool" "$(command -v "$tool")"
  fi
done
echo "Next: sudo deploy/install.sh"
