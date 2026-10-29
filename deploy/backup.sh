#!/usr/bin/env bash
#
# Back up the StreamLens database: a consistent mysqldump, compressed, kept for a number of
# days, and copied to S3 when BACKUP_S3_URI is set (with the host's IAM role, never a key).
#
#   sudo -u rba deploy/backup.sh                      # reads /opt/rba/backend/.env
#   ENV_FILE=backend/.env BACKUP_DIR=./backups deploy/backup.sh
#
# Schedule it daily, e.g. /etc/cron.d/streamlens-backup:
#   15 2 * * * rba /opt/rba/deploy/backup.sh >> /var/log/streamlens-backup.log 2>&1
#
# Object storage (reports, recordings, archives) is not dumped here: S3 versioning or
# replication on the bucket covers it. The password is passed through MYSQL_PWD, never on the
# command line, and nothing here prints it.
set -euo pipefail

ENV_FILE="${ENV_FILE:-/opt/rba/backend/.env}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/streamlens}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"
BACKUP_S3_URI="${BACKUP_S3_URI:-}"

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
fail() { log "error: $*"; exit 1; }

[ -r "$ENV_FILE" ] || fail "cannot read $ENV_FILE"
command -v mysqldump >/dev/null || fail "mysqldump is not installed (default-mysql-client)"

# Read only the DB_* names from the env file, without executing it.
read_env() { grep -E "^$1=" "$ENV_FILE" | tail -n 1 | cut -d= -f2- | sed -e 's/^"//' -e 's/"$//'; }
DB_ENGINE="$(read_env DB_ENGINE)"
DB_HOST="$(read_env DB_HOST)"
DB_PORT="$(read_env DB_PORT)"
DB_USER="$(read_env DB_USER)"
DB_NAME="$(read_env DB_NAME)"
case "${DB_ENGINE:-mysql}" in
  mysql|mariadb) ;;
  *) fail "DB_ENGINE=$DB_ENGINE: this script backs up MySQL/MariaDB only" ;;
esac
[ -n "$DB_HOST" ] && [ -n "$DB_USER" ] && [ -n "$DB_NAME" ] || fail "DB_HOST, DB_USER and DB_NAME must be set"

MYSQL_PWD="$(read_env DB_PASSWORD)"
export MYSQL_PWD

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
target="$BACKUP_DIR/${DB_NAME}-${stamp}.sql.gz"

log "dumping $DB_NAME from $DB_HOST to $target"
# --single-transaction: a consistent snapshot without locking InnoDB tables.
# --max-allowed-packet: report rows are LONGBLOBs.
mysqldump \
  --host="$DB_HOST" --port="${DB_PORT:-3306}" --user="$DB_USER" \
  --single-transaction --quick --routines --triggers --hex-blob \
  --max-allowed-packet=512M \
  "$DB_NAME" | gzip -6 > "$target.partial"
mv "$target.partial" "$target"
chmod 600 "$target"
log "wrote $(du -h "$target" | cut -f1)"

if [ -n "$BACKUP_S3_URI" ]; then
  command -v aws >/dev/null || fail "BACKUP_S3_URI is set and the aws CLI is not installed"
  aws s3 cp --only-show-errors --sse AES256 "$target" "${BACKUP_S3_URI%/}/$(basename "$target")"
  log "copied to ${BACKUP_S3_URI%/}/"
fi

find "$BACKUP_DIR" -name "${DB_NAME}-*.sql.gz" -type f -mtime "+$RETENTION_DAYS" -print -delete \
  | sed 's/^/removed /'
log "done"
