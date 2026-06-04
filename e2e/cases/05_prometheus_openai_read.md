# Case 05 — OpenAI auto prompt-cache read

## Goal

OpenAI does prompt caching automatically when a prompt is ≥ 1024 tokens
**and the exact prefix has been seen recently**. There is no
`cache_control` marker — caching is implicit. Two identical long
requests should yield `prompt_tokens_details.cached_tokens > 0` on the
second, and that value should appear in
`litellm_input_cached_tokens_metric{api_provider="openai"}`.

## Preconditions

- `OPENAI_API_KEY` set
- `e2e/tools/proxy status` reports `ready`
- Model in config: `gpt-4o-mini-cache` → `openai/gpt-4o-mini`
- Run both calls within ~5 minutes (idle TTL is 5–10 min)

## Steps

```bash
SEED="case05-$(date +%s)"

# 1. Warm-up call — primes OpenAI's prefix cache
e2e/tools/call --provider openai --prompt-tokens 1800 --seed "$SEED" \
    > /tmp/call_first.json
jq '.response.usage.prompt_tokens_details' /tmp/call_first.json

# 2. Snapshot
e2e/tools/metrics snapshot > /tmp/m_before.json

# 3. Identical 2nd call — expect cache hit
e2e/tools/call --provider openai --prompt-tokens 1800 --seed "$SEED" \
    > /tmp/call_second.json
jq '.response.usage.prompt_tokens_details' /tmp/call_second.json

e2e/tools/metrics snapshot > /tmp/m_after.json

# 4. Diff
e2e/tools/metrics diff /tmp/m_before.json /tmp/m_after.json \
    --metric litellm_input_cached_tokens_metric \
    --label api_provider=openai
```

## Expected

- 1st call: `.usage.prompt_tokens_details.cached_tokens` = 0
- 2nd call: `.usage.prompt_tokens_details.cached_tokens` > 0
- Diff shows a row with `api_provider="openai"` and `delta` matching
  the 2nd call's `cached_tokens`

## Known flakiness

OpenAI's automatic caching is best-effort:
- If you exceed ~15 req/min on the same prefix it may overflow to
  another shard and miss
- gpt-4o-2024-05-13 and chatgpt-4o-latest do **not** support prompt
  caching — stay on gpt-4o-mini for this case
- A second call within < 1s sometimes misses; if so, wait 2-3s and retry
