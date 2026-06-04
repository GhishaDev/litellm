#!/usr/bin/env bash
# Regression fixture for Case 17 — /v1/model/info and /v2/model/info
# must honor LiteLLM_UserTable.models (Personal Models).
#
# Closes the same discovery-vs-inference gap that PR #10 closed for
# /v1/models, extended to the two info endpoints which previously
# leaked the full deployment list (including litellm_params.api_base).
#
# Exit codes: 0 PASS, 77 SKIP (e.g. preconditions missing), else FAIL.

set -u

PROXY_URL="${PROXY_URL:-http://localhost:4011}"
MASTER_KEY="${MASTER_KEY:-sk-e2e-test}"
SUFFIX="case17-$(date +%s%N)"

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
    local user_id="$1" alias_suffix="$2"
    local resp
    resp=$(e2e/tools/keys new --user-id "$user_id" \
        --alias "$SUFFIX-$alias_suffix" 2>&1)
    local k
    k=$(printf '%s' "$resp" | jq -r '.response.key // empty' 2>/dev/null)
    [ -n "$k" ] || fail "keys new for user=$user_id returned no key. body=$resp"
    printf '%s' "$k"
}

# Distinct model_name set from a model_info endpoint, sorted, NL-delim.
list_model_names() {
    # $1 = bearer token, $2 = endpoint path (e.g. "/v1/model/info")
    curl -sS -H "Authorization: Bearer $1" "$PROXY_URL$2" \
        | jq -r '[.data[]?.model_name] | unique | .[]' \
        | LC_ALL=C sort
}

# Total data length on a model_info endpoint.
count_data() {
    curl -sS -H "Authorization: Bearer $1" "$PROXY_URL$2" \
        | jq -r '.data | length'
}

# The three endpoints we need to verify in lockstep.
ENDPOINTS=("/v1/model/info" "/v2/model/info" "/v2/model/info?include_team_models=true")

# ---- 1. Master baseline ----------------------------------------------------
MASTER_V1MODELS=$(curl -sS "${H_AUTH[@]}" "$PROXY_URL/v1/models" \
    | jq -r '.data[].id' | LC_ALL=C sort)
MASTER_COUNT=$(printf '%s\n' "$MASTER_V1MODELS" | grep -c .)
if [ "$MASTER_COUNT" -lt 1 ]; then
    echo "SKIP: proxy has 0 configured models — case requires at least 1"
    exit 77
fi
FIRST_MODEL=$(printf '%s\n' "$MASTER_V1MODELS" | head -n 1)

declare -A MASTER_BY_EP
for EP in "${ENDPOINTS[@]}"; do
    NAMES=$(list_model_names "$MASTER_KEY" "$EP")
    N=$(printf '%s\n' "$NAMES" | grep -c .)
    if [ "$N" -lt "$MASTER_COUNT" ]; then
        fail "master view of $EP undercounts model_name set ($N vs $MASTER_COUNT). names=$(printf '%s' "$NAMES" | tr '\n' ',')"
    fi
    MASTER_BY_EP["$EP"]="$NAMES"
done

# ---- 2. Restricted user → exactly {first_model} on every endpoint ----------
USER_RESTRICTED=$(create_user restricted "[\"$FIRST_MODEL\"]")
KEY_RESTRICTED=$(mint_key_for_user "$USER_RESTRICTED" restricted-key)

for EP in "${ENDPOINTS[@]}"; do
    OBS=$(list_model_names "$KEY_RESTRICTED" "$EP")
    if [ "$OBS" != "$FIRST_MODEL" ]; then
        fail "$EP for user restricted to [$FIRST_MODEL] must return only that. got: $(printf '%s' "$OBS" | tr '\n' ',')"
    fi
done

# ---- 3. no-default-models → empty data array on every endpoint -------------
USER_BLOCKED=$(create_user blocked '["no-default-models"]')
KEY_BLOCKED=$(mint_key_for_user "$USER_BLOCKED" blocked-key)

for EP in "${ENDPOINTS[@]}"; do
    N=$(count_data "$KEY_BLOCKED" "$EP")
    if [ "$N" != "0" ]; then
        fail "$EP for no-default-models user must return empty data; got count=$N"
    fi
done

# ---- 4. Open user (models=[]) → user.models filter MUST NOT narrow --------
#   - /v1/model/info and /v2/model/info (no flags) must equal master view:
#     user.models filter only ever narrows; an empty user.models leaves
#     the result untouched.
#   - /v2/model/info?include_team_models=true is excluded from this
#     equality check: that endpoint depends on user.teams membership,
#     not user.models. An open user with zero teams gets an empty
#     result on that endpoint by long-standing endpoint design, which
#     is orthogonal to the regression we're guarding against here.
USER_OPEN=$(create_user open '[]')
KEY_OPEN=$(mint_key_for_user "$USER_OPEN" open-key)

OPEN_USER_MODELS_ENDPOINTS=("/v1/model/info" "/v2/model/info")
for EP in "${OPEN_USER_MODELS_ENDPOINTS[@]}"; do
    OBS=$(list_model_names "$KEY_OPEN" "$EP")
    EXPECTED="${MASTER_BY_EP[$EP]}"
    if [ "$OBS" != "$EXPECTED" ]; then
        fail "$EP open-user view must match master (empty user.models must not narrow). expected=$(printf '%s' "$EXPECTED" | tr '\n' ','), got=$(printf '%s' "$OBS" | tr '\n' ',')"
    fi
done

# ---- 5. Master snapshot unchanged ------------------------------------------
for EP in "${ENDPOINTS[@]}"; do
    AFTER=$(list_model_names "$MASTER_KEY" "$EP")
    BEFORE="${MASTER_BY_EP[$EP]}"
    if [ "$AFTER" != "$BEFORE" ]; then
        fail "master view of $EP drifted during the test"
    fi
done

echo "PASS: /v1/model/info + /v2/model/info honor user.models (restricted=1, blocked=0, open=$MASTER_COUNT, endpoints=${#ENDPOINTS[@]})"
exit 0
