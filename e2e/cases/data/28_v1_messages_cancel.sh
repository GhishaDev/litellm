#!/usr/bin/env bash
# Case 28 — /v1/messages (Anthropic native) streaming cancel.
#
# Verifies that the cancellation catch in
# litellm/proxy/common_request_processing.py:async_streaming_data_generator
# fires for the Anthropic-native /v1/messages endpoint and propagates
# the cancellation markers into the SpendLogs row (the row carries
# status="success" + cancellation_indicator under the binary-status
# taxonomy).
#
# The two endpoints (/v1/chat/completions and /v1/messages) share the
# cost-tracking pipeline downstream but enter through different
# generator functions. Earlier in Phase 1 the catch was in place but
# the logging_obj lookup
#   `getattr(response, "logging_obj", None)`
# returned None on the Anthropic path (the response object is a bare
# async iterator without that attribute), so mark_logging_obj_cancelled
# became a no-op and markers never reached the row.
#
# Phase 2 fix (this case re-enables): logging_obj falls back to
# `request_data["litellm_logging_obj"]` when not present on the
# response object. See the catch block in
# common_request_processing.py:async_streaming_data_generator.

set -eu

PROXY_URL="${PROXY_URL:-http://localhost:4011}"
DB_CONTAINER="${DB_CONTAINER:-litellm-e2e-db}"
DB_USER="${DB_USER:-litellm}"
DB_NAME="${DB_NAME:-litellm}"
MASTER_KEY="${MASTER_KEY:-sk-e2e-test}"
MOCK_CONTAINER="${MOCK_CONTAINER:-litellm-e2e-mock}"

if ! docker exec "$MOCK_CONTAINER" python3 -c \
        "import urllib.request; urllib.request.urlopen('http://localhost:8080/healthz')" 2>/dev/null; then
    echo "SKIP: $MOCK_CONTAINER not up (run with --with-mock)"
    exit 77
fi

USER_SENTINEL="case28-$(date +%s%N)"

echo "[28] /v1/messages streaming cancel..."
# Body uses Anthropic-shape: top-level system / messages, max_tokens
# required, no "user" field (that's OpenAI). Stream pacing chosen so
# we cancel after ~2s of streaming.
set +e
timeout 2 curl -sN -X POST "$PROXY_URL/v1/messages" \
    -H "Authorization: Bearer $MASTER_KEY" \
    -H "Content-Type: application/json" \
    -H "anthropic-version: 2023-06-01" \
    -H "X-Mock-TTFT-Ms: 100" \
    -H "X-Mock-Chunks: 100" \
    -H "X-Mock-TPS: 20" \
    -H "metadata: {\"user_id\": \"'$USER_SENTINEL'\"}" \
    -d "{
      \"model\": \"mock-anthropic\",
      \"messages\": [{\"role\": \"user\", \"content\": \"Explain cancellation in detail.\"}],
      \"max_tokens\": 2000,
      \"stream\": true,
      \"metadata\": {\"user_id\": \"$USER_SENTINEL\"}
    }" > /tmp/case28.out 2>&1
RC=$?
set -e
echo "  curl rc=$RC, bytes streamed: $(wc -c < /tmp/case28.out)"

# The Anthropic /v1/messages path stores the metadata.user_id in
# LiteLLM_SpendLogs.end_user — same column as the OpenAI `user` field.
ROW=""
for i in $(seq 1 30); do
    sleep 1
    ROW=$(docker exec "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -tA -F'|' -c "
SELECT
    COALESCE(status, ''),
    COALESCE(completion_tokens::text, '0'),
    COALESCE(metadata::jsonb->>'cancellation_indicator', ''),
    COALESCE(metadata::jsonb->>'cancel_phase', ''),
    COALESCE(metadata::jsonb->>'delivery_status', ''),
    COALESCE(metadata::jsonb->>'billing_status', '')
FROM \"LiteLLM_SpendLogs\"
WHERE end_user = '$USER_SENTINEL'
   OR metadata::jsonb->>'requester_metadata' LIKE '%$USER_SENTINEL%'
ORDER BY \"startTime\" DESC LIMIT 1;
" 2>/dev/null | head -1)
    [ -n "$ROW" ] && break
done

if [ -z "$ROW" ]; then
    # /v1/messages may not propagate the user field the same way —
    # fall back to "find the most recent row for mock-anthropic"
    echo "  (end_user lookup empty; falling back to most recent mock-anthropic row)"
    ROW=$(docker exec "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -tA -F'|' -c "
SELECT
    COALESCE(status, ''),
    COALESCE(completion_tokens::text, '0'),
    COALESCE(metadata::jsonb->>'cancellation_indicator', ''),
    COALESCE(metadata::jsonb->>'cancel_phase', ''),
    COALESCE(metadata::jsonb->>'delivery_status', ''),
    COALESCE(metadata::jsonb->>'billing_status', '')
FROM \"LiteLLM_SpendLogs\"
WHERE model_group = 'mock-anthropic' AND \"startTime\" > NOW() - INTERVAL '60 seconds'
ORDER BY \"startTime\" DESC LIMIT 1;
" 2>/dev/null | head -1)
fi

if [ -z "$ROW" ]; then
    echo "FAIL: no SpendLogs row found for /v1/messages cancel"
    exit 1
fi

IFS='|' read -r STATUS TOKENS IND PHASE DEL BIL <<< "$ROW"
echo "  row: status=$STATUS completion_tokens=$TOKENS ind=$IND phase=$PHASE delivery=$DEL billing=$BIL"

OK=1
# Binary-status taxonomy: cancel row carries status="success" + marker.
# /v1/messages is streaming → streaming_partial phase → delivery=partial,
# billing=partial.
if [ "$STATUS" != "success" ]; then
    echo "FAIL: expected status=success, got '$STATUS' (catch missing in common_request_processing.py?)"
    OK=0
fi
if [ "$IND" != "client_disconnect" ]; then
    echo "FAIL: expected cancellation_indicator=client_disconnect, got '$IND'"
    OK=0
fi
if [ "$DEL" != "partial" ]; then
    echo "FAIL: expected delivery_status=partial, got '$DEL'"
    OK=0
fi
if [ "$BIL" != "partial" ]; then
    echo "FAIL: expected billing_status=partial, got '$BIL'"
    OK=0
fi

if [ $OK -eq 1 ]; then
    echo "PASS: /v1/messages cancel routed through cancel_finalize"
    exit 0
else
    exit 1
fi
