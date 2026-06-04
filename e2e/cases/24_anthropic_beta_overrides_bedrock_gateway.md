# Case 24 — `anthropic_beta_overrides` against a Bedrock-backed gateway

## Goal

Prove end-to-end that the new per-deployment
`anthropic_beta_overrides` config rewrites
`advanced-tool-use-2025-11-20` (the auto-injected superset flag) to
`tool-search-tool-2025-10-19` so a Bedrock-backed Anthropic-spec
gateway accepts the request instead of returning
`ValidationException: invalid beta flag`.

## Background

LiteLLM's Anthropic provider path (`anthropic/`) auto-injects
`anthropic-beta: advanced-tool-use-2025-11-20` whenever any of the
following are present in `tools`:

- `tool_search_tool_regex_20251119` / `tool_search_tool_bm25_20251119`
- `allowed_callers: ["code_execution_20250825"]` (programmatic tool calling)
- `input_examples: [...]` (tool use examples)

This is correct for `https://api.anthropic.com` but is **rejected by AWS
Bedrock** with `400 ValidationException: invalid beta flag`. Bedrock
exposes the same family of features under the older single-purpose
flag `tool-search-tool-2025-10-19` (in the body-level `anthropic_beta`
array, not as an HTTP header — but a Bedrock-backed Anthropic-spec
gateway translates the header into the body field for you).

Some gateways expose an Anthropic-spec front-end on top of a Bedrock
back-end (common for cost-routing or region-locked deployments).
Without an override mechanism, every LiteLLM user routing through such
a gateway must either:

- Patch `anthropic_beta_headers_config.json` (fork-level change), or
- Disable tool search entirely.

The new `anthropic_beta_overrides` config makes this per-deployment,
no fork required.

## What the new config looks like

```yaml
model_list:
  - model_name: claude-on-bedrock-gateway
    litellm_params:
      model: anthropic/claude-sonnet-4-6
      api_base: https://your-bedrock-gateway.example.com
      api_key: os.environ/GATEWAY_API_KEY
      anthropic_beta_overrides:
        advanced-tool-use-2025-11-20: tool-search-tool-2025-10-19
```

Value semantics:

- non-empty string → rewrite the beta to that string
- `null` (YAML) or empty string → suppress the beta entirely
- header absent from this map → existing behavior (provider mapping or pass-through)

User-supplied `extra_headers["anthropic-beta": ...]` bypasses the
override map (user wins). The override only governs LiteLLM's
auto-injected betas.

## Preconditions

- A Bedrock-backed Anthropic-spec gateway you control or have creds
  for. Set these in `e2e/.env2`:
  ```
  GATEWAY_BASE_URL=https://your-gateway.example.com
  GATEWAY_API_KEY=sk-...
  GATEWAY_MODEL=claude-sonnet-4-6
  ```
- Network egress to the gateway host.
- Python with `anthropic` and a checkout of this `ship/v1.83.10`
  branch with `fix/anthropic-beta-overrides` applied.

## Steps

### 1. Baseline: confirm bug exists without the override (control)

Without any override, LiteLLM still injects
`anthropic-beta: advanced-tool-use-2025-11-20`, which the gateway
forwards to Bedrock, which rejects.

```bash
python3 - <<'PY'
import os, sys, litellm
litellm.set_verbose = False
try:
    resp = litellm.completion(
        model="anthropic/claude-sonnet-4-6",
        api_base=os.environ["GATEWAY_BASE_URL"],
        api_key=os.environ["GATEWAY_API_KEY"],
        max_tokens=128,
        tools=[
            {"type": "tool_search_tool_regex_20251119",
             "name": "tool_search_tool_regex"},
        ],
        messages=[{"role": "user", "content": "hi"}],
    )
    print("UNEXPECTED OK:", resp.choices[0].finish_reason)
    sys.exit(1)
except litellm.BadRequestError as e:
    msg = str(e)
    if "invalid beta flag" in msg:
        print("EXPECTED 400 invalid beta flag — bug reproduces")
        sys.exit(0)
    print("UNEXPECTED ERROR:", msg)
    sys.exit(2)
PY
```

Expected: exit 0, stdout `EXPECTED 400 invalid beta flag — bug reproduces`.

### 2. With override: confirm the rewrite + accepted

```bash
python3 - <<'PY'
import os, litellm
litellm.set_verbose = False
resp = litellm.completion(
    model="anthropic/claude-sonnet-4-6",
    api_base=os.environ["GATEWAY_BASE_URL"],
    api_key=os.environ["GATEWAY_API_KEY"],
    max_tokens=512,
    anthropic_beta_overrides={
        "advanced-tool-use-2025-11-20": "tool-search-tool-2025-10-19",
    },
    tools=[
        {"type": "tool_search_tool_regex_20251119",
         "name": "tool_search_tool_regex"},
        {"name": "get_weather",
         "description": "Get the weather at a specific location",
         "input_schema": {
             "type": "object",
             "properties": {"location": {"type": "string"}},
             "required": ["location"],
         },
         "defer_loading": True},
    ],
    messages=[{"role": "user",
               "content": "What's the weather in SF? "
                          "Use tool_search_tool_regex first."}],
)
choice = resp.choices[0]
print("finish_reason:", choice.finish_reason)
assert choice.finish_reason == "tool_calls", choice
tool_calls = [tc.function.name for tc in (choice.message.tool_calls or [])]
print("tool_calls:", tool_calls)
# Server-side tool_search shows up alongside the discovered tool.
assert any(name in tool_calls for name in ("tool_search_tool_regex", "get_weather")), tool_calls
print("OK — override rewrite end-to-end works against real Bedrock-backed gateway")
PY
```

