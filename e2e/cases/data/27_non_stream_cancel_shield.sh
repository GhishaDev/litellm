#!/usr/bin/env bash
# Case 27 — Non-stream cancel → shield-and-wait.
#
# Status: deferred (skipped 77). The shield-and-wait helper
# (litellm/litellm_core_utils/cancel_finalize.py::finalize_non_stream_cancel)
# is implemented and unit-tested, but no caller in the proxy is wired
# to detect non-stream client cancellation. In LiteLLM's current
# HTTP/1.1 + uvicorn architecture, non-stream cancel detection requires
# polling `request.is_disconnected()` from a background task — the
# `check_request_disconnection` function exists in proxy_server.py but
# has zero callers (it's dead code). Until that polling is wired up,
# non-stream cancels look like "request completed normally" from the
# LiteLLM side regardless of client state.
#
# What actually happens with a non-stream cancel today:
#   - client closes TCP connection
#   - LiteLLM continues awaiting the upstream response (no CancelledError)
#   - upstream returns
#   - LiteLLM tries to write response to closed socket → write fails silently
#   - SpendLogs row is written with status=success, real usage tokens,
#     NO cancellation markers. The billing is technically correct
#     (upstream charged us, we charged the user) but the row is
#     indistinguishable from a normal success.
#
# To turn this into a real test:
#   1. Re-introduce check_request_disconnection polling (was removed
#      upstream at some point; revive it as a Tier-C bug fix)
#   2. Wire its `llm_api_call_task.cancel()` to trigger
#      finalize_non_stream_cancel
#   3. Then this case should pass: status=success_partial,
#      cancellation_indicator=client_disconnect, usage_source=
#      upstream_completed_after_cancel
#
# Tracked separately in the billing-accuracy roadmap as Phase 2 work.

echo "SKIP: non-stream cancel detection not wired up (see header comment)."
exit 77

set -eu

PROXY_URL="${PROXY_URL:-http://localhost:4011}"
DB_CONTAINER="${DB_CONTAINER:-litellm-e2e-db}"
DB_USER="${DB_USER:-litellm}"
DB_NAME="${DB_NAME:-litellm}"
MASTER_KEY="${MASTER_KEY:-sk-e2e-test}"
MOCK_CONTAINER="${MOCK_CONTAINER:-litellm-e2e-mock}"

# Pre-flight
if ! docker exec "$MOCK_CONTAINER" python3 -c \
        "import urllib.request; urllib.request.urlopen('http://localhost:8080/healthz')" 2>/dev/null; then
    echo "FAIL: $MOCK_CONTAINER not up. Start proxy with --with-mock."
    exit 1
fi

USER_SENTINEL="case27-$(date +%s%N)"

# Send non-stream request with 3-second TTFT on the upstream mock.
# Cut the client at 1 second so cancel arrives while LiteLLM is in
# the middle of `await client.post(...)`. cancel_finalize should
# shield the upstream call, wait for it to return (~3s), then write
# the SpendLogs row from the real upstream response.
echo "[27] non-stream cancel during upstream wait..."
set +e
timeout 1 curl -sS -X POST "$PROXY_URL/v1/chat/completions" \
    -H "Authorization: Bearer $MASTER_KEY" \
    -H "Content-Type: application/json" \
    -H "X-Mock-TTFT-Ms: 3000" \
    -H "X-Mock-Full-Chars: 800" \
    -d '{
      "model":"mock-anthropic",
      "user":"'$USER_SENTINEL'",
      "messages":[{"role":"user","content":"Tell me about cancellation handling. Several sentences please."}],
      "stream":false,
      "max_tokens":500
    }' > /tmp/case27.out 2>&1
RC=$?
set -e
echo "  curl rc=$RC, bytes received: $(wc -c < /tmp/case27.out)"
# Expect rc=124 (timeout) — client cancelled before mock finished.

# Poll up to 30s (shield wait + spend log batch can take a few seconds).
ROW=""
for i in $(seq 1 30); do
    sleep 1
    ROW=$(docker exec "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -tA -F'|' -c "
SELECT
    COALESCE(status, ''),
    COALESCE(completion_tokens::text, '0'),
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

IFS='|' read -r STATUS TOKENS IND PHASE SRC UPCOMP <<< "$ROW"
echo "  row: status=$STATUS completion_tokens=$TOKENS ind=$IND phase=$PHASE src=$SRC up=$UPCOMP"

OK=1
if [ "$STATUS" != "success_partial" ]; then
    echo "FAIL: expected status=success_partial, got '$STATUS'"
    OK=0
fi
if [ "$IND" != "client_disconnect" ]; then
    echo "FAIL: expected cancellation_indicator=client_disconnect, got '$IND'"
    OK=0
fi
# Phase should be "during_upstream" for non-stream cancel. Tolerate
# "streaming_partial" too because the proxy may route through the
# streaming catch (the non-stream catch is harder to reach if the
# response is being processed in pieces).
if [ "$PHASE" != "during_upstream" ] && [ "$PHASE" != "streaming_partial" ]; then
    echo "FAIL: expected cancel_phase in (during_upstream, streaming_partial), got '$PHASE'"
    OK=0
fi
# upstream_completed_after_cancel is the success case for shield-wait;
# completion_tokens > 0 proves the real upstream response made it
# through. Pre-fix we'd have either no row at all or 0 tokens.
if [ "${TOKENS:-0}" -le 0 ]; then
    echo "FAIL: expected completion_tokens > 0 (shield should have caught upstream response), got '$TOKENS'"
    OK=0
fi

if [ $OK -eq 1 ]; then
    echo "PASS: all assertions"
    exit 0
else
    exit 1
fi
