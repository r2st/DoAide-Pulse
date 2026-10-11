#!/usr/bin/env bash
# Pulse — deploy to the Hetzner box.
#
#   ./deploy/deploy.sh            # build, sync, migrate, restart
#   ./deploy/deploy.sh --no-build # skip the frontend build (backend-only change)
#   ./deploy/deploy.sh --skip-preflight  # skip local preflight checks
#
# The server is a plain rsync copy with no .git, matching the other apps on the
# box. Nothing builds on the server: it has 4 GB of RAM shared between six
# applications, and a Vite build there competes with live traffic.
set -euo pipefail

HOST=${PULSE_HOST:-89.167.8.178}
SSH_KEY=${PULSE_SSH_KEY:-/Users/dev/projects/keys/hetzner_deploy_ed25519}
REMOTE=/opt/Pulse
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SSH=(ssh -i "$SSH_KEY" "root@$HOST")

cd "$REPO_ROOT"

SKIP_BUILD=0
SKIP_PREFLIGHT=0
for arg in "$@"; do
  case "$arg" in
    --no-build) SKIP_BUILD=1 ;;
    --skip-preflight) SKIP_PREFLIGHT=1 ;;
  esac
done

# ---- Preflight checks (local) --------------------------------------------
if [ "$SKIP_PREFLIGHT" -eq 0 ] && [ -x deploy/preflight.sh ]; then
  echo "==> running preflight checks"
  deploy/preflight.sh --env-only
  echo
fi

DEPLOY_COMMIT=$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")
DEPLOY_TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)
echo "==> deploying commit $DEPLOY_COMMIT at $DEPLOY_TIMESTAMP"

if [ "$SKIP_BUILD" -eq 0 ]; then
  echo "==> building frontend"
  (cd frontend && npm run build)
fi

echo "==> syncing to $HOST:$REMOTE"
# The excludes are also what protects them from --delete: .env, the server's
# venv and the beat schedule live only on the box and must survive a deploy.
rsync -az --delete \
  --exclude '.git' --exclude '.env' --exclude '.env.*' --exclude 'keys/' \
  --exclude '.venv/' --exclude 'node_modules/' --exclude '__pycache__/' \
  --exclude '*.pyc' --exclude '.pytest_cache/' --exclude '.ruff_cache/' \
  --exclude '*.egg-info/' --exclude 'celerybeat-schedule*' \
  -e "ssh -i $SSH_KEY" \
  ./ "root@$HOST:$REMOTE/"

# frontend/dist is gitignored, so rsync it explicitly.
rsync -az --delete -e "ssh -i $SSH_KEY" \
  frontend/dist/ "root@$HOST:$REMOTE/frontend/dist/"

echo "==> installing deps, migrating, restarting"
"${SSH[@]}" bash -euo pipefail <<REMOTE_SCRIPT
# rsync runs as root; leaving root-owned files under a service that runs as
# \`pulse\` is the classic post-deploy 500.
chown -R pulse:pulse /opt/Pulse

sudo -u pulse /opt/Pulse/.venv/bin/pip install -q -r /opt/Pulse/backend/requirements.txt

# Unit files. rsync puts them under /opt/Pulse/deploy/systemd, which is not
# where systemd looks — until this ran, editing a unit and deploying changed
# nothing, and the drift was invisible because \`systemctl status\` reports the
# copy in /etc while the reviewed one sits in the repo.
#
# Copied rather than symlinked: a symlink into /opt would make an rsync mid-run
# a live edit of an active unit. Ownership is set explicitly because the chown
# above just made the source pulse-owned, and a root-run unit file that a
# service account can write is a privilege escalation.
units_changed=0
unit_names=()
for source in /opt/Pulse/deploy/systemd/*.service /opt/Pulse/deploy/systemd/*.timer; do
  installed=/etc/systemd/system/\$(basename "\$source")
  unit_names+=("\$(basename "\$source")")
  if ! cmp -s "\$source" "\$installed"; then
    install -m 0644 -o root -g root "\$source" "\$installed"
    echo "installed \$(basename "\$source")"
    units_changed=1
  fi
done

if [ "\$units_changed" -eq 0 ] && [ \${#unit_names[@]} -gt 0 ] &&
   systemctl show "\${unit_names[@]}" -p NeedDaemonReload --value | grep -qx yes; then
  echo "systemd is running an outdated copy of a unit"
  units_changed=1
fi

if [ "\$units_changed" -eq 1 ]; then
  systemctl daemon-reload
fi

systemctl stop pulse-beat pulse-worker || true
cd /opt/Pulse/backend
DB_URL=\$(grep -E '^DATABASE_URL=' /opt/Pulse/.env | head -1 | cut -d= -f2-)

# Snapshot the current alembic revision before migrating, so a failed deploy
# can downgrade back to exactly where it was.
PREV_REV=\$(sudo -u pulse env DATABASE_URL="\$DB_URL" \
  /opt/Pulse/.venv/bin/alembic current 2>/dev/null | grep -oP '^[a-f0-9]+' || echo "unknown")
echo "pre-migration alembic revision: \$PREV_REV"

# Advisory lock prevents two overlapping deploys from running migrations
# concurrently. The lock id (737868) is arbitrary but fixed; pg_try_advisory_lock
# returns false (exit 1 from psql) when another session holds it.
if ! sudo -u pulse env DATABASE_URL="\$DB_URL" \
  /opt/Pulse/.venv/bin/python -c "
import os, sys
from sqlalchemy import create_engine, text
e = create_engine(os.environ['DATABASE_URL'])
with e.connect() as c:
    if not c.execute(text('SELECT pg_try_advisory_lock(737868)')).scalar():
        print('ERROR: another migration is running', file=sys.stderr)
        sys.exit(1)
    import subprocess
    r = subprocess.run(
        ['/opt/Pulse/.venv/bin/alembic', 'upgrade', 'head'],
        cwd='/opt/Pulse/backend',
        env={**os.environ},
    )
    c.execute(text('SELECT pg_advisory_unlock(737868)'))
    sys.exit(r.returncode)
"; then
  echo "migration failed — not restarting services" >&2
  echo "previous alembic revision was: \$PREV_REV" >&2
  echo "to roll back: ./deploy/rollback.sh --db-only \$PREV_REV" >&2
  systemctl start pulse-worker pulse-beat || true
  exit 1
fi

POST_REV=\$(sudo -u pulse env DATABASE_URL="\$DB_URL" \
  /opt/Pulse/.venv/bin/alembic current 2>/dev/null | grep -oP '^[a-f0-9]+' || echo "unknown")
echo "post-migration alembic revision: \$POST_REV"

# Record the deploy for traceability.
echo "$DEPLOY_COMMIT \$PREV_REV \$POST_REV $DEPLOY_TIMESTAMP" >> /opt/Pulse/.deploy-history

systemctl restart pulse-api pulse-web
systemctl start pulse-worker pulse-beat

systemctl --no-pager --lines=0 status pulse-api pulse-web pulse-worker pulse-beat \
  | grep -E 'pulse-|Active:'

for attempt in \$(seq 1 15); do
  if curl -fsS --max-time 5 http://172.18.0.1:3006/api/v1/health; then
    echo
    echo "health ok after \${attempt} attempt(s)"
    exit 0
  fi
  sleep 1
done
echo "health check never came up — journalctl -u pulse-api -n 50" >&2
echo "previous alembic revision was: \$PREV_REV" >&2
echo "to roll back: ./deploy/rollback.sh --db-only \$PREV_REV" >&2
exit 1
REMOTE_SCRIPT

echo "==> done: https://pulse.doaide.com (commit $DEPLOY_COMMIT)"
