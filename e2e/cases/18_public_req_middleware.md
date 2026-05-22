# Case 18 — `X-Public-Req` middleware: streaming + header / query gating

## Goal

Verify the `PublicReqMiddleware` (installed via
`litellm_extras.entrypoint`) correctly differentiates external vs internal
requests **without buffering streaming bodies**.

Two-pronged check:

1. **Streaming integrity.** SSE chunks arrive incrementally; TTFB is
   small relative to total wall time. This is the regression guard
   against accidentally switching to `BaseHTTPMiddleware` or otherwise
   awaiting on body messages — both would collapse TTFB onto end-of-stream.
2. **Differential behavior.** `X-Public-Req: 1` strips `x-litellm-*`
   from response headers and blocks `/v1/models?include_metadata=true`.
   The same requests without the header pass through unchanged.

Functional logic (inbound header strip, case-insensitive matching,
forbidden-query parsing, non-HTTP scope handling) is covered by
`tests/test_litellm/test_public_req_middleware.py`. This e2e adds the
wire-level confirmation.

## Origin

We expose the LiteLLM proxy to external users behind a public Nginx
ingress. The ingress injects `X-Public-Req: 1` on every forwarded
request; internal services reach the proxy on a separate Service that
does not pass through the ingress. The middleware sits in the proxy's
ASGI stack and applies safeguards conditional on that header.

## Preconditions

- `e2e/tools/proxy status` reports `ready`
- The proxy image was built **after** `litellm_extras/` was added —
  run `e2e/tools/proxy rebuild` if unsure
- `ANTHROPIC_API_KEY` set (one ~50-word streaming completion;
  cost ≈ $0.002)

## Steps

```bash
bash e2e/cases/data/18_public_req_middleware.sh
echo "exit=$?"
```

The fixture executes six assertions. Each prints `PASS:` or `FAIL:`:

### Streaming integrity (paid)

- **A1** SSE response yields ≥ 3 `data:` chunks
- **A2** Streaming phase (wall − TTFB) > 200 ms — chunks spread over
  time rather than dumped at end-of-stream. Provider TTFT variance can
  push the ratio above 0.5 on short completions, so the absolute
  duration is more robust than a ratio threshold; a buffering
  middleware would collapse this to ≤ 5 ms regardless of provider.

### Header gating (paid)

- **A3** Public mode: zero `x-litellm-*` headers in response
- **A4** Internal mode (no `X-Public-Req`): at least one
  `x-litellm-*` header in response (control)

### Query gating on `/v1/models` (free)

- **A5** Public mode: `GET /v1/models?include_metadata=true` returns
  HTTP 200 with the **same** body as `GET /v1/models` (forbidden query
  silently stripped before reaching the proxy).
- **A6** Internal mode: same request returns HTTP 200 with a body that
  **differs** from the bare-models response (metadata expansion still
  works for internal callers).

### Inbound `x-litellm-*` strip (free — uses `mock_response`)

Two chat completions are made with `mock_response: "pong"` (no upstream
provider call). Each carries `X-Litellm-Spend-Logs-Metadata` with a
unique marker. LiteLLM persists honored header values to
`metadata.spend_logs_metadata` in the spend_logs row, so the marker's
presence/absence in Postgres is a definitive signal of whether the
header reached the proxy core.

- **A7** Internal mode (control): marker **must** appear in spend_logs
  within 15 s (proves the header would otherwise be honored)
- **A8** Public mode (`X-Public-Req: 1`): marker **must not** appear in
  any spend_logs row (proves the middleware stripped the header before
  LiteLLM saw it)

## Expected — GREEN

```
A1 PASS: 7 SSE chunks received
A2 PASS: ttfb=8294ms wall=10171ms streaming_phase=1877ms
A3 PASS: 0 x-litellm-* headers in public response
A4 PASS: 7 x-litellm-* headers in internal response
A5 PASS: /v1/models?include_metadata=true returned 200 with query stripped (public)
A6 PASS: /v1/models?include_metadata=true returned 200 with metadata expanded (internal)
A7 PASS: internal mode: x-litellm-spend-logs-metadata reached spend_logs (1 row)
A8 PASS: public mode: inbound x-litellm-spend-logs-metadata stripped (marker absent from spend_logs)
```

## Failure modes

| Symptom | Likely cause |
|---|---|
| A1 fails with 0-1 chunks | Middleware is buffering — switched to `BaseHTTPMiddleware`, or `send_wrapper` is awaiting on body |
| A2 `streaming_phase ≤ 5ms` | Middleware buffered the body and dumped it on close — same root cause as A1 |
| A3 fails (still see `x-litellm-*` in public response) | `send_wrapper` not wired, or middleware not installed; check `proxy logs` for `PublicReqMiddleware` |
| A4 fails (no `x-litellm-*` in internal) | Middleware is stripping for *all* requests; check `_is_public` returns False without the header |
| A5 fails — public bodies differ between `?include_metadata=true` and bare `/v1/models` | Query strip did not run — `/v1/models` not in `MODELS_PATHS`, middleware not installed, or `parse_qsl`/`urlencode` lost the rewrite |
| A5 fails — public returns 4xx | Middleware reverted to the old reject-with-400 behavior; revert the strip refactor |
| A6 fails — internal bodies identical | Middleware running for internal calls — `_is_public` defaulting to True; strip is happening when it shouldn't |
| A7 fails — internal marker missing from spend_logs | Async spend logger lag or DB schema drift — not a middleware bug. Raise the 15 s poll if reproducible |
| A8 fails — public marker present in spend_logs | Inbound `x-litellm-*` strip is NOT running. Verify the middleware is installed and that `LITELLM_HEADER_PREFIX` matching is case-insensitive |

## Cross-reference

- `litellm_extras/public_req_middleware.py` — middleware under test
- `litellm_extras/entrypoint.py` — wrapper that installs the middleware
- `e2e/_config/docker-compose.yml` — `command:` invokes the wrapper
- `tests/test_litellm/test_public_req_middleware.py` — functional unit tests
