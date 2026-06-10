#!/usr/bin/env bash
# Case 27 — Non-stream cancel → disconnect watcher + shield-and-wait
#           → row with status="success", delivery=none, billing=full.
#
# Verifies the disconnect-detection logic added in Phase 2 to
# common_request_processing.py:base_process_llm_request. Without
# this, non-stream cancellations were silent on the proxy side and
# billed as plain "success" rows.
#
# Strategy
# --------
# When the proxy is awaiting an LLM call, a background _disconnect_watcher
# task polls `request.is_disconnected()` every second. When the client
# closes the connection:
#
#   1. The watcher sets disconnect_flag["detected"]=True (it does NOT
#      cancel the LLM task — we want the upstream to complete so we
#      can bill real usage).
#   2. LLM call eventually returns with real upstream response.
#   3. The proxy tags both logging_obj instances (request-scoped and
#      response-scoped) with cancel markers
#      (phase=during_upstream, upstream_completed=True,
#      usage_source=upstream_completed_after_cancel).
#   4. Normal success_handler chain runs and writes the SpendLogs row
#      with status=success + cancellation_indicator marker + the real
#      upstream usage tokens.
#
# Tier: mock-only. Uses mock-anthropic with X-Mock-TTFT-Ms to delay
# the upstream response past the curl --max-time.

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
    echo "SKIP: $MOCK_CONTAINER not up (run with --with-mock)"
    exit 77
fi

USER_SENTINEL="case27-$(date +%s%N)"

# Send non-stream request with 1.5-second TTFT on the upstream mock.
# Cut the client at 0.5 seconds so cancel arrives while LiteLLM is in
# the middle of `await client.post(...)`. The proxy is started with
# LITELLM_CANCEL_SHIELD_TIMEOUT_S=2 (see docker-compose.yml). Shield
# should wait, mock returns at T+1.5s (inside the 2s budget), then
# the SpendLogs row gets the real upstream usage (billing=full).
echo "[27] non-stream cancel during upstream wait..."
set +e
timeout 0.5 curl -sS -X POST "$PROXY_URL/v1/chat/completions" \
    -H "Authorization: Bearer $MASTER_KEY" \
    -H "Content-Type: application/json" \
    -H "X-Mock-TTFT-Ms: 1500" \
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
    COALESCE(metadata::jsonb->>'upstream_completed', ''),
    COALESCE(metadata::jsonb->>'delivery_status', ''),
    COALESCE(metadata::jsonb->>'billing_status', '')
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

IFS='|' read -r STATUS TOKENS IND PHASE SRC UPCOMP DEL BIL <<< "$ROW"
echo "  row: status=$STATUS completion_tokens=$TOKENS ind=$IND phase=$PHASE src=$SRC up=$UPCOMP delivery=$DEL billing=$BIL"

OK=1
# Under the binary-status taxonomy the cancel row carries
# status="success" and the cancellation_indicator marker. Shield-success
# implies delivery_status="none" (nothing reached the client — non-stream)
# and billing_status="full" (upstream returned real usage AFTER cancel).
if [ "$STATUS" != "success" ]; then
    echo "FAIL: expected status=success, got '$STATUS'"
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
if [ "$DEL" != "none" ]; then
    echo "FAIL: expected delivery_status=none (non-stream, no chunks reached client), got '$DEL'"
    OK=0
fi
if [ "$BIL" != "full" ]; then
    echo "FAIL: expected billing_status=full (shield-success: upstream returned real usage), got '$BIL'"
    echo "      Got usage_source='$SRC'; needs to be upstream_completed_after_cancel for billing=full."
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
