#!/bin/bash
#
# mysql-backup.sh — dump each MySQL database to its own .sql, zip them, upload
# to an rclone remote, then clean up local temporary artifacts.
#
# Configured via /etc/mysql-backup/mysql-backup.conf (sourced).
#
set -euo pipefail

CONF_FILE="${MYSQL_BACKUP_CONF:-/etc/mysql-backup/mysql-backup.conf}"

if [ ! -r "$CONF_FILE" ]; then
    echo "[mysql-backup] config not readable: $CONF_FILE" >&2
    exit 2
fi
# shellcheck source=/dev/null
. "$CONF_FILE"

: "${MYSQL_HOST:=localhost}"
: "${MYSQL_USER:=root}"
: "${MYSQL_PASS:=password}"
: "${MYSQL_EXCLUDE:=information_schema performance_schema sys mysql}"
: "${RCLONE_REMOTE:=drive:MySQLBackup}"
: "${RCLONE_CONFIG:=}"
: "${BACKUP_DIR:=/var/lib/mysql-backup}"
: "${ZIP_PREFIX:=o2mysql}"

STAMP="$(date +%Y%m%d%H%M%S)"
WORK_DIR="$BACKUP_DIR/tmp/${ZIP_PREFIX}.${STAMP}"
ZIP_NAME="${ZIP_PREFIX}.${STAMP}.zip"
ZIP_PATH="$WORK_DIR/$ZIP_NAME"
RC=0

log() { echo "[mysql-backup] $*"; }

cleanup() {
    rc=$?
    log "cleanup: removing $WORK_DIR"
    rm -rf "$WORK_DIR"
    exit $rc
}
trap cleanup EXIT

mkdir -p "$WORK_DIR"
cd "$WORK_DIR"

log "started at $STAMP (host=$MYSQL_HOST user=$MYSQL_USER)"

MYSQL_BIN="${MYSQL_BIN:-mysql}"
MYSQLDUMP_BIN="${MYSQLDUMP_BIN:-mysqldump}"
ZIP_BIN="${ZIP_BIN:-zip}"
RCLONE_BIN="${RCLONE_BIN:-rclone}"

# Build exclude set (space-separated) into a grep -v pattern.
EXCLUDE_RE=""
for x in $MYSQL_EXCLUDE; do
    [ -n "$EXCLUDE_RE" ] && EXCLUDE_RE="$EXCLUDE_RE|"
    EXCLUDE_RE="$EXCLUDE_RE^$(printf '%s' "$x" | sed 's/[][\.\\*]/\\&/g')$"
done

log "querying databases on $MYSQL_HOST"
DATABASES="$("$MYSQL_BIN" -h"$MYSQL_HOST" -u"$MYSQL_USER" -p"$MYSQL_PASS" \
    -N -B -e "SHOW DATABASES;" 2>/dev/null \
    | grep -vE "$EXCLUDE_RE" || true)"

if [ -z "$DATABASES" ]; then
    log "no databases to back up (after exclusions)"
    exit 1
fi

dump_ok=1
for db in $DATABASES; do
    out="$WORK_DIR/$db.sql"
    log "dumping: $db"
    if ! "$MYSQLDUMP_BIN" --single-transaction --routines --triggers --events \
        -h"$MYSQL_HOST" -u"$MYSQL_USER" -p"$MYSQL_PASS" "$db" > "$out" 2>/dev/null; then
        log "FAILED to dump: $db"
        dump_ok=0
    fi
done

if [ "$dump_ok" -ne 1 ]; then
    log "one or more database dumps failed"
    exit 1
fi

sql_count=$(find "$WORK_DIR" -maxdepth 1 -name '*.sql' -type f | wc -l | tr -d ' ')
if [ "$sql_count" -lt 1 ]; then
    log "no .sql files produced; aborting"
    exit 1
fi
log "dumped $sql_count database(s)"

log "zipping into $ZIP_NAME"
if ! "$ZIP_BIN" -j -q "$ZIP_PATH" "$WORK_DIR"/*.sql; then
    log "zip failed"
    exit 1
fi

log "uploading $ZIP_NAME to rclone remote $RCLONE_REMOTE"
RCLONE_ARGS=(copy "$ZIP_PATH" "$RCLONE_REMOTE")
if [ -n "$RCLONE_CONFIG" ]; then
    RCLONE_ARGS=(--config "$RCLONE_CONFIG" "${RCLONE_ARGS[@]}")
fi
if ! "$RCLONE_BIN" "${RCLONE_ARGS[@]}"; then
    log "rclone upload failed"
    exit 1
fi

log "upload complete: $RCLONE_REMOTE/$ZIP_NAME"
# Success: cleanup runs via trap; local zip is NOT retained.
