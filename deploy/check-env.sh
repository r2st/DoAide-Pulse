#!/usr/bin/env bash
# Pulse — compare local and server environment configuration.
#
#   ./deploy/check-env.sh          # diff .env keys between local and server
#
# Catches config drift: a key added to .env.example locally that was never
# added to the server's .env, or a server-only key that the codebase no longer
# reads. Only key *names* are compared — values are never printed or
# transmitted, so this is safe to run against production.
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

cd "$REPO_ROOT"

echo "Pulse environment parity check"
echo "==============================="
echo

# ---- Gather key names (never values) from each source ---------------------
example_keys=$(grep -E '^[A-Z_]+=.' .env.example 2>/dev/null | sed 's/=.*//' | sort -u)
local_keys=$(grep -E '^[A-Z_]+=.' .env 2>/dev/null | sed 's/=.*//' | sort -u)
server_keys=$("${SSH[@]}" "grep -E '^[A-Z_]+=.' $REMOTE/.env 2>/dev/null | sed 's/=.*//' | sort -u")

errors=0

# ---- .env.example vs local .env ------------------------------------------
echo "Local .env vs .env.example:"
missing_local=$(comm -23 <(echo "$example_keys") <(echo "$local_keys"))
if [ -n "$missing_local" ]; then
    printf "  ${RED}missing locally:${NC}\n"
    echo "$missing_local" | sed 's/^/    /'
    errors=$((errors + 1))
else
    printf "  ${GREEN}✓ all example keys present locally${NC}\n"
fi

echo

# ---- .env.example vs server .env ------------------------------------------
echo "Server .env vs .env.example:"
missing_server=$(comm -23 <(echo "$example_keys") <(echo "$server_keys"))
if [ -n "$missing_server" ]; then
    printf "  ${RED}missing on server:${NC}\n"
    echo "$missing_server" | sed 's/^/    /'
    errors=$((errors + 1))
else
    printf "  ${GREEN}✓ all example keys present on server${NC}\n"
fi

echo

# ---- local .env vs server .env --------------------------------------------
echo "Local .env vs server .env:"
only_local=$(comm -23 <(echo "$local_keys") <(echo "$server_keys"))
only_server=$(comm -13 <(echo "$local_keys") <(echo "$server_keys"))

if [ -n "$only_local" ]; then
    printf "  ${YELLOW}only in local .env:${NC}\n"
    echo "$only_local" | sed 's/^/    /'
fi
if [ -n "$only_server" ]; then
    printf "  ${YELLOW}only on server .env:${NC}\n"
    echo "$only_server" | sed 's/^/    /'
fi
if [ -z "$only_local" ] && [ -z "$only_server" ]; then
    printf "  ${GREEN}✓ same keys in both environments${NC}\n"
fi

echo

# ---- Server Python + pip versions -----------------------------------------
echo "Server runtime versions:"
"${SSH[@]}" bash -euo pipefail <<'REMOTE_SCRIPT'
echo "  python: $(/opt/Pulse/.venv/bin/python --version 2>&1)"
echo "  pip:    $(/opt/Pulse/.venv/bin/pip --version 2>&1 | cut -d' ' -f1-2)"
echo "  alembic: $(/opt/Pulse/.venv/bin/alembic --version 2>&1 | head -1)"
REMOTE_SCRIPT

echo
if [ "$errors" -gt 0 ]; then
    printf "${RED}DRIFT DETECTED${NC}\n"
    exit 1
else
    printf "${GREEN}ENVIRONMENTS IN SYNC${NC}\n"
fi
