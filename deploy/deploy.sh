#!/usr/bin/env bash
# Pulse — deploy to the Hetzner box.
#
#   ./deploy/deploy.sh            # build, sync, migrate, restart
#   ./deploy/deploy.sh --no-build # skip the frontend build (backend-only change)
#
# The server is a plain rsync copy with no .git, matching the other apps on the
# box. Nothing builds on the server: it has 4 GB of RAM shared between six
# applications, and a Vite build there competes with live traffic.
set -euo pipefail

HOST=${PULSE_HOST:-89.167.8.178}
SSH_KEY=${PULSE_SSH_KEY:-/Users/dev/projects/Products/GoSumo/keys/hetzner_deploy_ed25519}
REMOTE=/opt/Pulse
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
# `pulse` is the classic post-deploy 500.
chown -R pulse:pulse /opt/Pulse

sudo -u pulse /opt/Pulse/.venv/bin/pip install -q -r /opt/Pulse/backend/requirements.txt

# Unit files. rsync puts them under /opt/Pulse/deploy/systemd, which is not
# where systemd looks — until this ran, editing a unit and deploying changed
# nothing, and the drift was invisible because `systemctl status` reports the
# copy in /etc while the reviewed one sits in the repo. R71's uvicorn drain
# window and R72's worker TimeoutStopSec were both committed, tested, deployed
# and inert for exactly that reason.
#
# Copied rather than symlinked: a symlink into /opt would make an rsync mid-run
# a live edit of an active unit. Ownership is set explicitly because the chown
# above just made the source pulse-owned, and a root-run unit file that a
# service account can write is a privilege escalation.
units_changed=0
unit_names=()
for source in /opt/Pulse/deploy/systemd/*.service /opt/Pulse/deploy/systemd/*.timer; do
  installed=/etc/systemd/system/$(basename "$source")
  unit_names+=("$(basename "$source")")
  if ! cmp -s "$source" "$installed"; then
    install -m 0644 -o root -g root "$source" "$installed"
    echo "installed $(basename "$source")"
    units_changed=1
  fi
done

# A unit can be out of step without its content differing: the file in /etc
# matches the repo, but systemd is still running what it loaded before somebody
# copied that file there by hand. `cmp` cannot see it — it compares two files,
# and systemd's loaded state is neither of them — so ask systemd instead.
#
# Not hypothetical, and not rare enough to leave out: pulse-beat was in exactly
# this state when the install loop above first ran. The hand-copy had already
# made the contents match, so the loop correctly installed nothing, and beat
# went on running a stale definition that no future deploy would ever have
# noticed.
if [ "$units_changed" -eq 0 ] && [ ${#unit_names[@]} -gt 0 ] &&
   systemctl show "${unit_names[@]}" -p NeedDaemonReload --value | grep -qx yes; then
  echo "systemd is running an outdated copy of a unit"
  units_changed=1
fi

# Only on a change: daemon-reload re-executes every generator on the box, and
# this runs on every deploy. The restarts below pick up whatever it loaded.
if [ "$units_changed" -eq 1 ]; then
  systemctl daemon-reload
fi

systemctl stop pulse-beat pulse-worker || true
cd /opt/Pulse/backend
sudo -u pulse env $(grep -E '^DATABASE_URL=' /opt/Pulse/.env | xargs) \
  /opt/Pulse/.venv/bin/alembic upgrade head
systemctl restart pulse-api pulse-web
systemctl start pulse-worker pulse-beat

systemctl --no-pager --lines=0 status pulse-api pulse-web pulse-worker pulse-beat \
  | grep -E 'pulse-|Active:'

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
echo "health check never came up — journalctl -u pulse-api -n 50" >&2
exit 1
REMOTE_SCRIPT

echo "==> done: https://pulse.doaide.com"
