#!/usr/bin/env bash
# Pulse — rollback to a previous deploy.
#
#   ./deploy/rollback.sh                    # rollback to previous git commit
#   ./deploy/rollback.sh <commit>           # rollback to a specific commit
#   ./deploy/rollback.sh --db-only <rev>    # only downgrade the database to an alembic revision
#   ./deploy/rollback.sh --list-backups     # list available database backups
#
# The rollback is a full redeploy of the specified commit: it syncs the old
# code, downgrades the database if needed, and restarts services. The database
# backup taken before migration (by deploy.sh) is kept as a safety net.
set -euo pipefail

HOST=${PULSE_HOST:-89.167.8.178}
SSH_KEY=${PULSE_SSH_KEY:-/Users/dev/projects/keys/hetzner_deploy_ed25519}
REMOTE=/opt/Pulse
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SSH=(ssh -i "$SSH_KEY" "root@$HOST")

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m'

die() { printf "${RED}error: %s${NC}\n" "$*" >&2; exit 1; }

# ---- List backups ----------------------------------------------------------
if [[ ${1:-} == "--list-backups" ]]; then
    echo "Available database backups on $HOST:"
    "${SSH[@]}" 'ls -lh /var/backups/pulse/*.dump 2>/dev/null || echo "  (none found)"'
    exit 0
fi

# ---- DB-only downgrade -----------------------------------------------------
if [[ ${1:-} == "--db-only" ]]; then
    TARGET_REV=${2:?usage: rollback.sh --db-only <alembic-revision>}
    echo "==> downgrading database to alembic revision $TARGET_REV"
    "${SSH[@]}" bash -euo pipefail <<REMOTE_SCRIPT
systemctl stop pulse-beat pulse-worker || true
cd /opt/Pulse/backend
DB_URL=\$(grep -E '^DATABASE_URL=' /opt/Pulse/.env | head -1 | cut -d= -f2-)
sudo -u pulse env DATABASE_URL="\$DB_URL" /opt/Pulse/.venv/bin/alembic downgrade $TARGET_REV
systemctl restart pulse-api
systemctl start pulse-worker pulse-beat
REMOTE_SCRIPT
    printf "${GREEN}database downgraded to %s${NC}\n" "$TARGET_REV"
    exit 0
fi

# ---- Full rollback ---------------------------------------------------------
TARGET_COMMIT=${1:-HEAD~1}
cd "$REPO_ROOT"

CURRENT_COMMIT=$(git rev-parse HEAD)
ROLLBACK_COMMIT=$(git rev-parse "$TARGET_COMMIT")

if [ "$CURRENT_COMMIT" = "$ROLLBACK_COMMIT" ]; then
    die "target commit $TARGET_COMMIT is the current HEAD — nothing to roll back"
fi

printf "${YELLOW}Rolling back from %.8s to %.8s${NC}\n" "$CURRENT_COMMIT" "$ROLLBACK_COMMIT"
echo "Current:  $(git log --oneline -1 "$CURRENT_COMMIT")"
echo "Rollback: $(git log --oneline -1 "$ROLLBACK_COMMIT")"
echo

read -r -p "Proceed? [y/N] " confirm
[[ "$confirm" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 0; }

echo "==> checking out $ROLLBACK_COMMIT"
git stash -u --quiet 2>/dev/null || true
git checkout "$ROLLBACK_COMMIT" --quiet

echo "==> building frontend at rollback commit"
(cd frontend && npm run build) || die "frontend build failed at rollback commit"

echo "==> syncing rollback to $HOST:$REMOTE"
rsync -az --delete \
    --exclude '.git' --exclude '.env' --exclude '.env.*' --exclude 'keys/' \
    --exclude '.venv/' --exclude 'node_modules/' --exclude '__pycache__/' \
    --exclude '*.pyc' --exclude '.pytest_cache/' --exclude '.ruff_cache/' \
    --exclude '*.egg-info/' --exclude 'celerybeat-schedule*' \
    -e "ssh -i $SSH_KEY" \
    ./ "root@$HOST:$REMOTE/"

rsync -az --delete -e "ssh -i $SSH_KEY" \
    frontend/dist/ "root@$HOST:$REMOTE/frontend/dist/"

echo "==> restarting services with rollback code"
"${SSH[@]}" bash -euo pipefail <<'REMOTE_SCRIPT'
chown -R pulse:pulse /opt/Pulse
sudo -u pulse /opt/Pulse/.venv/bin/pip install -q -r /opt/Pulse/backend/requirements.txt

systemctl stop pulse-beat pulse-worker || true

cd /opt/Pulse/backend
DB_URL=$(grep -E '^DATABASE_URL=' /opt/Pulse/.env | head -1 | cut -d= -f2-)
sudo -u pulse env DATABASE_URL="$DB_URL" /opt/Pulse/.venv/bin/alembic upgrade head

systemctl restart pulse-api pulse-web
systemctl start pulse-worker pulse-beat

for attempt in $(seq 1 15); do
    if curl -fsS --max-time 5 http://172.18.0.1:3006/api/v1/health; then
        echo
        echo "health ok after rollback"
        exit 0
    fi
    sleep 1
done
echo "health check failed after rollback" >&2
exit 1
REMOTE_SCRIPT

echo "==> returning to original HEAD"
git checkout "$CURRENT_COMMIT" --quiet
git stash pop --quiet 2>/dev/null || true

printf "${GREEN}Rollback complete.${NC} Server is running %.8s\n" "$ROLLBACK_COMMIT"
echo "To make this permanent, revert the commits and redeploy."
