#!/usr/bin/env bash
# Regression fixture for Case 16 — reset_budget_windows tick must not
# raise prisma.errors.MissingRequiredValueError.
#
# Verifies the cherry-pick of BerriAI/litellm#26346 lives on this branch:
# the periodic ResetBudgetJob.reset_budget_windows tick must complete
# without the Prisma engine rejecting the `Json?` null-filter.
#
# Mechanic: seed one key + one team with `budget_limits` set, wait long
# enough for at least one reset_budget_job tick (interval is forced to
# 10–15 s by the e2e docker-compose override), then grep the proxy
# container's logs from the start of the case forward for the error
# signature. Empty grep = PASS.
#
# Exit codes: 0 PASS, 77 SKIP, anything else FAIL.

set -u

PROXY_URL="${PROXY_URL:-http://localhost:4011}"
MASTER_KEY="${MASTER_KEY:-sk-e2e-test}"
CONTAINER="${PROXY_CONTAINER:-litellm-e2e}"
SUFFIX="case16-$(date +%s%N)"

KEY=""

H_AUTH=(-H "Authorization: Bearer $MASTER_KEY")
H_JSON=(-H "Content-Type: application/json")

cleanup() {
    if [ -n "$KEY" ]; then
        curl -sS "${H_AUTH[@]}" "${H_JSON[@]}" "$PROXY_URL/key/delete" \
            -d "{\"keys\":[\"$KEY\"]}" > /dev/null 2>&1
    fi
}
trap cleanup EXIT

fail() { echo "FAIL: $*"; exit 1; }

# ---- Preflight ---------------------------------------------------------
# The proxy container must exist (so we can read its logs at the end).
if ! docker inspect "$CONTAINER" > /dev/null 2>&1; then
    echo "SKIP: docker container '$CONTAINER' not found — start the proxy first (e2e/tools/proxy start)"
    exit 77
fi

# Confirm the e2e reset-interval override is in effect on the container.
# Without it, the default ~600 s interval means no tick fires inside our
# observation window and the case can't actually verify anything. SKIP
# (don't FAIL) so an out-of-date container doesn't masquerade as a fix
# regression.
MIN_TIME=$(docker inspect -f \
    '{{range .Config.Env}}{{println .}}{{end}}' "$CONTAINER" 2>/dev/null \
    | awk -F= '/^PROXY_BUDGET_RESCHEDULER_MIN_TIME=/ {print $2; exit}')
if [ -z "$MIN_TIME" ] || [ "$MIN_TIME" -gt 60 ] 2>/dev/null; then
    echo "SKIP: container '$CONTAINER' has PROXY_BUDGET_RESCHEDULER_MIN_TIME='${MIN_TIME:-unset}' (> 60 s)."
    echo "      Run 'e2e/tools/proxy restart' to pick up the e2e defaults (10/15 s)."
    exit 77
fi

# ---- 1. Log cursor -----------------------------------------------------
# `docker logs --since` accepts an RFC3339 timestamp. Sleep one second
# first so the cursor is provably in the past once we start emitting work
# (microsecond-precision clock differences between host & container can
# otherwise cause "since" to start *after* our work).
SINCE_TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
sleep 1

# ---- 2. Seed a key with budget_limits ----------------------------------
# We only need ONE row in LiteLLM_VerificationToken with budget_limits
# IS NOT NULL to make the reset job hit the failing path. The bug
# (RobertCraigie/prisma-client-py#714) is symmetric between the key
# and team paths in `reset_budget_windows`; the team path is covered by
# unit tests (`test_reset_budget_windows_resets_expired_team_window`,
# `test_reset_budget_windows_query_error_does_not_break_team_path`) so
# this case stays focused on the key path and avoids an unrelated
# Prisma serialization bug in `/team/new`'s `budget_limits` handling.
KEY_RESP=$(curl -sS "${H_AUTH[@]}" "${H_JSON[@]}" "$PROXY_URL/key/generate" \
    -d "{\"key_alias\":\"$SUFFIX-key\",\"budget_limits\":[{\"max_budget\":100.0,\"budget_duration\":\"1d\"}]}")
KEY=$(printf '%s' "$KEY_RESP" | jq -r '.key // empty')
[ -n "$KEY" ] || fail "key/generate returned no key. body=$KEY_RESP"

# ---- 3. Wait for ≥ 2 reset_budget_job ticks ----------------------------
# Interval is 10–15 s under the e2e override. 35 s = 2–3 guaranteed ticks
# plus headroom for an APScheduler misfire-grace startup delay.
sleep 35

# ---- 4. Grep container logs for the bug signature ----------------------
LOGS=$(docker logs --since "$SINCE_TS" "$CONTAINER" 2>&1)
if [ -z "$LOGS" ]; then
    fail "docker logs --since '$SINCE_TS' '$CONTAINER' returned nothing — log cursor may be in the future, or container exited."
fi

# Match either the raw Prisma exception class or the exception-wrapper
# log line our fix-site `try/except` emits.
HITS=$(printf '%s\n' "$LOGS" | grep -E \
    "MissingRequiredValueError|Failed to reset budget windows" || true)
if [ -n "$HITS" ]; then
    echo "FAIL: reset_budget_windows raised after waiting ≥ 2 ticks:"
    printf '%s\n' "$HITS" | head -20 | sed 's/^/    /'
    exit 1
fi

echo "PASS: reset_budget_windows completed silently across ≥ 2 ticks (key=$KEY)"
exit 0
