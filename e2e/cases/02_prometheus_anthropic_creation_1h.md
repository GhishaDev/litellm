# Case 02 — Anthropic prompt cache creation (1h TTL, extended)

## Goal

Same as Case 01, but with `ttl: "1h"`. Verify the `cache_ttl="1h"`
bucket of the creation counter increments instead of `5m`.

## Preconditions

- `ANTHROPIC_API_KEY` set, **with extended-cache-ttl support enabled**
  for the account. Most accounts have this by default in 2026; older
  accounts may still need to opt in. If the request 400s with a
  beta-header error, this case is N/A on your account.
- `e2e/tools/proxy status` reports `ready`

## Steps

```bash
e2e/tools/metrics snapshot > /tmp/m_before.json

SEED="case02-$(date +%s)"
e2e/tools/call \
    --provider anthropic \
    --cache ephemeral \
    --ttl 1h \
    --prompt-tokens 1500 \
    --seed "$SEED" \
    > /tmp/call_response.json

e2e/tools/metrics snapshot > /tmp/m_after.json

jq '.response.usage' /tmp/call_response.json
e2e/tools/metrics diff /tmp/m_before.json /tmp/m_after.json \
    --metric litellm_prompt_cache_creation_tokens_metric
```

## Expected

- `response_status` == 200
- `.response.usage.cache_creation_input_tokens` > 0
- `.response.usage.cache_creation.ephemeral_1h_input_tokens` > 0
  (newer Anthropic API shape)
- Diff shows **one row** with `cache_ttl="1h"` and matching delta
- **No row** with `cache_ttl="5m"` for this delta

## Skip rule

If response is 400 with content like `extended-cache-ttl-2025-04-11 not
enabled` or `requires beta header`, mark this case **N/A on this
account** rather than failing.
