# Case 21 — `/v1/messages` error responses must be Anthropic-shaped

## Goal

Regression guard for the Anthropic-shape error wrapper applied to
`POST /v1/messages` failures. Three-pronged assertion:

1. **Non-streaming `/v1/messages` 4xx** returns top-level Anthropic
   shape (`{type:"error", error:{type, message}}`), no `{"detail": ...}`
   wrapper, no OpenAI-only `param` / `code` fields.
2. **Streaming `/v1/messages` 4xx** (error raised before SSE starts)
   returns the same Anthropic-shaped JSON body — NOT an SSE stream.
3. **Control: `/v1/chat/completions` 4xx** still returns OpenAI shape
   (`{error: {message, type, param, code}}`) — proves the Anthropic
   wrapper is scoped to the Anthropic endpoint and didn't leak globally.

## Background

LiteLLM's `/v1/messages` endpoint used to wrap errors in the OpenAI
`ProxyException` envelope:

```json
{"error":{"message":"litellm.RateLimitError: AnthropicException - ...","type":"throttling_error","param":null,"code":"429"}}
```

The Anthropic API spec is different:

```json
{"type":"error","error":{"type":"rate_limit_error","message":"..."}}
```

Anthropic SDK clients pattern-match on `error.error.type` ∈
{`invalid_request_error`, `rate_limit_error`, `overloaded_error`, …}, so
the OpenAI envelope broke them. Fixed by routing errors on `/v1/messages`
through `AnthropicExceptionMapping.transform_to_anthropic_error()` and
returning via `JSONResponse` (not `HTTPException`, which would wrap the
body in a spurious `{"detail": ...}`). See
`litellm/proxy/anthropic_endpoints/endpoints.py:anthropic_response`.

The same root cause affected `/v1/messages/count_tokens` — also fixed in
the same change. Not exercised here (unit-tested instead).

## Preconditions

- `e2e/tools/proxy status` reports `ready` against an image built AFTER
  the `fix/anthropic-error-passthrough` branch (`e2e/tools/proxy build`
  if unsure).
- `ANTHROPIC_API_KEY` set is **not** required — all three tests trigger
  4xx via malformed request bodies that get rejected before reaching
  upstream.
- A routable Anthropic-model deployment (default e2e config provides
  `claude-sonnet-cache`).

## Steps

```bash
bash e2e/cases/data/21_anthropic_error_shape.sh
echo "exit=$?"
```

The fixture issues three calls. Each prints `PASS:` or `FAIL:`:

- **B1** non-streaming `/v1/messages` with malformed body (empty
  `messages: []`) → expects 4xx, top-level `{type:"error", error:{...}}`,
  no `detail`, no `error.param`, no `error.code`.
- **B2** streaming `/v1/messages` with the same malformed body +
  `stream:true` → expects 4xx with **JSON body** (NOT an SSE stream);
  same shape assertions as B1.
- **B3** non-streaming `/v1/chat/completions` (control) with malformed
  body → expects 4xx with OpenAI shape (top-level `{error:{...}}`,
  **no** top-level `type:"error"`); proves Anthropic wrapper is not
  global.

## Expected — GREEN

```
PASS B1 non-streaming /v1/messages (HTTP <non-2xx>): type=<upstream-error-type>
PASS B2 streaming /v1/messages (HTTP <non-2xx>): type=<upstream-error-type>
PASS B3: /v1/chat/completions error stayed OpenAI-shaped
```

`error.type` reflects whatever the upstream gateway returns (Anthropic
proper returns `invalid_request_error`; the `new-api` proxy returns
`new_api_error`; etc.). The assertion checks shape, not the specific
enum — that's covered by unit tests for the wrap path. Status code may
be 4xx or 5xx; some LiteLLM exception paths default to 500 when the
upstream status is not preserved (separate issue, outside this case's
scope).

## Failure modes

| Symptom | Likely cause |
|---|---|
| `FAIL B1: body has top-level 'detail' key` | Endpoint reverted to `raise HTTPException(detail=...)`; FastAPI default handler re-wraps. Must use `return JSONResponse(content=..., status_code=..., headers=...)`. |
| `FAIL B1: body missing top-level 'type'` / `error.type not in Anthropic enum` | The except block isn't routing through `AnthropicExceptionMapping.transform_to_anthropic_error()` — check `anthropic_response` in `litellm/proxy/anthropic_endpoints/endpoints.py`. |
| `FAIL B1: body has error.param or error.code` | OpenAI-only fields leaked back in. The Anthropic schema is `{type, message}` only — `param`/`code` are OpenAI semantics. |
| `FAIL B2: response is SSE / Content-Type text/event-stream` | Streaming pre-call validation is somehow returning a 200 SSE before erroring. Error-before-SSE-starts must short-circuit to a JSON 4xx. |
| `FAIL B3: chat/completions error has top-level type=error` | The Anthropic wrapper leaked into the OpenAI endpoint. Verify the wrapper change is scoped to `anthropic_endpoints/endpoints.py` and not in shared error middleware. |
| All three tests fail with `proxy not ready` | `e2e/tools/proxy rebuild` after pulling this branch. |

## Cross-reference

- Case 14 — guards success-path `usage` shape on `/v1/messages`
- Case 19 — guards a specific 400 (invalid thinking signature) and
  its retry header semantics
- `litellm/proxy/anthropic_endpoints/endpoints.py:anthropic_response` —
  the error path under test
- `litellm/anthropic_interface/exceptions/exception_mapping_utils.py` —
  `transform_to_anthropic_error()` + `_strip_litellm_wrapper_prefixes()`
- `tests/test_litellm/proxy/test_anthropic_error_passthrough.py` —
  unit-level TestClient coverage of the same paths
