# Case 17 — `/v1/model/info` + `/v2/model/info` honor `user.models`

## Goal

`GET /v1/model/info` (Path B — no `litellm_model_id`) and
`GET /v2/model/info` (all flag combinations) must filter the returned
deployment list by the requesting user's `LiteLLM_UserTable.models`
field, matching what `can_user_call_model` enforces at inference time.

Both endpoints previously leaked the full proxy deployment list,
including each deployment's `litellm_params.api_base` — a config
disclosure issue. This is the same root cause as
BerriAI/litellm#26420 (which was fixed for `/v1/models` only in
PR #10) extended to the two info endpoints.

## Preconditions

- `e2e/tools/proxy status` reports `ready` (Postgres + litellm up)
- The default generated config has at least 3 model slots
  (`claude-sonnet-cache`, `claude-haiku-cache`, `gpt-4o-mini-cache`)
- No real provider API keys required: this case never hits an upstream

## Steps

```bash
PROXY="${PROXY_URL:-http://localhost:4011}"
MASTER="${MASTER_KEY:-sk-e2e-test}"
SUFFIX="case17-$(date +%s%N)"

# 1. Baseline: master sees all configured deployments on every endpoint
for EP in "/v1/model/info" "/v2/model/info" "/v2/model/info?include_team_models=true"; do
    N=$(curl -sS "$PROXY$EP" -H "Authorization: Bearer $MASTER" \
        | jq -r '[.data[].model_name] | unique | length')
    test "$N" -ge 3
done

# 2. Create restricted user: user.models = [first model name only]
FIRST=$(curl -sS "$PROXY/v1/models" -H "Authorization: Bearer $MASTER" \
    | jq -r '.data[0].id')
USER_R=$(curl -sS "$PROXY/user/new" \
    -H "Authorization: Bearer $MASTER" -H "Content-Type: application/json" \
    -d "{\"user_alias\":\"$SUFFIX-r\",\"models\":[\"$FIRST\"],\"auto_create_key\":false}" \
    | jq -r '.user_id')
KEY_R=$(e2e/tools/keys new --user-id "$USER_R" \
    --alias "$SUFFIX-r-key" | jq -r '.response.key')

# 3. Restricted user must see EXACTLY [$FIRST] on every endpoint
for EP in "/v1/model/info" "/v2/model/info" "/v2/model/info?include_team_models=true"; do
    OBS=$(curl -sS "$PROXY$EP" -H "Authorization: Bearer $KEY_R" \
        | jq -r '[.data[].model_name] | unique | .[]' | LC_ALL=C sort)
    test "$OBS" = "$FIRST"
done

# 4. no-default-models sentinel → empty list on every endpoint
USER_B=$(curl -sS "$PROXY/user/new" \
    -H "Authorization: Bearer $MASTER" -H "Content-Type: application/json" \
    -d "{\"user_alias\":\"$SUFFIX-b\",\"models\":[\"no-default-models\"],\"auto_create_key\":false}" \
    | jq -r '.user_id')
KEY_B=$(e2e/tools/keys new --user-id "$USER_B" \
    --alias "$SUFFIX-b-key" | jq -r '.response.key')
for EP in "/v1/model/info" "/v2/model/info" "/v2/model/info?include_team_models=true"; do
    N=$(curl -sS "$PROXY$EP" -H "Authorization: Bearer $KEY_B" \
        | jq -r '.data | length')
    test "$N" = "0"
done

# 5. Unrestricted (user.models=[]) → matches master snapshot
USER_O=$(curl -sS "$PROXY/user/new" \
    -H "Authorization: Bearer $MASTER" -H "Content-Type: application/json" \
    -d "{\"user_alias\":\"$SUFFIX-o\",\"models\":[],\"auto_create_key\":false}" \
    | jq -r '.user_id')
KEY_O=$(e2e/tools/keys new --user-id "$USER_O" \
    --alias "$SUFFIX-o-key" | jq -r '.response.key')
# Open user must see same distinct model_name set as master

# 6. Cleanup
for K in "$KEY_R" "$KEY_B" "$KEY_O"; do
    curl -sS "$PROXY/key/delete" \
        -H "Authorization: Bearer $MASTER" -H "Content-Type: application/json" \
        -d "{\"keys\":[\"$K\"]}" > /dev/null
done
for U in "$USER_R" "$USER_B" "$USER_O"; do
    curl -sS "$PROXY/user/delete" \
        -H "Authorization: Bearer $MASTER" -H "Content-Type: application/json" \
        -d "{\"user_ids\":[\"$U\"]}" > /dev/null
done
```

## Expected

- Step 1: each endpoint returns ≥ 3 distinct `model_name`s for master
- Step 3: each endpoint returns exactly `[$FIRST]` for restricted user.
  Before the fix `/v1/model/info` and bare `/v2/model/info` would
  return all 3+ — that's the regression we're guarding against.
- Step 4: each endpoint returns `data: []` (length 0) for the
  no-default-models sentinel user
- Step 5: open user's distinct model_name set equals master's

## Failure modes

| Symptom | Cause |
|---|---|
| Step 3 `/v1/model/info` returns >1 distinct model_name | Path B (`litellm_model_id is None`) is still self-rolling `get_key_models + get_team_models + get_complete_model_list` without `get_available_models_for_user` — check `proxy_server.py:model_info_v1`. |
| Step 3 bare `/v2/model/info` returns full list, but `?include_team_models=true` filters | The defense-in-depth `apply_user_models_filter_to_deployments` call is missing or placed inside the `include_team_models` branch instead of unconditionally after enrichment. |
| Step 4 returns non-zero | `_apply_user_models_filter` no-default-models short-circuit broken. Check `litellm/proxy/utils.py:_apply_user_models_filter`. |
| Step 5 diff non-empty | Filter leaked into the empty-`user.models` fallthrough. Check the early-return `if user_obj is None or not user_obj.models: return all_models`. |
| `keys new` fails with 4xx | DB not ready — `e2e/tools/proxy logs --tail 50`. |
