#!/usr/bin/env bash
# Regression fixture for Case 15 — /v1/models honors user.models.
#
# When the requesting user has a non-empty `LiteLLM_UserTable.models`
# (Personal Models), `GET /v1/models` must return ONLY the subset the
# user is allowed to call. Before the fix the endpoint ignored
# user.models entirely and listed every proxy model, even though
# /v1/chat/completions for those models returned 401.
#
# See BerriAI/litellm#26420.
#
# Exit codes: 0 PASS, 77 SKIP (e.g. preconditions missing), else FAIL.

set -u

PROXY_URL="${PROXY_URL:-http://localhost:4011}"
MASTER_KEY="${MASTER_KEY:-sk-e2e-test}"
SUFFIX="case15-$(date +%s%N)"

USER_RESTRICTED=""
USER_BLOCKED=""
USER_OPEN=""
KEY_RESTRICTED=""
KEY_BLOCKED=""
KEY_OPEN=""

H_AUTH=(-H "Authorization: Bearer $MASTER_KEY")
H_JSON=(-H "Content-Type: application/json")

cleanup() {
    for K in "$KEY_RESTRICTED" "$KEY_BLOCKED" "$KEY_OPEN"; do
        [ -n "$K" ] && curl -sS "${H_AUTH[@]}" "${H_JSON[@]}" \
            "$PROXY_URL/key/delete" \
            -d "{\"keys\":[\"$K\"]}" > /dev/null 2>&1
    done
    for U in "$USER_RESTRICTED" "$USER_BLOCKED" "$USER_OPEN"; do
        [ -n "$U" ] && curl -sS "${H_AUTH[@]}" "${H_JSON[@]}" \
            "$PROXY_URL/user/delete" \
            -d "{\"user_ids\":[\"$U\"]}" > /dev/null 2>&1
    done
}
trap cleanup EXIT

fail() { echo "FAIL: $*"; exit 1; }

create_user() {
    # $1 = label, $2 = JSON array literal for models field
    local label="$1" models="$2"
    local resp
    resp=$(curl -sS "${H_AUTH[@]}" "${H_JSON[@]}" "$PROXY_URL/user/new" \
        -d "{\"user_alias\":\"$SUFFIX-$label\",\"models\":$models,\"auto_create_key\":false}")
    local uid
    uid=$(printf '%s' "$resp" | jq -r '.user_id // empty')
    [ -n "$uid" ] || fail "user/new($label) returned no user_id. body=$resp"
    printf '%s' "$uid"
}

mint_key_for_user() {
    # $1 = user_id, $2 = alias-suffix
    local user_id="$1" alias_suffix="$2"
    local resp
    resp=$(e2e/tools/keys new --user-id "$user_id" \
        --alias "$SUFFIX-$alias_suffix" 2>&1)
    local k
    k=$(printf '%s' "$resp" | jq -r '.response.key // empty' 2>/dev/null)
    [ -n "$k" ] || fail "keys new for user=$user_id returned no key. body=$resp"
    printf '%s' "$k"
}

list_models_ids() {
    # $1 = bearer token. Sorted, newline-delimited.
    curl -sS -H "Authorization: Bearer $1" "$PROXY_URL/v1/models" \
        | jq -r '.data[].id // empty' | LC_ALL=C sort
}

# ---- 1. Master baseline -----------------------------------------------------
MASTER_LIST=$(list_models_ids "$MASTER_KEY")
MASTER_COUNT=$(printf '%s\n' "$MASTER_LIST" | grep -c .)
if [ "$MASTER_COUNT" -lt 1 ]; then
    echo "SKIP: proxy has 0 configured models — case requires at least 1"
    exit 77
fi
FIRST_MODEL=$(printf '%s\n' "$MASTER_LIST" | head -n 1)

# ---- 2. Restricted user → exactly {first_model} -----------------------------
USER_RESTRICTED=$(create_user restricted "[\"$FIRST_MODEL\"]")
KEY_RESTRICTED=$(mint_key_for_user "$USER_RESTRICTED" restricted-key)
RESTRICTED_LIST=$(list_models_ids "$KEY_RESTRICTED")

if [ "$RESTRICTED_LIST" != "$FIRST_MODEL" ]; then
    fail "user restricted to [$FIRST_MODEL] must see only that. got: $(printf '%s' "$RESTRICTED_LIST" | tr '\n' ',')"
fi

# ---- 3. no-default-models → empty -------------------------------------------
USER_BLOCKED=$(create_user blocked '["no-default-models"]')
KEY_BLOCKED=$(mint_key_for_user "$USER_BLOCKED" blocked-key)
BLOCKED_COUNT=$(curl -sS -H "Authorization: Bearer $KEY_BLOCKED" \
    "$PROXY_URL/v1/models" | jq -r '.data | length')
if [ "$BLOCKED_COUNT" != "0" ]; then
    fail "no-default-models sentinel must produce empty list; got count=$BLOCKED_COUNT"
fi

# ---- 4. Open user (models=[]) → matches master -------------------------------
USER_OPEN=$(create_user open '[]')
KEY_OPEN=$(mint_key_for_user "$USER_OPEN" open-key)
OPEN_LIST=$(list_models_ids "$KEY_OPEN")
if [ "$OPEN_LIST" != "$MASTER_LIST" ]; then
    fail "unrestricted user must match master. master=$(printf '%s' "$MASTER_LIST" | tr '\n' ','), open=$(printf '%s' "$OPEN_LIST" | tr '\n' ',')"
fi

# ---- 5. Master snapshot unchanged --------------------------------------------
MASTER_LIST_AFTER=$(list_models_ids "$MASTER_KEY")
if [ "$MASTER_LIST" != "$MASTER_LIST_AFTER" ]; then
    fail "master snapshot drifted during the test"
fi

echo "PASS: /v1/models honors user.models (restricted=1, blocked=0, open=$MASTER_COUNT)"
exit 0
