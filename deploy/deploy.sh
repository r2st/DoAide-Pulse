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

# Unit files. rsync puts them under /opt/Herald/deploy/systemd, which is not
# where systemd looks — until this ran, editing a unit and deploying changed
# nothing, and the drift was invisible because `systemctl status` reports the
# copy in /etc while the reviewed one sits in the repo. R71's uvicorn drain
# window and R72's worker TimeoutStopSec were both committed, tested, deployed
# and inert for exactly that reason.
#
# Copied rather than symlinked: a symlink into /opt would make an rsync mid-run
# a live edit of an active unit. Ownership is set explicitly because the chown
# above just made the source herald-owned, and a root-run unit file that a
# service account can write is a privilege escalation.
units_changed=0
for source in /opt/Herald/deploy/systemd/*.service /opt/Herald/deploy/systemd/*.timer; do
  installed=/etc/systemd/system/$(basename "$source")
  if ! cmp -s "$source" "$installed"; then
    install -m 0644 -o root -g root "$source" "$installed"
    echo "installed $(basename "$source")"
    units_changed=1
  fi
done
# Only on a change: daemon-reload re-executes every generator on the box, and
# this runs on every deploy. The restarts below pick up whatever it loaded.
if [ "$units_changed" -eq 1 ]; then
  systemctl daemon-reload
fi

systemctl stop herald-beat herald-worker || true
cd /opt/Herald/backend
sudo -u herald env $(grep -E '^DATABASE_URL=' /opt/Herald/.env | xargs) \
  /opt/Herald/.venv/bin/alembic upgrade head
systemctl restart herald-api herald-web
systemctl start herald-worker herald-beat

systemctl --no-pager --lines=0 status herald-api herald-web herald-worker herald-beat \
  | grep -E 'herald-|Active:'

# Retry: this runs milliseconds after `systemctl restart`, so a single curl
# raced uvicorn's bind and failed an otherwise good deploy. The endpoint probes
# Postgres and Redis and answers 503 if either is down, so -f still turns a
# broken dependency into a failed deploy — which is the point of checking.
for attempt in $(seq 1 15); do
  if curl -fsS --max-time 5 http://172.18.0.1:3006/api/v1/health; then
    echo
    echo "health ok after ${attempt} attempt(s)"
    exit 0
  fi
  sleep 1
done
echo "health check never came up — journalctl -u herald-api -n 50" >&2
exit 1
REMOTE_SCRIPT

echo "==> done: https://herald.aiknol.com"
