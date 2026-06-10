# Case 35 — Prometheus metric routing for cancellations

## Goal

End-to-end verification that PR #82's Layer 2 fix routes cancelled
requests through `litellm_spend_metric_total` and **suppresses** them
from `litellm_llm_api_failed_requests_metric_total`, against the real
Prometheus client state inside the running proxy.

Before PR #82:

- `prometheus.async_log_failure_event` unconditionally bumped
  `litellm_llm_api_failed_requests_metric_total` and did NOT touch
  `litellm_spend_metric_total`. Cancellations going through
  `_fallback_to_failure_hook` (zero-chunk streaming cancel,
  shield_timeout, no_completion) ended up polluting the failure-rate
  alert AND under-counting cancel revenue.

After PR #82:

- The same hook branches on
  `standard_logging_payload.metadata.cancellation_indicator`. When the
  marker is present, the cancel is routed into `litellm_spend_metric`
  and skipped from the failure counter.

## Tier

`mock-only` — uses the in-network `mock-anthropic` deployment +
`X-Mock-Fail: 503` to force a real failure as the control probe.

## Probes

The case fires two requests against the same `model=mock-claude`
upstream and snaps `litellm_llm_api_failed_requests_metric_total`
deltas across each:

| Probe | Request | Expected counter delta | DB row expectation |
|---|---|---|---|
| A | streaming `mock-anthropic`, client `timeout 3` mid-stream | `0` (cancel suppressed) | `status="success"` + `metadata.cancellation_indicator="client_disconnect"` |
| B | non-stream `mock-anthropic` + `X-Mock-Fail: 503` (control) | `>= 1` (real failure counted) | `status="failure"` + no marker |

The discrimination between A and B is the **load-bearing assertion**:
both probes share the same `model` / `team` / `api_key_alias` label
permutation, so the counter delta is purely a function of which branch
of `async_log_failure_event` fired. Probe B's `>=1` delta also proves
the fix isn't over-suppressing — non-cancel failures must still count.

## Why we don't assert spend metric VALUES in this case

The mock provider's model name (`mock-claude`) isn't in the litellm
cost map, so `compute_prompt_only_cost(...)` returns `0.0` even when
Layer 1 (the `_failure_handler_helper_fn` change in PR #82) fires
correctly. With cost=0, `litellm_spend_metric.inc(amount=0)` is a
no-op on the counter value — you can't distinguish "Layer 2 fired
with 0 cost" from "Layer 2 didn't fire" by inspecting the value
alone.

Spend-value verification lives elsewhere:

- `tests/test_litellm/litellm_core_utils/test_litellm_logging.py
  ::TestFailureHandlerCancelCost` — uses
  `anthropic/claude-haiku-4-5` (in the cost map), asserts
  `response_cost > 0` after `_failure_handler_helper_fn` runs with
  a `CancelledError`.
- `e2e/cases/data/33_real_anthropic_cancel.sh` — real provider,
  asserts the SpendLogs row carries the actual billed
  `prompt_tokens × upstream rate` figure.

## Reproducing manually

```bash
# Start the proxy with the mock provider
e2e/tools/proxy start --with-mock

# Baseline the failure counter
curl -sSL http://localhost:4011/metrics \
  | awk '/^litellm_llm_api_failed_requests_metric_total\{.*model="mock-claude"/ { sum+=$NF } END { print sum }'

# Fire a streaming cancel
timeout 3 curl -sN -X POST http://localhost:4011/v1/chat/completions \
  -H "Authorization: Bearer sk-e2e-test" \
  -H "X-Mock-TTFT-Ms: 500" -H "X-Mock-Chunks: 80" -H "X-Mock-TPS: 15" \
  -d '{"model":"mock-anthropic","stream":true,
       "messages":[{"role":"user","content":"hi"}],"max_tokens":2000}' \
  > /dev/null

# Re-snap the counter — delta must be 0
sleep 4
curl -sSL http://localhost:4011/metrics \
  | awk '/^litellm_llm_api_failed_requests_metric_total\{.*model="mock-claude"/ { sum+=$NF } END { print sum }'
```

## Failure-mode lookup

| Symptom | Likely cause |
|---|---|
| Probe A `delta != 0` | Layer 2 branch in `prometheus.async_log_failure_event` isn't recognising the `cancellation_indicator` marker — check `_slm_metadata.get("cancellation_indicator")` extraction |
| Probe B `delta < 1` | Layer 2 over-suppressing — non-cancel failures must still go through `litellm_llm_api_failed_requests_metric` |
| Probe A row `status=failure` | Cancel taxonomy regression in `proxy_track_cost_callback.async_post_call_failure_hook` — `_is_cancel` not detecting `CancelledError` |
| Probe B row `status=success` | `X-Mock-Fail: 503` not threading through to real exception — check `mock_provider.py` |

## Cross-references

- `litellm/integrations/prometheus.py:async_log_failure_event` — Layer 2 branch
- `litellm/litellm_core_utils/litellm_logging.py:_failure_handler_helper_fn` — Layer 1 cost pre-population
- `litellm/proxy/hooks/proxy_track_cost_callback.py` — `_is_cancel` detection + cancel marker preservation
- `e2e/cases/29_failure_not_polluted.md` — sibling case that proves the inverse direction (real failures stay in the failure bucket)

## Tier classification

C (universal observability fix — every LiteLLM proxy operator that
runs Prometheus would see the same counter pollution) + D (the
"cancels are not failures" semantic is the company opinion baked
into the Layer 2 branch).
