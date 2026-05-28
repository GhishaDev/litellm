#!/usr/bin/env bash
# Case 22 fixture — see e2e/cases/22_gemini_credential_custom_api_base.md
#
# Asserts that a deployment with `litellm_params.model = gemini/...`
# plus a custom `api_base` routes through the gemini provider and the
# x-litellm-model-api-base header surfaces the custom URL (proving the
# UI api_base field added to the Google_AI_Studio credential form
# actually reaches the runtime).
#
# Exits 0 on PASS, 77 on SKIP, anything else on FAIL.

set -u

PROXY="${PROXY_URL:-http://localhost:4011}"
KEY="${MASTER_KEY:-sk-e2e-test}"
MODEL="gemini-custom-base"
FAILED=0

pass() { echo "PASS: $*"; }
fail() { echo "FAIL: $*"; FAILED=1; }
skip() { echo "SKIP: $*"; exit 77; }

# Pull GEMINI_API_BASE from .env so we can assert the response header
# matches. We don't need the key here — the proxy holds it.
ENV_FILE="$(dirname "$0")/../../.env"
if [ -f "$ENV_FILE" ]; then
    # Source the value without exporting other vars unintentionally.
    EXPECTED_API_BASE=$(
        grep -E '^GEMINI_API_BASE=' "$ENV_FILE" | tail -1 | sed -E 's/^GEMINI_API_BASE=//; s/^"//; s/"$//; s/^'"'"'//; s/'"'"'$//'
    )
else
    EXPECTED_API_BASE=""
fi

if ! curl -sSf -o /dev/null -m 3 "$PROXY/health/readiness"; then
    skip "proxy not ready at $PROXY"
fi

# If the proxy didn't render the deployment, GEMINI_API_KEY was unset.
if ! curl -sSf -m 5 -H "Authorization: Bearer $KEY" "$PROXY/v1/models" \
        2>/dev/null | grep -q '"id"[[:space:]]*:[[:space:]]*"'"$MODEL"'"'; then
    skip "deployment '$MODEL' missing from /v1/models — set GEMINI_API_KEY (and optionally GEMINI_API_BASE / MODEL_GEMINI) in e2e/.env, then re-run e2e/tools/proxy restart"
fi

if [ -z "$EXPECTED_API_BASE" ]; then
    skip "GEMINI_API_BASE not set in e2e/.env — case 22 needs a custom api_base to assert against"
fi

# ---- C1: chat/completions with the custom-api_base gemini deployment ----
read -r -d '' BODY <<JSON || true
{"model":"$MODEL","max_tokens":8,"messages":[{"role":"user","content":"hi"}]}
JSON

C1_BODY=$(mktemp); C1_HDRS=$(mktemp)
C1_STATUS=$(curl -sS -D "$C1_HDRS" -o "$C1_BODY" -w "%{http_code}" \
    -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
    -d "$BODY" "$PROXY/v1/chat/completions")

if [ "$C1_STATUS" != "200" ]; then
    fail "HTTP $C1_STATUS (expected 200)"
    echo "--- body (truncated) ---"
    head -c 600 "$C1_BODY"; echo
    rm -f "$C1_BODY" "$C1_HDRS"
    exit $FAILED
fi

# Extract headers we care about. tr -d '\r' because curl preserves CRLF.
ACTUAL_API_BASE=$(grep -i '^x-litellm-model-api-base:' "$C1_HDRS" \
    | tail -1 | tr -d '\r' | sed -E 's/^[^:]+:[[:space:]]*//')
ACTUAL_GROUP=$(grep -i '^x-litellm-model-group:' "$C1_HDRS" \
    | tail -1 | tr -d '\r' | sed -E 's/^[^:]+:[[:space:]]*//')

if [ "$ACTUAL_API_BASE" = "$EXPECTED_API_BASE" ]; then
    pass "HTTP 200, x-litellm-model-api-base=$ACTUAL_API_BASE"
else
    fail "x-litellm-model-api-base mismatch: expected='$EXPECTED_API_BASE', got='$ACTUAL_API_BASE'"
fi

if [ "$ACTUAL_GROUP" = "$MODEL" ]; then
    pass "x-litellm-model-group=$ACTUAL_GROUP"
else
    fail "x-litellm-model-group mismatch: expected='$MODEL', got='$ACTUAL_GROUP'"
fi

# Body shape sanity — gemini provider returns OpenAI-compatible JSON via LiteLLM.
if python3 -c "import json,sys; d=json.load(open('$C1_BODY')); assert isinstance(d.get('choices'), list) and len(d['choices'])>=1" 2>/dev/null; then
    pass "body has 'choices' array"
else
    fail "body missing or malformed 'choices' array"
    echo "--- body (truncated) ---"
    head -c 600 "$C1_BODY"; echo
fi

rm -f "$C1_BODY" "$C1_HDRS"
exit $FAILED
