#!/usr/bin/env bash
# Pulse — nightly database backup.
#
#   ./deploy/backup.sh                    # dump to /var/backups/pulse
#   PULSE_BACKUP_DIR=/tmp/drill ./deploy/backup.sh   # restore drill
#
# Run by `pulse-backup.timer`. Until this existed the only backups Pulse had
# were the ad-hoc `pg_dump` in deploy/DEPLOYMENT.md's rollback section, taken by
# hand "before anything risky" — which meant the newest copy of the database was
# whenever somebody last remembered. Deleting a piece is a hard `DELETE` (see
# `routers.content.delete_content`), so a mistake there had nothing to recover
# from.
#
# Two limits worth knowing rather than discovering:
#
#   * The dumps land on the same disk as the database. That covers operator
#     error and corruption; it does not cover losing the box. Copying $DEST
#     off-host is a separate job and is not done yet.
#   * Only the database is dumped. `frontend/dist` and the venv come back from
#     a deploy, but anything written into /opt/Pulse at runtime does not.
#
# Postgres is shared with the other products on this box. This dumps one
# database, by name — never `pg_dumpall`, which would put GSTBot's and
# HomeNex's data in a file owned by Pulse's service account.
set -euo pipefail

# Every knob is an environment variable with a default, so the unit file can
# stay declarative and a one-off restore drill can point the script at a
# scratch directory without editing it.
ENV_FILE="${PULSE_ENV_FILE:-/opt/Pulse/.env}"
DEST="${PULSE_BACKUP_DIR:-/var/backups/pulse}"
RETENTION_DAYS="${PULSE_BACKUP_RETENTION_DAYS:-14}"
# A dump that fills the disk takes out six products, not one. This is the floor
# the dump refuses to start below, in MiB.
MIN_FREE_MB="${PULSE_BACKUP_MIN_FREE_MB:-1024}"

log() { printf '%s\n' "$*"; }
die() { printf '\033[31merror: %s\033[0m\n' "$*" >&2; exit 1; }

# ---- Connection details ---------------------------------------------------
# Under systemd the unit's EnvironmentFile has already put DATABASE_URL in the
# environment. By hand it has not, so fall back to reading the one line out of
# the env file rather than sourcing it — /opt/Pulse/.env is systemd's format,
# not shell, and sourcing it would execute anything a future value contained.
if [ -z "${DATABASE_URL:-}" ]; then
    [ -r "$ENV_FILE" ] || die "$ENV_FILE is not readable and DATABASE_URL is unset"
    DATABASE_URL="$(sed -n 's/^DATABASE_URL=//p' "$ENV_FILE" | tail -n 1)"
    [ -n "$DATABASE_URL" ] || die "no DATABASE_URL in $ENV_FILE"
fi

command -v pg_dump >/dev/null || die "pg_dump is missing (apt install postgresql-client)"
command -v pg_restore >/dev/null || die "pg_restore is missing (apt install postgresql-client)"
command -v python3 >/dev/null || die "python3 is missing; it parses DATABASE_URL below"

# Split the URL with a real URL parser rather than a regex: the password is
# percent-encoded in there, and a `@` or `%` in it is the kind of thing that
# silently produces a wrong host at 03:30 and an empty backup directory by the
# time anyone looks. The SQLAlchemy driver suffix (`+psycopg`) is ignored.
#
# The result is exported as libpq's PG* variables instead of being passed to
# pg_dump as a URI, so the password never appears in the argv of a process on a
# box six products can read `ps` on.
connection="$(DATABASE_URL="$DATABASE_URL" python3 - <<'PY'
import os
import shlex
import sys
from urllib.parse import unquote, urlsplit

url = urlsplit(os.environ["DATABASE_URL"])
database = url.path.lstrip("/")
if not url.hostname or not database:
    sys.exit("DATABASE_URL names no host or no database")

# unquote: libpq wants the decoded value, and urlsplit hands back what was in
# the URL. A password written `p%40ss` is `p@ss` to the server, and passing the
# encoded form through authenticates as the wrong string.
for name, value in (
    ("PGHOST", url.hostname),
    ("PGPORT", str(url.port or 5432)),
    ("PGUSER", unquote(url.username or "")),
    ("PGPASSWORD", unquote(url.password or "")),
    ("PGDATABASE", unquote(database)),
):
    print(f"export {name}={shlex.quote(value)}")
PY
)" || die "DATABASE_URL in $ENV_FILE is not a URL this can dump"
eval "$connection"

# ---- Preflight ------------------------------------------------------------
[ -d "$DEST" ] || die "$DEST does not exist; see 'Backups' in deploy/DEPLOYMENT.md"
[ -w "$DEST" ] || die "$DEST is not writable by $(id -un)"

free_mb="$(df -Pm "$DEST" | awk 'NR == 2 { print $4 }')"
[ "${free_mb:-0}" -ge "$MIN_FREE_MB" ] \
    || die "only ${free_mb}MiB free on $DEST, below the ${MIN_FREE_MB}MiB floor"

# ---- Dump -----------------------------------------------------------------
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
target="$DEST/${PGDATABASE}-${stamp}.dump"
partial="$target.partial"
# Written under a .partial name and renamed only once pg_restore has read it
# back, so the retention sweep below can treat every *.dump as restorable and
# an interrupted run leaves nothing that looks like a backup.
trap 'rm -f "$partial"' EXIT

umask 077
log "Dumping $PGDATABASE from $PGHOST:$PGPORT"
# --format=custom, not plain SQL: it compresses, and it is the only format
# pg_restore can read selectively — restoring one table out of it during an
# incident does not mean replaying the whole database.
pg_dump --format=custom --compress=9 --no-owner --no-privileges --file="$partial" \
    || die "pg_dump failed; the previous dumps in $DEST are untouched"

# A dump that exits 0 can still be truncated by a full disk. Reading the table
# of contents back is cheap and is the difference between having backups and
# believing you do.
pg_restore --list "$partial" >/dev/null \
    || die "pg_dump wrote $partial but pg_restore cannot read it"

mv "$partial" "$target"
log "Wrote $target ($(du -h "$target" | cut -f1))"

# ---- Retention ------------------------------------------------------------
# Only reached after a dump that verified, so this cannot leave the directory
# empty — unless RETENTION_DAYS is 0, which would delete the file just written
# and is treated as a configuration error rather than a policy.
[ "$RETENTION_DAYS" -ge 1 ] || die "PULSE_BACKUP_RETENTION_DAYS must be at least 1"
find "$DEST" -maxdepth 1 -type f -name '*.dump' -mtime "+$RETENTION_DAYS" -print -delete \
    | sed 's/^/Pruned /'

kept="$(find "$DEST" -maxdepth 1 -type f -name '*.dump' | wc -l)"
log "$kept dump(s) in $DEST, keeping ${RETENTION_DAYS} days"
