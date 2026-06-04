# Case 08 — Per-virtual-key Prometheus labels

## Goal

When a request is authenticated with a **virtual key** (not master_key),
the resulting Prometheus samples must be labeled with that key's
**hashed_api_key** (sha256) and **api_key_alias**. This proves the
PrometheusLabelFactoryContext picks up `user_api_key_*` metadata from
the auth chain, not just from master_key fallback.

Pre-DB cases 01-07 always saw `hashed_api_key='<master_key_hash>'` and
`api_key_alias='None'`. Case 08 verifies the labels track an actual
DB-backed virtual key.

## Preconditions

- Postgres + litellm up (`e2e/tools/proxy status` reports `ready`)
- `ANTHROPIC_API_KEY` set in `.env`

## Steps

```bash
# 1. Mint a virtual key with a stable alias
ALIAS="case08-key-$(date +%s)"
e2e/tools/keys new --alias "$ALIAS" --models claude-sonnet-cache \
    --duration 30m > /tmp/keys_new.json
VKEY=$(jq -r '.response.key' /tmp/keys_new.json)
echo "minted vkey alias=$ALIAS"

# 2. Compute its expected hash for label assertion
EXPECTED_HASH=$(e2e/tools/keys hash "$VKEY")

# 3. Snapshot before
e2e/tools/metrics snapshot > /tmp/m_before_08.json

# 4. Call through the virtual key (NOT master_key)
SEED="case08-$(date +%s)"
e2e/tools/call --provider anthropic --cache ephemeral --ttl 5m \
    --prompt-tokens 1500 --seed "$SEED" --api-key "$VKEY" \
    --user-id "case08-user-$SEED" \
    > /tmp/call_08.json
jq '.response.usage.prompt_tokens_details' /tmp/call_08.json

# 5. Snapshot after
e2e/tools/metrics snapshot > /tmp/m_after_08.json

# 6. Inspect the new sample: it should carry the EXPECTED_HASH and the
#    alias we set. Filter by alias to narrow down.
e2e/tools/metrics diff /tmp/m_before_08.json /tmp/m_after_08.json \
    --metric litellm_input_cache_creation_tokens_metric \
    --label api_key_alias=$ALIAS

# 7. (Optional) confirm the hashed_api_key matches
e2e/tools/metrics diff /tmp/m_before_08.json /tmp/m_after_08.json \
    --metric litellm_input_cache_creation_tokens_metric \
    --label hashed_api_key=$EXPECTED_HASH

# 8. Cleanup
e2e/tools/keys delete --key "$VKEY" > /dev/null
```

## Expected

- `keys new` returns `status=200` and `.response.key` starts with `sk-`
- Step 6 diff shows **exactly one row** with the alias label populated
- Step 7 diff shows the **same row** (proves both labels agree on the
  same series)
- Both rows show a positive `delta` matching the call's
  `cache_creation_tokens`

## Failure modes

| Symptom | Cause |
|---|---|
| `keys new` 4xx | Postgres not ready, or schema migration didn't finish — `e2e/tools/proxy logs --tail 50` |
| diff finds 0 rows with alias | `user_api_key_*` metadata isn't flowing into PrometheusLabelFactoryContext; bug in auth → prometheus integration |
| `EXPECTED_HASH` mismatches the metric's `hashed_api_key` | hashing algorithm drift; `e2e/tools/keys hash` uses sha256 — verify it still matches `litellm.proxy.utils.hash_token` |
