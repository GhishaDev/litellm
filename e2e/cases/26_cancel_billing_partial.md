# Case 26 — Cancel billing: stream + non-stream cancel produces partial-delivery SpendLogs row

## Goal

End-to-end verification of the Phase 1/2/3 billing-accuracy work
(commits `80127194b5…4dcdd73f1a`):

1. **Streaming cancel** with chunks received → SpendLogs row exists with
   `status="success"`, `metadata.cancellation_indicator="client_disconnect"`,
   derived `delivery_status="partial"` + `billing_status="partial"`, and
   real `spend > 0` (the chunks were reassembled and priced through the
   regular cost calculator).

2. **Non-streaming cancel** during upstream wait → shield-and-wait
   succeeds (mock returns quickly), SpendLogs row records
   `usage_source="upstream_completed_after_cancel"` with `delivery_status="none"`
   (nothing reached the client) and `billing_status="full"` (real
   upstream usage).

3. **Zero-chunk cancel** (client gives up before first byte) →
   SpendLogs row exists with `delivery_status="none"` (or "partial"
   if the mock managed to emit SSE openers before the cancel),
   `billing_status="none"`.

Before Phase 1 all three of these were black holes — no SpendLogs row
at all, an orphaned Langfuse trace, and the upstream provider had
already billed us. This case proves the bleed is plugged.

## Status taxonomy

The earlier iteration of Phase 1/2 used a tri-valued
`status="success_partial"` sentinel for these rows. Phase 3
(2026-06-10) refactored that away: the top-level `status` is now
binary `"success" | "failure"` (aligned with upstream LiteLLM), and the
cancellation taxonomy lives in metadata as two orthogonal dimensions:

- `delivery_status: full | partial | none` — what reached the client
- `billing_status:  full | partial | none` — what we actually charged

Both are derived from the existing 5 cancel markers
(`cancellation_indicator`, `cancel_phase`, `usage_source`,
`upstream_completed`, `bytes_delivered_to_client`) by
`spend_tracking_utils._derive_delivery_billing_status` — single source
of truth, never written directly.

## Tier

`mock-only` — uses the in-network `mock-anthropic` deployment with
`X-Mock-TTFT-Ms` + `X-Mock-Chunks` headers to deterministically
control stream pacing without real provider cost.

## What the fixture does

`e2e/cases/data/26_cancel_billing_partial.sh` runs three probes and
verifies the resulting SpendLogs rows:

| Probe | Request | Cancel via | Expected `delivery_status` | Expected `billing_status` | Expected `usage_source` |
|---|---|---|---|---|---|
| C1 | streaming `mock-anthropic`, TTFT=2000ms, ~50 chunks | curl `--max-time 4` (cancels after some chunks flushed) | `partial` | `partial` | `tokenizer_estimate` or `upstream_truth` |
| C2 | streaming `mock-anthropic`, more chunks accumulated | curl `--max-time 4` | `partial` | `partial` | as C1 |
| C3 | streaming `mock-anthropic`, TTFT=10000ms (cancel before first chunk of content) | curl `--max-time 1` | `partial` or `none` (depends on mock SSE-opener timing) | `none` (no real content reassembled) | `no_completion` |

Each probe uses a unique sentinel `end_user` value (`case26-<nanos>-c<N>`)
so the SpendLogs row is unambiguously identifiable.

For each probe the fixture polls `LiteLLM_SpendLogs` (up to 20s — the
async spend writer can lag, and the non-stream shield itself can
intentionally hold the request open for several seconds) and asserts:

- Exactly one row exists for that sentinel
- `status == 'success'` (NOT failure — cancel is not a system failure)
- `metadata->>'cancellation_indicator' == 'client_disconnect'`
- `metadata->>'cancel_phase'` is one of the expected lifecycle values
- `metadata->>'delivery_status'` and `metadata->>'billing_status'` match
  the per-probe expectation above
- `completion_tokens > 0` for C1/C2 (chunks reassembled) — proves the
  cost calculator ran on the partial response

## Why this is hard to test without a real proxy + DB

The cancel path traverses several independent pieces of plumbing:

1. **asyncio.CancelledError propagation** through the FastAPI request
   task and the streaming generator. Unit tests can fake this but
   the actual uvicorn event-loop behavior is what production sees.

2. **The shield+wait timer** in `finalize_non_stream_cancel` only
   makes sense when there's a real upstream task to await. Mocked
   tests can prove the shield logic; only a real HTTP roundtrip can
   prove the FastAPI request-cancel signal actually reaches the
   handler in time.

3. **The metadata bridge** from `logging_obj.model_call_details` →
   `request_data.litellm_params.metadata` → `_get_spend_logs_metadata`
   → derivation helper → `LiteLLM_SpendLogs.metadata` JSON column.
   Several hops; any one break and the markers (or the derived
   delivery/billing fields) vanish silently.

Unit tests cover each piece. This case proves the chain is wired.

## Reproducing manually (for debugging a future failure)

```bash
# Start the proxy with the mock provider
e2e/tools/proxy start --with-mock

# Probe C1: streaming, cancel mid-stream
USER_SENTINEL="case26-debug-$(date +%s)"
timeout 4 curl -N -X POST http://localhost:4011/v1/chat/completions \
  -H "Authorization: Bearer sk-e2e-test" \
  -H "Content-Type: application/json" \
  -H "X-Mock-TTFT-Ms: 500" \
  -H "X-Mock-Chunks: 50" \
  -H "X-Mock-TPS: 5" \
  -d "{
    \"model\":\"mock-anthropic\",
    \"user\":\"$USER_SENTINEL\",
    \"messages\":[{\"role\":\"user\",\"content\":\"Tell me about cancellation handling\"}],
    \"stream\":true,
    \"max_tokens\":2000
  }"

# Verify in DB
docker exec litellm-e2e-db psql -U litellm -d litellm -c "
SELECT request_id, status, spend, completion_tokens,
       metadata::jsonb->>'cancellation_indicator',
       metadata::jsonb->>'cancel_phase',
       metadata::jsonb->>'delivery_status',
       metadata::jsonb->>'billing_status',
       metadata::jsonb->>'usage_source'
FROM \"LiteLLM_SpendLogs\"
WHERE end_user = '$USER_SENTINEL'
ORDER BY \"startTime\" DESC LIMIT 1;
"
```

Expect a row with `status='success'`, `delivery_status='partial'`,
`billing_status='partial'`, `cancel_phase='streaming_partial'`, and
`completion_tokens > 0`.

## Cross-references

- `litellm/litellm_core_utils/cancel_finalize.py` — catch + dispatch
- `litellm/litellm_core_utils/cancel_billing.py` — partial cost + metadata bridge
- `litellm/proxy/spend_tracking/spend_tracking_utils.py:_derive_delivery_billing_status`
  — single source of truth for the orthogonal taxonomy
- `litellm/proxy/hooks/proxy_track_cost_callback.py` — failure-fallback
  cost compute (was hardcoded 0.0; now uses `compute_prompt_only_cost`)
- `tests/test_litellm/litellm_core_utils/test_cancel_finalize.py` — unit tests
- `tests/test_litellm/litellm_core_utils/test_cancel_billing.py` — unit tests
- `tests/test_litellm/proxy/spend_tracking/test_spend_logs_cancellation_metadata.py`
  — derivation + materialisation tests

## Tier classification

C (universal bug fix — every LiteLLM proxy operator hits the
cancellation black hole) + D (the orthogonal `delivery_status` /
`billing_status` derivation is a billing-policy choice we want carried
in our fork until upstream agrees on the semantic mapping).
