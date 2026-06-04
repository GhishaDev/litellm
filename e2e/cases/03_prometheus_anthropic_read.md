# Case 03 — Anthropic prompt cache read

## Goal

Two identical Anthropic requests (same `cache_control`-marked system
prompt, same seed). The **second** request should read from cache:
- `cache_read_input_tokens > 0` in the API response
- `litellm_prompt_cache_read_tokens_metric` increments

The first request still writes the cache; you'll see the creation
counter move too, but we focus the assertions on the read counter delta
between request 1 and request 2.

## Preconditions

- `ANTHROPIC_API_KEY` set
- `e2e/tools/proxy status` reports `ready`
- Run within ~5 minutes of starting (5m TTL on the cache entry)

## Steps

```bash
SEED="case03-$(date +%s)"
# user_id makes the corp gateway sticky-route to the same upstream
# account on both calls, so the second call can read the cache the
# first call wrote. Without it the gateway round-robins across
# upstream accounts and cache_read never hits.
USER_ID="case03-user-$SEED"

# 1. First call — writes the cache
e2e/tools/call --provider anthropic --cache ephemeral --ttl 5m \
    --prompt-tokens 1500 --seed "$SEED" --user-id "$USER_ID" \
    > /tmp/call_first.json
jq '.response.usage' /tmp/call_first.json

# 2. Snapshot AFTER first call so we measure the *read* delta cleanly
e2e/tools/metrics snapshot > /tmp/m_before.json

# 3. Second call — should hit cache
e2e/tools/call --provider anthropic --cache ephemeral --ttl 5m \
    --prompt-tokens 1500 --seed "$SEED" --user-id "$USER_ID" \
    > /tmp/call_second.json
jq '.response.usage' /tmp/call_second.json

# 4. Snapshot after
e2e/tools/metrics snapshot > /tmp/m_after.json

# 5. Diff: focus on the read metric
e2e/tools/metrics diff /tmp/m_before.json /tmp/m_after.json \
    --metric litellm_prompt_cache_read_tokens_metric
```

## Expected

- First request: `cache_creation_input_tokens > 0`,
  `cache_read_input_tokens == 0`
- Second request: `cache_read_input_tokens > 0`,
  `cache_creation_input_tokens` either 0 or small (refresh)
- Diff for `litellm_prompt_cache_read_tokens_metric` shows one row with
  `api_provider="anthropic"` and `delta` == second request's
  `cache_read_input_tokens`

## Failure modes

| Symptom | Action |
|---|---|
| 2nd call shows `cache_read_input_tokens=0` after retries | Likely **gateway** doesn't honor cache reads. Verify by bypassing the proxy: POST the same payload directly to `$ANTHROPIC_API_BASE/v1/messages` twice. If direct also shows `cache_read=0` on the 2nd call, the gateway is the limitation, not litellm. The runbook fixture (`e2e/cases/data/03_prometheus_anthropic_read.sh`) exits 77 (SKIP) in this case. |
| `delta` mismatches `cache_read_input_tokens` | other traffic hitting the proxy concurrently — pause other calls and rerun |

## Gateway compatibility note

This case asserts on cache **read** behavior. It requires the upstream
Anthropic endpoint (`$ANTHROPIC_API_BASE`) to share cache state across
requests. Tested-compatible:

- Direct `https://api.anthropic.com` (with a real Anthropic key)
- Gateways that proxy without rotating cache keys or stripping
  `prompt-caching` extensions

Tested-incompatible (case 03 will be skipped):

- Thin Anthropic shims that forward `cache_control` to upstream but
  don't share the underlying cache across requests
- Gateways that inject per-request fingerprints into the cache key
