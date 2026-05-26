# Case 19 — Anthropic invalid-thinking-signature retry is opt-in

## Goal

Verify the gateway behavior for Anthropic 400 `Invalid signature in thinking
block` errors:

1. **Default OFF (no flag, no header)** — the proxy must propagate the 400
   verbatim. Silently stripping thinking blocks and retrying would hide the
   underlying routing / key-rotation problem and drop reasoning context
   without the caller noticing.
2. **Per-request opt-in (`x-litellm-strip-thinking-on-signature-error: 1`)**
   — the proxy strips all `thinking` / `redacted_thinking` blocks from
   history, retries once, and surfaces `x-litellm-thinking-stripped: true`
   on the response. A WARNING log line is emitted with model + call_id.
3. **Observability** — when the strip retry succeeds, the Prometheus
   counter `litellm_anthropic_thinking_signature_retry_total{outcome="success"}`
   increments by 1.

Functional logic (gate, header parsing, signature-error detection) is
covered by `tests/test_litellm/llms/anthropic/test_anthropic_thinking_signature_retry.py`.
This e2e adds the wire-level confirmation against a real upstream.

## Origin

We run LiteLLM as a public gateway. When clients replay assistant history
across sessions, or when keys rotate behind the gateway, Anthropic returns
`messages.N.content.M: Invalid 'signature' in 'thinking' block`. The
previous behavior silently dropped thinking blocks and retried, which both
masked the underlying instability (multi-key rotation, non-sticky routing)
and degraded multi-turn reasoning quality without the caller knowing.

The new behavior is **off by default**: the 400 propagates so operators
notice it. Callers who genuinely want auto-recovery can opt in.

## Preconditions

- `e2e/tools/proxy status` reports `ready` against an image built AFTER the
  `fix/anthropic-thinking-signature-retry-config` branch
  (`e2e/tools/proxy build` if unsure).
- `ANTHROPIC_API_KEY` set in `e2e/.env` (cost: ~$0.001, three short
  `/v1/messages` calls — two are 400, the strip retry costs one
  successful Sonnet call).
- The upstream provider must propagate Anthropic's signature-error message
  verbatim. Pure pass-through gateways do; some translation layers
  reshape the error and Case 19 SKIPs with a clear diagnostic in that case.

## Steps

```bash
bash e2e/cases/data/19_anthropic_thinking_signature_retry_gate.sh
echo "exit=$?"
```

The fixture executes four assertions. Each prints `PASS:` or `FAIL:`:

- **A1 — Probe**: send `/v1/messages` with a fabricated thinking signature.
  If the upstream surfaces the canonical "Invalid signature in thinking
  block" message, A1 PASSes and the rest of the case runs. Otherwise the
  whole case SKIPs (the gateway has reshaped the error and Case 19 can't
  drive the retry path against this backend).
- **A2 — Default OFF propagates 400**: same request without any override
  → HTTP 400, NO `x-litellm-thinking-stripped` response header.
- **A3 — Header opt-in triggers strip + retry**: same request +
  `x-litellm-strip-thinking-on-signature-error: 1` → HTTP 200,
  `x-litellm-thinking-stripped: true` response header present.
- **A4 — Prometheus counter increments**: between A2 and A3, the metric
  `litellm_anthropic_thinking_signature_retry_total` (sum over labels)
  grows by ≥ 1.

## Expected — GREEN

```
A1 PASS: upstream surfaces canonical 'Invalid signature in thinking block' (HTTP 400)
A2 PASS: default OFF — HTTP 400, no x-litellm-thinking-stripped header
A3 PASS: header opt-in — HTTP 200, x-litellm-thinking-stripped: true present
A4 PASS: litellm_anthropic_thinking_signature_retry_total +1 (=N)
```

## Failure modes

| Symptom | Likely cause |
|---|---|
| A1 SKIP with non-matching 400 body | Upstream reshapes Anthropic's error — `is_anthropic_invalid_thinking_signature_error` can't match. Either fix the matcher or run this case against a direct-Anthropic endpoint |
| A2 returns 200 | **Regression** — default-OFF was bypassed; `should_retry_anthropic_messages_on_http_error` is firing without opt-in |
| A2 has `x-litellm-thinking-stripped` header | Header is being emitted even when the strip retry never ran — `get_custom_headers` flag check is wrong |
| A3 returns 400 | Header→param plumbing broken: `_get_strip_thinking_on_signature_error_from_request` not wired, or `data["strip_thinking_on_signature_error"]` isn't reaching `litellm_params` |
| A3 missing `x-litellm-thinking-stripped` header | `logging_obj.model_call_details["litellm_thinking_signature_stripped"]` not set in the retry helper, or `get_custom_headers` not reading it |
| A4 counter unchanged | PrometheusLogger callback not enabled (check `litellm_settings.callbacks: ["prometheus"]`), or `kwargs.get("litellm_thinking_signature_stripped")` not surfacing through standard_logging |

## Cross-reference

- `litellm/__init__.py` — module flag `anthropic_strip_thinking_on_signature_error`
- `litellm/llms/base_llm/anthropic_messages/transformation.py` — gate
- `litellm/llms/custom_httpx/llm_http_handler.py` — retry helper, WARN log,
  `model_call_details["litellm_thinking_signature_stripped"]` flag
- `litellm/proxy/litellm_pre_call_utils.py` — header extraction
- `litellm/proxy/common_request_processing.py` — response header
- `litellm/integrations/prometheus.py` — counter
- `tests/test_litellm/llms/anthropic/test_anthropic_thinking_signature_retry.py`
  — functional unit tests
