#!/usr/bin/env bash
# Regression fixture for Case 11 — error_information.error_message must be populated.
#
# When auth fails (e.g. invalid virtual key), the spend_logs row's
# metadata.error_information.error_message must contain the human-readable
# error string ("Authentication Error, Invalid proxy server token..."),
# not an empty string. Dashboard "LLM Failure" rows are unusable as a
# triage signal without it.
#
# Verifies against the running e2e proxy + Postgres.

set -eu

PROXY_URL="${PROXY_URL:-http://localhost:4011}"
DB_CONTAINER="${DB_CONTAINER:-litellm-e2e-db}"
DB_USER="${DB_USER:-litellm}"
DB_NAME="${DB_NAME:-litellm}"

# Unique sentinel key so we can find exactly this request's spend_logs row
SENTINEL_KEY="sk-case11-$(date +%s%N)"
HASH=$(printf '%s' "$SENTINEL_KEY" | sha256sum | awk '{print $1}')

# 1. Trigger the 401
HTTP_CODE=$(curl -sS -o /dev/null -w '%{http_code}' \
    -X POST "$PROXY_URL/v1/chat/completions" \
    -H "Authorization: Bearer $SENTINEL_KEY" \
    -H "Content-Type: application/json" \
    -d '{"model":"x","messages":[{"role":"user","content":"hi"}]}')
if [ "$HTTP_CODE" != "401" ]; then
    echo "FAIL: expected HTTP 401, got $HTTP_CODE"
    exit 1
fi

# 2. Wait for async spend logger to land
sleep 2

# 3. Read the row stored for this key hash
ROW=$(docker exec "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -tA -F'|' -c "
SELECT
    COALESCE(metadata::jsonb->'error_information'->>'error_code',    ''),
    COALESCE(metadata::jsonb->'error_information'->>'error_class',   ''),
    COALESCE(metadata::jsonb->'error_information'->>'error_message', ''),
    COALESCE(length(metadata::jsonb->'error_information'->>'traceback')::text, '0')
FROM \"LiteLLM_SpendLogs\"
WHERE api_key = '$HASH'
ORDER BY \"startTime\" DESC
LIMIT 1;
")
if [ -z "$ROW" ]; then
    echo "FAIL: no spend_logs row found for hash $HASH"
    exit 1
fi

IFS='|' read -r CODE CLASS MSG TB_LEN <<< "$ROW"

echo "stored error_code:    '$CODE'"
echo "stored error_class:   '$CLASS'"
echo "stored error_message: '$MSG'"
echo "stored traceback len: $TB_LEN"

FAIL=0
if [ "$CODE" != "401" ]; then
    echo "FAIL: expected error_code=401, got '$CODE'"
    FAIL=1
fi
if [ "$CLASS" != "ProxyException" ]; then
    echo "FAIL: expected error_class=ProxyException, got '$CLASS'"
    FAIL=1
fi
if [ -z "$MSG" ]; then
    echo "FAIL: error_message is EMPTY — this is the regression we guard against."
    echo "      Traceback length is $TB_LEN (>0 means the path that should "
    echo "      populate error_message did run); only the message itself is lost."
    FAIL=1
elif ! echo "$MSG" | grep -qE "Authentication Error|Invalid proxy server token"; then
    echo "FAIL: error_message present but missing expected substring."
    echo "      got: $MSG"
    FAIL=1
fi
if [ "$TB_LEN" -lt 100 ]; then
    echo "FAIL: traceback unexpectedly short ($TB_LEN chars) — logging path may be partially broken"
    FAIL=1
fi

if [ "$FAIL" -eq 0 ]; then
    echo "PASS: error_information populated correctly"
    exit 0
else
    exit 1
fi
