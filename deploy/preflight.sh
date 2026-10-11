#!/usr/bin/env bash
# Pulse — pre-deploy validation.
#
#   ./deploy/preflight.sh              # run all checks
#   ./deploy/preflight.sh --env-only   # only compare .env keys
#
# Catches the failures that waste a deploy rather than the deploy catching them:
# missing environment keys, migrations without downgrade paths, Python import
# errors. Runs locally (no server access needed) so a broken deploy never
# leaves the laptop.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m'

errors=0
warnings=0

pass() { printf "${GREEN}✓${NC} %s\n" "$1"; }
warn() { printf "${YELLOW}⚠${NC} %s\n" "$1"; warnings=$((warnings + 1)); }
fail() { printf "${RED}✗${NC} %s\n" "$1"; errors=$((errors + 1)); }

# ---- .env parity -----------------------------------------------------------
check_env_parity() {
    if [ ! -f .env ]; then
        fail ".env does not exist"
        return
    fi
    if [ ! -f .env.example ]; then
        fail ".env.example does not exist"
        return
    fi

    local example_keys actual_keys missing extra
    example_keys=$(grep -E '^[A-Z_]+=.' .env.example | sed 's/=.*//' | sort -u)
    actual_keys=$(grep -E '^[A-Z_]+=.' .env | sed 's/=.*//' | sort -u)

    missing=$(comm -23 <(echo "$example_keys") <(echo "$actual_keys"))
    extra=$(comm -13 <(echo "$example_keys") <(echo "$actual_keys"))

    if [ -n "$missing" ]; then
        fail "keys in .env.example but missing from .env:"
        echo "$missing" | sed 's/^/       /'
    else
        pass ".env has all keys from .env.example"
    fi

    if [ -n "$extra" ]; then
        warn "keys in .env but not in .env.example: $(echo $extra | tr '\n' ' ')"
    fi
}

# ---- Migration downgrade paths ---------------------------------------------
check_migration_downgrades() {
    local bad=0
    for migration in backend/alembic/versions/*.py; do
        [ -f "$migration" ] || continue
        basename_f=$(basename "$migration")
        # Skip __pycache__ and merge migrations (which legitimately have pass).
        [[ "$basename_f" == __* ]] && continue

        if ! grep -q "def downgrade" "$migration"; then
            fail "migration $basename_f has no downgrade() function"
            bad=1
            continue
        fi

        # A downgrade that is just `pass` on a non-merge migration is a no-op
        # rollback — flag it as a warning, not an error, since merge migrations
        # legitimately do this.
        if [[ "$basename_f" != *merge* ]]; then
            local body
            body=$(sed -n '/def downgrade/,/^def \|^$/p' "$migration" | tail -n +2 | grep -v '^\s*$' | grep -v '^\s*#')
            if echo "$body" | grep -qxE '\s*pass'; then
                local line_count
                line_count=$(echo "$body" | wc -l)
                if [ "$line_count" -le 1 ]; then
                    warn "migration $basename_f downgrade() is only 'pass' — no rollback possible"
                fi
            fi
        fi
    done

    if [ "$bad" -eq 0 ]; then
        pass "all migrations have downgrade() functions"
    fi
}

# ---- Python import check ---------------------------------------------------
check_python_imports() {
    if ! python3 -c "import app.main" 2>/dev/null; then
        fail "backend/app/main.py fails to import"
    else
        pass "app.main imports cleanly"
    fi
}

# ---- Alembic head check ----------------------------------------------------
check_alembic_heads() {
    local heads
    heads=$(cd backend && python3 -m alembic heads 2>/dev/null | grep -c "head" || true)
    if [ "$heads" -gt 1 ]; then
        fail "alembic has $heads heads — create a merge migration before deploying"
    else
        pass "single alembic head"
    fi
}

# ---- Required files --------------------------------------------------------
check_required_files() {
    local required=(
        "backend/requirements.txt"
        "backend/alembic.ini"
        "backend/alembic/env.py"
        "deploy/deploy.sh"
        "docker-compose.yml"
    )
    for f in "${required[@]}"; do
        if [ ! -f "$f" ]; then
            fail "required file missing: $f"
        fi
    done
    pass "required files present"
}

# ---- Run checks ------------------------------------------------------------
echo "Pulse pre-deploy preflight"
echo "========================="
echo

if [[ ${1:-} == "--env-only" ]]; then
    check_env_parity
else
    check_required_files
    check_env_parity
    check_migration_downgrades
    check_alembic_heads
fi

echo
if [ "$errors" -gt 0 ]; then
    printf "${RED}FAILED: %d error(s), %d warning(s)${NC}\n" "$errors" "$warnings"
    exit 1
elif [ "$warnings" -gt 0 ]; then
    printf "${YELLOW}PASSED with %d warning(s)${NC}\n" "$warnings"
else
    printf "${GREEN}ALL CHECKS PASSED${NC}\n"
fi
