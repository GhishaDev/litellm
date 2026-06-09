#!/usr/bin/env bash
# Case 30 — Non-stream cancel + shield_timeout path.
#
# Status: deferred (skipped 77). Same root cause as case 27: the
# finalize_non_stream_cancel helper (including its shield-timeout
# branch) is implemented and unit-tested, but no proxy code path
# triggers it because non-stream cancel detection isn't wired up.
# Until check_request_disconnection polling is revived (see case 27
# header for the full explanation), the shield_timeout branch is
# unreachable from a real HTTP request and this case cannot pass
# deterministically.

echo "SKIP: shield_timeout path not reachable until non-stream cancel polling is wired (see case 27)."
exit 77

# --- Original case 30 implementation below (kept for reference, runs
# --- after the early exit above is removed once Phase 2 lands the
# --- polling task in proxy_server.py).
#
# Verifies the LITELLM_CANCEL_SHIELD_TIMEOUT_S env var and the
# fallback branch in finalize_non_stream_cancel:
#
#   except asyncio.TimeoutError:
#       details["usage_source"] = "shield_timeout"
#       upstream_task.cancel()
#       await _fallback_to_failure_hook(...)
#
# To drive this deterministically in CI we configure the proxy with
# a very short shield timeout (set via env at proxy start) and a mock
# response slower than that. The test asserts the SpendLogs row shows
# the shield_timeout source rather than upstream_completed_after_cancel.
#
# Tier: mock-only. Requires LITELLM_CANCEL_SHIELD_TIMEOUT_S=2 set on
# the proxy container.

set -eu

PROXY_URL="${PROXY_URL:-http://localhost:4011}"
DB_CONTAINER="${DB_CONTAINER:-litellm-e2e-db}"
DB_USER="${DB_USER:-litellm}"
DB_NAME="${DB_NAME:-litellm}"
MASTER_KEY="${MASTER_KEY:-sk-e2e-test}"
MOCK_CONTAINER="${MOCK_CONTAINER:-litellm-e2e-mock}"
PROXY_CONTAINER="${PROXY_CONTAINER:-litellm-e2e}"

# Check that the proxy was started with a short shield timeout.
SHIELD_TIMEOUT=$(docker exec "$PROXY_CONTAINER" \
    printenv LITELLM_CANCEL_SHIELD_TIMEOUT_S 2>/dev/null || echo "")
if [ -z "$SHIELD_TIMEOUT" ] || python3 -c "import sys; sys.exit(0 if float('$SHIELD_TIMEOUT') < 30 else 1)" 2>/dev/null; then
    : # short timeout is set, can proceed
else
    echo "SKIP: LITELLM_CANCEL_SHIELD_TIMEOUT_S not set or >=30s on proxy container."
    echo "      Restart with: LITELLM_CANCEL_SHIELD_TIMEOUT_S=2 e2e/tools/proxy start --with-mock"
    exit 77
fi
echo "  proxy LITELLM_CANCEL_SHIELD_TIMEOUT_S=$SHIELD_TIMEOUT s"

USER_SENTINEL="case30-$(date +%s%N)"

echo "[30] non-stream cancel + shield_timeout..."
# Mock takes 8 seconds (well over the 2s shield). Client cancels at 1s.
# Expected sequence:
#   T+0   client POST
#   T+1   client times out → CancelledError into proxy
#   T+1   cancel_finalize starts shield + wait_for(upstream, timeout=2s)
#   T+3   shield wait_for raises asyncio.TimeoutError
#         → usage_source=shield_timeout, upstream_task.cancel()
#         → fallback to failure hook → row classified success_partial
#           via the CancelledError branch in async_post_call_failure_hook
set +e
timeout 1 curl -sS -X POST "$PROXY_URL/v1/chat/completions" \
    -H "Authorization: Bearer $MASTER_KEY" \
    -H "Content-Type: application/json" \
    -H "X-Mock-TTFT-Ms: 8000" \
    -H "X-Mock-Full-Chars: 300" \
    -d '{
      "model":"mock-anthropic",
      "user":"'$USER_SENTINEL'",
      "messages":[{"role":"user","content":"shield timeout test"}],
      "stream":false,
      "max_tokens":300
    }' > /tmp/case30.out 2>&1
RC=$?
set -e
echo "  curl rc=$RC, bytes received: $(wc -c < /tmp/case30.out)"

# Poll up to 30s — shield waits, then upstream completes silently in
# background, then spend_log gets batched.
ROW=""
for i in $(seq 1 30); do
    sleep 1
    ROW=$(docker exec "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -tA -F'|' -c "
SELECT
    COALESCE(status, ''),
    COALESCE(metadata::jsonb->>'cancellation_indicator', ''),
    COALESCE(metadata::jsonb->>'cancel_phase', ''),
    COALESCE(metadata::jsonb->>'usage_source', ''),
    COALESCE(metadata::jsonb->>'upstream_completed', '')
FROM \"LiteLLM_SpendLogs\"
WHERE end_user = '$USER_SENTINEL'
ORDER BY \"startTime\" DESC LIMIT 1;
" 2>/dev/null | head -1)
    [ -n "$ROW" ] && break
done

if [ -z "$ROW" ]; then
    echo "FAIL: no SpendLogs row for $USER_SENTINEL after 30s"
    exit 1
fi

IFS='|' read -r STATUS IND PHASE SRC UPCOMP <<< "$ROW"
echo "  row: status=$STATUS ind=$IND phase=$PHASE src=$SRC up=$UPCOMP"

OK=1
if [ "$STATUS" != "success_partial" ]; then
    echo "FAIL: expected status=success_partial, got '$STATUS'"
    OK=0
fi
if [ "$IND" != "client_disconnect" ]; then
    echo "FAIL: expected cancellation_indicator=client_disconnect, got '$IND'"
    OK=0
fi
if [ "$SRC" != "shield_timeout" ]; then
    echo "FAIL: expected usage_source=shield_timeout, got '$SRC'"
    echo "      (Shield budget = ${SHIELD_TIMEOUT}s; mock TTFT=8000ms — shield should have"
    echo "       given up before upstream returned. If src=upstream_completed_after_cancel,"
    echo "       the shield timeout knob isn't being honored.)"
    OK=0
fi

if [ $OK -eq 1 ]; then
    echo "PASS: shield_timeout path correctly recorded"
    exit 0
else
    exit 1
fi
