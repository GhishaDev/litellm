#!/usr/bin/env bash
# Case 29 — Real upstream errors must classify as status=failure,
# NOT success_partial.
#
# Verifies the failure-path branch in
# litellm/proxy/hooks/proxy_track_cost_callback.py:async_post_call_failure_hook:
#
#   _is_cancel = isinstance(original_exception, asyncio.CancelledError)
#   _metadata["status"] = "success_partial" if _is_cancel else "failure"
#
# Without this guard, my Phase 1 work could accidentally classify
# real provider 5xx as success_partial — corrupting the failure rate
# metric on every dashboard. This case forces a 503 from the mock
# and asserts the SpendLogs row stays classified as "failure".
#
# Tier: mock-only. Uses X-Mock-Fail to make the mock return 503.

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

USER_SENTINEL="case29-$(date +%s%N)"

echo "[29] forced upstream 503 → should be classified as failure (not success_partial)..."
set +e
HTTP_CODE=$(curl -sS -o /tmp/case29.out -w '%{http_code}' \
    -X POST "$PROXY_URL/v1/chat/completions" \
    -H "Authorization: Bearer $MASTER_KEY" \
    -H "Content-Type: application/json" \
    -H "X-Mock-Fail: 503" \
    -d '{
      "model":"mock-anthropic",
      "user":"'$USER_SENTINEL'",
      "messages":[{"role":"user","content":"this will fail"}],
      "stream":false,
      "max_tokens":100
    }')
set -e
echo "  HTTP status from proxy: $HTTP_CODE, bytes received: $(wc -c < /tmp/case29.out)"

# Poll for the spend_logs row
ROW=""
for i in $(seq 1 30); do
    sleep 1
    ROW=$(docker exec "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -tA -F'|' -c "
SELECT
    COALESCE(status, ''),
    COALESCE(metadata::jsonb->>'cancellation_indicator', ''),
    COALESCE(metadata::jsonb->'error_information'->>'error_class', ''),
    COALESCE(metadata::jsonb->'error_information'->>'error_code', ''),
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

IFS='|' read -r STATUS IND ERR_CLASS ERR_CODE DEL BIL <<< "$ROW"
echo "  row: status=$STATUS ind=$IND error_class=$ERR_CLASS error_code=$ERR_CODE delivery=$DEL billing=$BIL"

OK=1
# THIS is the keystone assertion. The whole point of the refactor is
# that "cancel" (status=success + marker) doesn't leak into "failure"
# (status=failure + no marker). A real upstream 5xx must land cleanly
# in the failure bucket — otherwise we've corrupted the failure-rate
# dashboards.
if [ "$STATUS" != "failure" ]; then
    echo "FAIL: real upstream error was classified as '$STATUS', expected 'failure'"
    echo "      This means CancelledError detection in"
    echo "      proxy_track_cost_callback.async_post_call_failure_hook"
    echo "      is matching non-cancel exceptions too — the cancel-billing"
    echo "      taxonomy is being applied to real provider errors."
    OK=0
fi
# Real errors must NOT carry the cancellation marker.
if [ -n "$IND" ]; then
    echo "FAIL: real upstream error wrote cancellation_indicator='$IND' — should be empty"
    OK=0
fi
# Derived dimensions must both be "none" for a real failure. If either
# came back as "partial" or "full" we've corrupted the taxonomy
# (failure rows should never imply we delivered or billed anything).
if [ "$DEL" != "none" ]; then
    echo "FAIL: real failure leaked delivery_status='$DEL' (expected 'none')"
    echo "      → cancel taxonomy bleeding into the failure path"
    OK=0
fi
if [ "$BIL" != "none" ]; then
    echo "FAIL: real failure leaked billing_status='$BIL' (expected 'none')"
    OK=0
fi
# Confirm there IS an error_class recorded (the row should still get
# proper error metadata, just under the failure classification).
if [ -z "$ERR_CLASS" ]; then
    echo "FAIL: error_class empty — failure-path metadata population broken"
    OK=0
fi

if [ $OK -eq 1 ]; then
    echo "PASS: 503 upstream error correctly classified as status=failure, delivery=none, billing=none"
    exit 0
else
    exit 1
fi
