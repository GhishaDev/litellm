#!/usr/bin/env bash
# Case 32 — Concurrent streaming cancellations: no task leak, every
# row written.
#
# Verifies the cancel-finalize plumbing under load. The unit test
# tests/test_litellm/litellm_core_utils/test_cancel_finalize.py
# covers single-request cases; this case fires N parallel requests
# and cancels all of them mid-stream, then asserts:
#   - all N SpendLogs rows exist (no black hole under contention)
#   - each row carries the success_partial taxonomy
#   - the proxy process didn't OOM or leak asyncio tasks (we
#     spot-check by looking at request-handler latency on a control
#     request after the burst)
#
# Tier: mock-only. Light enough for CI — 10 concurrent stream
# cancels, each ~2s.

set -eu

PROXY_URL="${PROXY_URL:-http://localhost:4011}"
DB_CONTAINER="${DB_CONTAINER:-litellm-e2e-db}"
DB_USER="${DB_USER:-litellm}"
DB_NAME="${DB_NAME:-litellm}"
MASTER_KEY="${MASTER_KEY:-sk-e2e-test}"
MOCK_CONTAINER="${MOCK_CONTAINER:-litellm-e2e-mock}"

if ! docker exec "$MOCK_CONTAINER" python3 -c \
        "import urllib.request; urllib.request.urlopen('http://localhost:8080/healthz')" 2>/dev/null; then
    echo "FAIL: $MOCK_CONTAINER not up. Start proxy with --with-mock."
    exit 1
fi

N=10
RUN_ID="case32-$(date +%s%N)"

echo "[32] firing $N concurrent streaming cancels..."

# Fire N concurrent curls, each cancelled after 2s.
# Use distinct end_user values so we can count rows per request.
pids=()
for i in $(seq 1 $N); do
    USER_SENTINEL="${RUN_ID}-${i}"
    (
        timeout 2 curl -sN -X POST "$PROXY_URL/v1/chat/completions" \
            -H "Authorization: Bearer $MASTER_KEY" \
            -H "Content-Type: application/json" \
            -H "X-Mock-TTFT-Ms: 100" \
            -H "X-Mock-Chunks: 80" \
            -H "X-Mock-TPS: 15" \
            -d "{
              \"model\":\"mock-anthropic\",
              \"user\":\"$USER_SENTINEL\",
              \"messages\":[{\"role\":\"user\",\"content\":\"concurrent cancel test $i\"}],
              \"stream\":true,
              \"max_tokens\":2000
            }" > /dev/null 2>&1
    ) &
    pids+=($!)
done

# Wait for all to complete (they'll all be killed by timeout 2)
for pid in "${pids[@]}"; do
    wait "$pid" 2>/dev/null || true
done
echo "  all $N curls completed (cancelled by timeout)"

# Poll for rows. Cancel writes can lag a few seconds, especially
# under contention.
EXPECTED=$N
ROW_COUNT=0
for i in $(seq 1 45); do
    sleep 1
    ROW_COUNT=$(docker exec "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -tA -c "
SELECT count(*)
FROM \"LiteLLM_SpendLogs\"
WHERE end_user LIKE '${RUN_ID}-%' AND status='success_partial';
" 2>/dev/null | head -1)
    if [ "${ROW_COUNT:-0}" -ge "$EXPECTED" ]; then
        echo "  $ROW_COUNT/$EXPECTED rows after ${i}s"
        break
    fi
done

OK=1
if [ "${ROW_COUNT:-0}" -lt "$EXPECTED" ]; then
    echo "FAIL: expected $EXPECTED success_partial rows, got ${ROW_COUNT:-0}"
    OK=0
fi

# Spot-check: after the cancel burst, the proxy should still respond
# normally to a quick health-check style request. If the asyncio task
# pool is leaked, this would hang or take much longer than the baseline.
CONTROL_START=$(date +%s%N)
HEALTH=$(curl -sS --max-time 5 -o /dev/null -w '%{http_code}' \
    "$PROXY_URL/health/liveliness" 2>/dev/null || echo "TIMEOUT")
CONTROL_END=$(date +%s%N)
CONTROL_MS=$(( (CONTROL_END - CONTROL_START) / 1000000 ))
echo "  post-burst health probe: HTTP $HEALTH in ${CONTROL_MS}ms"
if [ "$HEALTH" != "200" ]; then
    echo "FAIL: proxy unhealthy after cancel burst (task leak / hang)"
    OK=0
fi
if [ "$CONTROL_MS" -gt 3000 ]; then
    echo "FAIL: proxy responded slowly (${CONTROL_MS}ms) — possible task contention"
    OK=0
fi

# Verify all rows have the marker set (sample a few)
SAMPLED_MARKERS=$(docker exec "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -tA -c "
SELECT count(*)
FROM \"LiteLLM_SpendLogs\"
WHERE end_user LIKE '${RUN_ID}-%'
  AND metadata::jsonb->>'cancellation_indicator' = 'client_disconnect';
" 2>/dev/null | head -1)
echo "  rows with cancellation_indicator: $SAMPLED_MARKERS/$EXPECTED"
if [ "${SAMPLED_MARKERS:-0}" -lt "$EXPECTED" ]; then
    echo "FAIL: only $SAMPLED_MARKERS/$EXPECTED rows have cancellation_indicator"
    OK=0
fi

if [ $OK -eq 1 ]; then
    echo "PASS: $N concurrent cancels → $EXPECTED success_partial rows + healthy proxy"
    exit 0
else
    exit 1
fi
