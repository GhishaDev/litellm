# Case 20 — Per-deployment `returned_model_name` overrides the response model

## Goal

Verify that `litellm_params.returned_model_name` (per-deployment, literal
string) replaces the `model` field returned to clients across all four
combinations of streaming/non-streaming × OpenAI `/v1/chat/completions` /
Anthropic `/v1/messages`. The override must also propagate into the
nested `message_start.message.model` for Anthropic SSE — that path
previously leaked the upstream model id because the existing
chat-completions chunk restamper only touches the top-level `chunk.model`.

## Origin

We expose a public LiteLLM gateway. Without this override, clients see
either the routing alias (`claude-sonnet-cache`) or — on `/v1/messages`
streaming — the raw upstream model id (`claude-sonnet-4-6`). Gateways
need a single, deterministic facade name regardless of routing.

## Preconditions

- `e2e/tools/proxy status` reports `ready` against an image built AFTER the
  `fix/anthropic-returned-model-name` branch (`e2e/tools/proxy build` if
  unsure). The rendered config must include the `claude-renamed`
  deployment — `proxy restart` re-renders.
- `ANTHROPIC_API_KEY` set (cost: 2 short Sonnet calls ≈ $0.001).
- `OPENAI_API_KEY` set (2 short calls against the configured base).

The rendered config (`e2e/_config/.litellm.rendered.yaml`) declares:

```yaml
- model_name: claude-renamed
  litellm_params:
    model: anthropic/claude-sonnet-4-6        # upstream
    api_key: os.environ/ANTHROPIC_API_KEY
    api_base: os.environ/ANTHROPIC_API_BASE
    returned_model_name: public-name-for-clients
```

The expected response model on every successful call is
`public-name-for-clients` (NOT `claude-renamed`, NOT
`anthropic/claude-sonnet-4-6`).

## Steps

```bash
bash e2e/cases/data/20_returned_model_name.sh
echo "exit=$?"
```

The fixture issues four calls against `claude-renamed`. Each prints
`PASS:` or `FAIL:`:

- **A1** non-streaming `/v1/messages` → top-level `response.model`
- **A2** streaming `/v1/messages` → `message_start.message.model` in the
  first SSE event
- **A3** non-streaming `/v1/chat/completions` → top-level `response.model`
- **A4** streaming `/v1/chat/completions` → first chunk's `model`

All four must equal `public-name-for-clients`.

## Expected — GREEN

```
A1 PASS: non-streaming /v1/messages model=public-name-for-clients
A2 PASS: streaming /v1/messages message_start.message.model=public-name-for-clients
A3 PASS: non-streaming /v1/chat/completions model=public-name-for-clients
A4 PASS: streaming /v1/chat/completions chunk.model=public-name-for-clients
```

## Failure modes

| Symptom | Likely cause |
|---|---|
| A1 returns upstream / alias name | `_override_openai_response_model` is not consuming `_litellm_returned_model_name`; check `common_request_processing.py:1316` and the function's `override_model_name` branch |
| A2 returns upstream model id (`claude-sonnet-4-6`) | The Anthropic SSE generator restamp block in `async_streaming_data_generator` is not running, or `_litellm_returned_model_name` did not propagate to `request_data` |
| A2 returns alias (`claude-renamed`) | restamp running but with the wrong source string — verify `_get_client_requested_model_for_streaming` priority |
| A3 returns alias `claude-renamed` (not the override) | Same as A1 — non-streaming path didn't apply the override |
| A4 returns upstream id | `_restamp_streaming_chunk_model` bypassed (Azure-router / fastest_response branches without the override-bypass check) |
| Any test fails with deployment-not-found | Image was built before this branch was merged, or `proxy restart` was not run after the render-script change |

## Cross-reference

- `litellm/types/router.py` — `returned_model_name` field on `GenericLiteLLMParams`
- `litellm/proxy/common_request_processing.py:325` — `_override_openai_response_model(override_model_name=...)`
- `litellm/proxy/common_request_processing.py:~1102` — resolution: stash `_litellm_returned_model_name` into `self.data`
- `litellm/proxy/common_request_processing.py:~1830` — SSE `message_start` rewrite
- `litellm/proxy/proxy_server.py` — `_get_client_requested_model_for_streaming` priority + `_restamp_streaming_chunk_model` override-bypass
- `tests/test_litellm/test_returned_model_name.py` — functional unit tests
- `e2e/tools/proxy` — adds `claude-renamed` deployment to the rendered config
