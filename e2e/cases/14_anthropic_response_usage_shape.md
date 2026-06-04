# Case 14 — Non-streaming `/v1/messages` usage must match Anthropic spec

## Goal

Regression guard for the `usage.total_tokens` strip applied to
non-streaming Anthropic-shape responses. Three-pronged assertion to
catch both **regression** (the strip stops working) and **over-reach**
(the strip leaks into endpoints it shouldn't touch).

## Background

The Anthropic `/v1/messages` spec defines exactly these usage fields:

- `input_tokens`
- `output_tokens`
- `cache_creation_input_tokens`
- `cache_read_input_tokens`
- `cache_creation.{ephemeral_5m_input_tokens, ephemeral_1h_input_tokens}`

`total_tokens` is **not** in the spec — it's an OpenAI convention. Before
the fix, LiteLLM's non-streaming `/v1/messages` response carried a
LiteLLM-injected `usage.total_tokens = input + output`, which:

1. **Diverged from streaming** — `message_delta.usage` in the SSE path
   has never carried `total_tokens`. Same endpoint, different shape.
2. **Diverged from upstream** — direct calls to
   `https://api.anthropic.com/v1/messages` (and even the corp gateway
   at `maasapi.*/v1/messages`) never return `total_tokens`.
3. **Numerically misleading** — `total = input + output` undercounts
   when `cache_read` and `cache_creation` are non-zero, because cache
   tokens land in their own counters. A 100k-token cached prompt with
   1 non-cache input token + 200 output tokens reports
   `total_tokens = 201` — off by ~99.8%.

The strip is implemented in
`litellm/proxy/anthropic_endpoints/endpoints.py` at the
`anthropic_response` function's success path, via
`_strip_total_tokens_from_anthropic_response`. It only touches dict-shaped
responses; streaming responses go through a different code path and were
already spec-compliant.

## Preconditions

- `e2e/tools/proxy status` reports `ready`
- `ANTHROPIC_API_KEY` set
- `claude-sonnet-cache` configured as a routable model (default in the
  e2e harness, uses anthropic backend)

## Steps

```bash
bash e2e/cases/data/14_anthropic_response_usage_shape.sh
echo "exit=$?"
```

The fixture makes three calls:

1. **Non-streaming POST `/v1/messages`** → asserts `usage` has
   `input_tokens` and `output_tokens` but NOT `total_tokens`
2. **Streaming POST `/v1/messages`** → parses the `message_delta` event,
   asserts its `usage` does NOT have `total_tokens`
3. **POST `/v1/chat/completions`** (OpenAI-shape control) → asserts
   `usage.total_tokens` IS present (proves the strip is scoped to the
   Anthropic passthrough endpoint, not global)

## Expected — GREEN

```
[case 14] non-streaming /v1/messages...
  usage.total_tokens present?  false  (want: false)
  usage.input_tokens present?  true   (want: true)
  usage.output_tokens present? true   (want: true)

[case 14] streaming /v1/messages...
  message_delta.usage.total_tokens present? false (want: false)

[case 14] OpenAI-shape /v1/chat/completions (control: should keep total_tokens)...
  /v1/chat/completions usage.total_tokens present? true (want: true)

PASS: /v1/messages usage shape matches Anthropic spec; /v1/chat/completions unchanged
```

## Failure modes

| Symptom | Cause |
|---|---|
| `FAIL [1]: non-streaming /v1/messages still has usage.total_tokens` | The strip helper isn't being called, or some other code path adds total_tokens after the strip. Check `_strip_total_tokens_from_anthropic_response` is invoked in the success path before `return result`. |
| `FAIL [1]: usage is missing input_tokens or output_tokens` | The strip went too far — it stripped legitimate fields. Helper should pop only `total_tokens`. |
| `FAIL [2]: streaming message_delta.usage has total_tokens` | Someone added LiteLLM total injection to the streaming SSE path. Streaming should remain pure passthrough. |
| `FAIL [3]: /v1/chat/completions lost total_tokens` | The strip leaked into the OpenAI-shape endpoint — that endpoint is supposed to carry total_tokens. Verify the strip is wired in `anthropic_endpoints/endpoints.py` only, not in shared response middleware. |
| `FAIL [2]: no message_delta event` | Streaming itself broke (separate regression). Check case 13. |

## Cross-reference

- Case 13 — guards streaming TTFT timing (separate aspect of
  `/v1/messages`)
- This case (14) — guards usage shape on both paths

Together they pin the `/v1/messages` endpoint's wire format to match
the Anthropic spec, in both streaming and non-streaming modes.
