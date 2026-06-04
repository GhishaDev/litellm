# Case 01 — Anthropic prompt cache creation (5m TTL)

## Goal

A single request that marks the system prompt with
`cache_control: {"type": "ephemeral"}` should:
1. Cause Anthropic to write a 5-minute cache entry, reflected as
   `cache_creation_input_tokens > 0` in the API response.
2. Increment
   `litellm_input_cache_creation_tokens_metric{cache_ttl="5m", api_provider="anthropic"}`
   by exactly that amount.
3. **Not** increment any `cache_ttl="1h"` series.
4. **Not** increment `litellm_input_cached_tokens_metric` (first
   request, nothing to read yet).

## Preconditions

- `ANTHROPIC_API_KEY` set in `e2e/.env`
- `e2e/tools/proxy status` reports `ready`

## Steps

```bash
# 0. Confirm baseline state
e2e/tools/proxy status

# 1. Snapshot metrics
e2e/tools/metrics snapshot > /tmp/m_before.json

# 2. Make one Anthropic request with 5m ephemeral cache.
#    Seed = unique to avoid reading a cache entry from a previous run.
#    user_id = stable identifier so the corp Anthropic gateway routes to
#    a deterministic upstream account (otherwise round-robin LB splits
#    the cache namespace and metric labels don't aggregate cleanly).
SEED="case01-$(date +%s)"
e2e/tools/call \
    --provider anthropic \
    --cache ephemeral \
    --ttl 5m \
    --prompt-tokens 1500 \
    --seed "$SEED" \
    --user-id "case01-user-$SEED" \
    > /tmp/call_response.json

# 3. Snapshot metrics again
e2e/tools/metrics snapshot > /tmp/m_after.json

# 4. Inspect what the provider actually did
jq '.response.usage' /tmp/call_response.json

# 5. Diff metrics
e2e/tools/metrics diff /tmp/m_before.json /tmp/m_after.json \
    --metric litellm_input_cache_creation_tokens_metric
e2e/tools/metrics diff /tmp/m_before.json /tmp/m_after.json \
    --metric litellm_input_cached_tokens_metric
```

## Expected

- `response_status` == 200 in `/tmp/call_response.json`
- `.response.usage.cache_creation_input_tokens` > 0
- `.response.usage.cache_read_input_tokens` == 0 (first time this prefix is sent)
- Diff for `litellm_input_cache_creation_tokens_metric` shows **exactly one**
  row with `cache_ttl="5m"`, `api_provider="anthropic"`, `delta` ==
  `cache_creation_input_tokens`
- Diff for `litellm_input_cached_tokens_metric` shows no rows
  (or shows rows only from unrelated traffic — judge by labels)

## Common failures

| Symptom | Likely cause |
|---|---|
| `cache_creation_input_tokens=0` | prompt too short — bump `--prompt-tokens` to 2200 (haiku threshold) |
| `response_status=401` | `ANTHROPIC_API_KEY` missing or wrong |
| `response_status=400 "cache_control invalid"` | model doesn't support caching (use Sonnet 3.5+) |
| metric delta = 0 but usage shows tokens | `callbacks: ["prometheus"]` missing from config, or proxy was started before our code fix landed (run `e2e/tools/proxy rebuild`) |
