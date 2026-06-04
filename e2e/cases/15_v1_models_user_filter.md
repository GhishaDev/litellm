# Case 15 — `/v1/models` honors `user.models` (Personal Models)

## Goal

`GET /v1/models` must filter the model list by the requesting user's
`LiteLLM_UserTable.models` field, matching what `can_user_call_model`
enforces at inference time. Without this, an internal user restricted
to a subset of models sees the full proxy list on `/v1/models` even
though calling any non-allowed model returns 401.

Verifies the fix for BerriAI/litellm#26420.

## Preconditions

- `e2e/tools/proxy status` reports `ready` (Postgres + litellm up)
- The auto-generated config has at least 3 model slots
  (`claude-sonnet-cache`, `claude-haiku-cache`, `gpt-4o-mini-cache`) —
  the default `e2e/tools/proxy start` rendering provides all three
- No API keys required: this case never hits an upstream provider

## Steps

```bash
PROXY="${PROXY_URL:-http://localhost:4011}"
MASTER="${MASTER_KEY:-sk-e2e-test}"
SUFFIX="case15-$(date +%s%N)"

# 1. Baseline: master key sees all 3 configured models.
curl -sS "$PROXY/v1/models" \
    -H "Authorization: Bearer $MASTER" \
    | jq -r '.data[].id' | sort > /tmp/c15_master.txt
test "$(wc -l < /tmp/c15_master.txt)" -ge 3

# 2. Create a user restricted to claude-sonnet-cache ONLY.
USER_RESTRICTED=$(curl -sS "$PROXY/user/new" \
    -H "Authorization: Bearer $MASTER" -H "Content-Type: application/json" \
    -d "{\"user_alias\":\"$SUFFIX-restricted\",\"models\":[\"claude-sonnet-cache\"],\"auto_create_key\":false}" \
    | jq -r '.user_id')

# 3. Mint a key under that user with key.models=[] so user.models is
#    the ONLY layer that can narrow the list.
KEY_RESTRICTED=$(e2e/tools/keys new --user-id "$USER_RESTRICTED" \
    --alias "$SUFFIX-restricted-key" | jq -r '.response.key')

# 4. Restricted user must see exactly [claude-sonnet-cache] — neither
#    of the other two configured models.
curl -sS "$PROXY/v1/models" \
    -H "Authorization: Bearer $KEY_RESTRICTED" \
    | jq -r '.data[].id' | sort > /tmp/c15_restricted.txt
diff <(echo "claude-sonnet-cache") /tmp/c15_restricted.txt

# 5. `no-default-models` sentinel → empty list.
USER_BLOCKED=$(curl -sS "$PROXY/user/new" \
    -H "Authorization: Bearer $MASTER" -H "Content-Type: application/json" \
    -d "{\"user_alias\":\"$SUFFIX-blocked\",\"models\":[\"no-default-models\"],\"auto_create_key\":false}" \
    | jq -r '.user_id')
KEY_BLOCKED=$(e2e/tools/keys new --user-id "$USER_BLOCKED" \
    --alias "$SUFFIX-blocked-key" | jq -r '.response.key')
curl -sS "$PROXY/v1/models" \
    -H "Authorization: Bearer $KEY_BLOCKED" \
    | jq -r '.data | length'   # → must print 0

# 6. Unrestricted user (empty user.models) → sees full proxy list.
USER_OPEN=$(curl -sS "$PROXY/user/new" \
    -H "Authorization: Bearer $MASTER" -H "Content-Type: application/json" \
    -d "{\"user_alias\":\"$SUFFIX-open\",\"models\":[],\"auto_create_key\":false}" \
    | jq -r '.user_id')
KEY_OPEN=$(e2e/tools/keys new --user-id "$USER_OPEN" \
    --alias "$SUFFIX-open-key" | jq -r '.response.key')
curl -sS "$PROXY/v1/models" \
    -H "Authorization: Bearer $KEY_OPEN" \
    | jq -r '.data[].id' | sort > /tmp/c15_open.txt
diff /tmp/c15_master.txt /tmp/c15_open.txt   # must be no-op

# 7. Master view still complete (defense-in-depth — per-user narrowing
#    must never bleed into the master key).
curl -sS "$PROXY/v1/models" \
    -H "Authorization: Bearer $MASTER" \
    | jq -r '.data[].id' | sort > /tmp/c15_master_after.txt
diff /tmp/c15_master.txt /tmp/c15_master_after.txt

# 8. Cleanup
for KEY in "$KEY_RESTRICTED" "$KEY_BLOCKED" "$KEY_OPEN"; do
    curl -sS "$PROXY/key/delete" \
        -H "Authorization: Bearer $MASTER" -H "Content-Type: application/json" \
        -d "{\"keys\":[\"$KEY\"]}" > /dev/null
done
for U in "$USER_RESTRICTED" "$USER_BLOCKED" "$USER_OPEN"; do
    curl -sS "$PROXY/user/delete" \
        -H "Authorization: Bearer $MASTER" -H "Content-Type: application/json" \
        -d "{\"user_ids\":[\"$U\"]}" > /dev/null
done
```

## Expected

- Step 1: 3+ model ids in `c15_master.txt`
- Step 4: `c15_restricted.txt` contains **exactly** `claude-sonnet-cache`
  and nothing else. Before the fix this file would contain all 3 models
  — that is the bug.
- Step 5: `jq '.data | length'` prints `0`
- Step 6: `diff` exits 0 — empty `user.models` falls through to the
  proxy list
- Step 7: master snapshot identical before/after

## Failure modes

| Symptom | Cause |
|---|---|
| Step 4 diff shows `claude-haiku-cache` / `gpt-4o-mini-cache` | Pre-#26420 behavior: `get_available_models_for_user` doesn't consult `user.models`. Fix lives in `litellm/proxy/utils.py` (`_apply_user_models_filter`) + `litellm/proxy/auth/model_checks.py` (`get_user_models`). |
| Step 5 prints non-zero | `SpecialModelNames.no_default_models` sentinel not short-circuited in the filter — check the `no-default-models` branch of `_apply_user_models_filter`. |
| Step 4 returns `[]` instead of `[claude-sonnet-cache]` | Filter too aggressive — likely confused access-group expansion or used `fnmatch` against an exact model name with a stray `*`. |
| `keys new` fails with 4xx | DB not ready or schema migration didn't finish — `e2e/tools/proxy logs --tail 50`. |
| Step 7 diff non-empty | Filter leaked into master path. Check that `_apply_user_models_filter` early-returns when `user_api_key_dict.user_id is None` (master key auth). |