Expected: `finish_reason: tool_calls`, `OK — override rewrite end-to-end works...`.

### 3. Inspect outbound header (no network call — pure unit verification)

Bypass the real gateway and inspect what LiteLLM actually emitted by
trapping the outbound httpx request:

```bash
python3 - <<'PY'
import httpx, litellm

captured = {}
orig_send = httpx.Client.send

def trap(self, request, **kw):
    captured["beta"] = request.headers.get("anthropic-beta")
    captured["url"] = str(request.url)
    # Short-circuit: return a minimal valid Anthropic-shape response so
    # litellm doesn't blow up parsing.
    body = (b'{"id":"msg_test","type":"message","role":"assistant",'
            b'"content":[{"type":"text","text":"ok"}],'
            b'"stop_reason":"end_turn","model":"claude-sonnet-4-6",'
            b'"usage":{"input_tokens":1,"output_tokens":1}}')
    return httpx.Response(200, request=request, content=body,
                          headers={"content-type": "application/json"})

httpx.Client.send = trap

litellm.completion(
    model="anthropic/claude-sonnet-4-6",
    api_base="https://example.invalid",
    api_key="sk-test",
    max_tokens=8,
    anthropic_beta_overrides={
        "advanced-tool-use-2025-11-20": "tool-search-tool-2025-10-19",
    },
    tools=[{"type": "tool_search_tool_regex_20251119",
            "name": "tool_search_tool_regex"}],
    messages=[{"role": "user", "content": "hi"}],
)
print("URL:", captured["url"])
print("anthropic-beta sent:", repr(captured["beta"]))
assert captured["beta"] is not None
assert "tool-search-tool-2025-10-19" in captured["beta"], captured["beta"]
assert "advanced-tool-use-2025-11-20" not in captured["beta"], captured["beta"]
print("OK — outbound header is exactly the rewritten value")
PY
```

Expected:

```
URL: https://example.invalid/v1/messages
anthropic-beta sent: 'tool-search-tool-2025-10-19'
OK — outbound header is exactly the rewritten value
```

### 4. (Optional) Verify suppress mode

```bash
python3 - <<'PY'
import httpx, litellm

captured = {}
orig_send = httpx.Client.send

def trap(self, request, **kw):
    captured["beta"] = request.headers.get("anthropic-beta")
    body = (b'{"id":"msg_test","type":"message","role":"assistant",'
            b'"content":[{"type":"text","text":"ok"}],'
            b'"stop_reason":"end_turn","model":"x",'
            b'"usage":{"input_tokens":1,"output_tokens":1}}')
    return httpx.Response(200, request=request, content=body,
                          headers={"content-type": "application/json"})

httpx.Client.send = trap

litellm.completion(
    model="anthropic/claude-sonnet-4-6",
    api_base="https://example.invalid",
    api_key="sk-test",
    max_tokens=8,
    anthropic_beta_overrides={"advanced-tool-use-2025-11-20": None},  # suppress
    tools=[{"type": "tool_search_tool_regex_20251119",
            "name": "tool_search_tool_regex"}],
    messages=[{"role": "user", "content": "hi"}],
)
print("anthropic-beta sent:", repr(captured["beta"]))
# Suppression removes the only beta -> header should be absent OR empty.
assert captured["beta"] is None or captured["beta"] == "", captured["beta"]
print("OK — null override suppresses the auto-injected beta")
PY
```

## Verification matrix

| Surface | Expected behavior with `{ "advanced-tool-use-2025-11-20": "tool-search-tool-2025-10-19" }` |
|---|---|
| Outbound HTTP `anthropic-beta` | Contains `tool-search-tool-2025-10-19`, NOT `advanced-tool-use-2025-11-20` |
| Bedrock-backed gateway response | HTTP 200, `finish_reason: tool_calls` |
| LiteLLM `BadRequestError: invalid beta flag` | Gone |
| User `extra_headers["anthropic-beta"]` (if user supplied one) | Untouched |
| Other auto-injected betas (e.g. `mcp-client-2025-04-04` if mcp tool also present) | Untouched |

## Regression guard

Run after every change touching `litellm/anthropic_beta_headers_manager.py`,
`litellm/llms/anthropic/common_utils.py`, or any of the 6 manager call
sites:

```bash
uv run pytest tests/test_litellm/test_anthropic_beta_overrides.py \
              tests/test_litellm/llms/anthropic/chat/test_anthropic_beta_overrides_threading.py \
              tests/test_litellm/test_anthropic_beta_headers_filtering.py -v
```

## Notes

- This case intentionally exercises a **real** Bedrock-backed gateway
  for steps 1 + 2. Steps 3 + 4 use httpx interception and need no
  network. Skip 1 + 2 in offline CI.
- The fix is **mechanism, not policy** — it does not change LiteLLM's
  default auto-injection behavior. Users who don't configure
  `anthropic_beta_overrides` see identical pre-fix behavior.
- For the broader debate ("should LiteLLM auto-inject
  `advanced-tool-use-2025-11-20` for the Anthropic route at all, now
  that tool search is GA on `api.anthropic.com`?") see PR discussion.
  Removing the auto-injection is a separate, breaking change.
