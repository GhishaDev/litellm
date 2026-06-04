# Case 06 — OpenAI never emits cache_creation_tokens_metric

## Goal

`litellm_input_cache_creation_tokens_metric` is an Anthropic-only
concept (LiteLLM's `prompt_tokens_details.cache_creation_tokens` is
populated only from Anthropic's `cache_creation_input_tokens`). Even
when OpenAI auto-caches a long prompt, this metric must **not**
increment for an OpenAI call.

This case prevents a regression where someone "helpfully" maps OpenAI's
cached tokens into the creation counter — which would muddle dashboards
and overstate cache writes.

## Preconditions

- `OPENAI_API_KEY` set
- `e2e/tools/proxy status` reports `ready`

## Steps

```bash
e2e/tools/metrics snapshot > /tmp/m_before.json

SEED="case06-$(date +%s)"
# Two calls so a cache read DEFINITELY happens (max chance of metric
# accidentally firing if mapping is wrong).
e2e/tools/call --provider openai --prompt-tokens 1800 --seed "$SEED" \
    > /tmp/call_first.json
e2e/tools/call --provider openai --prompt-tokens 1800 --seed "$SEED" \
    > /tmp/call_second.json

e2e/tools/metrics snapshot > /tmp/m_after.json

# Look for ANY row with api_provider=openai in the creation metric
e2e/tools/metrics diff /tmp/m_before.json /tmp/m_after.json \
    --metric litellm_input_cache_creation_tokens_metric \
    --label api_provider=openai
```

## Expected

- Both calls return 200
- Final diff prints **`(no deltas)`** — zero rows for openai under
  `cache_creation_tokens_metric`
- (For sanity, the read metric should still increment — see Case 05)
