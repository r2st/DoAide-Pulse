#!/usr/bin/env bash
# Herald — deploy to the Hetzner box.
#
#   ./deploy/deploy.sh            # build, sync, migrate, restart
#   ./deploy/deploy.sh --no-build # skip the frontend build (backend-only change)
#
# The server is a plain rsync copy with no .git, matching the other apps on the
# box. Nothing builds on the server: it has 4 GB of RAM shared between six
# applications, and a Vite build there competes with live traffic.
set -euo pipefail

HOST=${HERALD_HOST:-89.167.8.178}
SSH_KEY=${HERALD_SSH_KEY:-/Users/dev/projects/Products/GoSumo/keys/hetzner_deploy_ed25519}
REMOTE=/opt/Herald
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SSH=(ssh -i "$SSH_KEY" "root@$HOST")

cd "$REPO_ROOT"

if [[ ${1:-} != "--no-build" ]]; then
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
"${SSH[@]}" bash -euo pipefail <<'REMOTE_SCRIPT'
# rsync runs as root; leaving root-owned files under a service that runs as
# `herald` is the classic post-deploy 500.
chown -R herald:herald /opt/Herald

sudo -u herald /opt/Herald/.venv/bin/pip install -q -r /opt/Herald/backend/requirements.txt

systemctl stop herald-beat herald-worker || true
cd /opt/Herald/backend
sudo -u herald env $(grep -E '^DATABASE_URL=' /opt/Herald/.env | xargs) \
  /opt/Herald/.venv/bin/alembic upgrade head
systemctl restart herald-api herald-web
systemctl start herald-worker herald-beat

systemctl --no-pager --lines=0 status herald-api herald-web herald-worker herald-beat \
  | grep -E 'herald-|Active:'
curl -fsS http://172.18.0.1:3006/api/v1/health && echo
REMOTE_SCRIPT

echo "==> done: https://herald.aiknol.com"
