#!/usr/bin/env bash
# Regression fixture for Case 25 — backend auth error.type contract.
#
# Wave 6a added `_classify_auth_failure` to
# litellm/proxy/auth/auth_exception_handler.py — it emits a structured
# `error.type` field in 401/403 response bodies so the UI can route on
# the failure category (PR #68 layered the frontend to read it).
#
# Without an e2e lock, a future upstream refactor of the auth pipeline
# could silently stop emitting these structured types — unit tests on
# the classifier itself would still pass, but end-to-end the UI would
# fall back to substring heuristics for every 401/403. This case asserts
# the wire contract directly so that regression is caught.
#
# 4 probes, one per ProxyErrorTypes:
#   A1 no Authorization header        → auth_invalid_credentials
#   A2 malformed (no sk- prefix)      → auth_invalid_credentials
#   A3 sk- key absent from DB         → token_not_found_in_db
#   A4 non-admin role + admin route   → auth_permission_denied
#
# All four currently return HTTP 401 (Bedrock-style 403 is recognized by
# the classifier but the proxy's auth pipeline raises 401 even for
# permission denials — that's an upstream quirk we don't try to
# rationalize). The assertion is on `error.type`, not the status code.

set -eu

PROXY_URL="${PROXY_URL:-http://localhost:4011}"
MASTER_KEY="${MASTER_KEY:-sk-e2e-test}"

BODY='{"model":"mock-openai","messages":[{"role":"user","content":"hi"}]}'

# Helper: POST $BODY with given Authorization header (or none), echo body.
probe() {
    local auth_arg="$1" route="$2"
    if [ -n "$auth_arg" ]; then
        curl -sSL -X POST "$PROXY_URL$route" \
            -H "Authorization: Bearer $auth_arg" \
            -H "Content-Type: application/json" \
            -d "$BODY"
    else
        curl -sSL -X POST "$PROXY_URL$route" \
            -H "Content-Type: application/json" \
            -d "$BODY"
    fi
}

assert_error_type() {
    local label="$1" expected="$2" payload="$3"
    local actual
    actual=$(printf '%s' "$payload" | python3 -c "import json,sys; print((json.load(sys.stdin).get('error') or {}).get('type',''))" 2>/dev/null || echo "")
    if [ "$actual" = "$expected" ]; then
        echo "PASS: $label → error.type=$actual"
        return 0
    else
        echo "FAIL: $label expected error.type=$expected, got '$actual'. Full body:"
        printf '%s\n' "$payload" | head -c 500
        echo ""
        return 1
    fi
}

fails=0

# A1: no Authorization header at all.
r=$(probe "" /v1/chat/completions)
assert_error_type "A1 no-auth-header" "auth_invalid_credentials" "$r" || fails=$((fails+1))

# A2: malformed key (doesn't start with sk-).
r=$(probe "notavalidkey" /v1/chat/completions)
assert_error_type "A2 malformed-key" "auth_invalid_credentials" "$r" || fails=$((fails+1))

# A3: well-formed sk- key but not in the DB cache or VerificationTokenTable.
r=$(probe "sk-doesnotexist-case25" /v1/chat/completions)
assert_error_type "A3 bogus-sk-key" "token_not_found_in_db" "$r" || fails=$((fails+1))

# A4: authenticated user without admin role hits an admin-only route.
# Provision an internal_user-role key via the master key, then have it
# call /key/generate (admin-only).
internal_key=$(curl -sSL -X POST "$PROXY_URL/key/generate" \
    -H "Authorization: Bearer $MASTER_KEY" \
    -H "Content-Type: application/json" \
    -d '{"user_role":"internal_user"}' \
    | python3 -c "import json,sys; print(json.load(sys.stdin).get('key',''))" 2>/dev/null)
if [ -z "$internal_key" ]; then
    echo "FAIL: A4 setup — could not provision internal_user key"
    fails=$((fails+1))
else
    r=$(curl -sSL -X POST "$PROXY_URL/key/generate" \
        -H "Authorization: Bearer $internal_key" \
        -H "Content-Type: application/json" \
        -d '{}')
    assert_error_type "A4 internal-user-on-admin-route" "auth_permission_denied" "$r" || fails=$((fails+1))
fi

if [ "$fails" -eq 0 ]; then
    echo "PASS: all 4 auth error.type contract probes hit expected values"
    exit 0
fi
echo "FAIL: $fails of 4 probes did not match contract"
exit 1
