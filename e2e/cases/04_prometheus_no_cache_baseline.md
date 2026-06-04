# Case 04 — No cache_control means no cache metric emission

## Goal

A plain Anthropic request without `cache_control` markers should
**neither** create a cache entry **nor** emit any of our new
provider-prompt-cache metrics. This guards against accidentally
counting every request as a cache event.

## Preconditions

- `ANTHROPIC_API_KEY` set
- `e2e/tools/proxy status` reports `ready`

## Steps

```bash
e2e/tools/metrics snapshot > /tmp/m_before.json

SEED="case04-$(date +%s)"
e2e/tools/call --provider anthropic --cache none \
    --prompt-tokens 1500 --seed "$SEED" \
    --user-id "case04-user-$SEED" > /tmp/call_response.json

e2e/tools/metrics snapshot > /tmp/m_after.json

jq '.response.usage' /tmp/call_response.json
e2e/tools/metrics diff /tmp/m_before.json /tmp/m_after.json \
    --metric litellm_input_cached_tokens_metric
e2e/tools/metrics diff /tmp/m_before.json /tmp/m_after.json \
    --metric litellm_input_cache_creation_tokens_metric
```

## Expected

- `response_status` == 200
- `.response.usage.cache_creation_input_tokens` either absent or 0
- `.response.usage.cache_read_input_tokens` either absent or 0
- Both `diff` invocations print `(no deltas)` (or only show rows from
  unrelated traffic — judge by labels matching this call's model)
