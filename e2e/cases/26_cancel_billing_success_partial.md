# Case 26 — Cancel billing: stream + non-stream cancel produces `success_partial` SpendLogs

## Goal

End-to-end verification of the Phase 1 billing-accuracy work
(commits `5e9be86…b1420d0`):

1. **Streaming cancel** with chunks received → SpendLogs row exists with
   `status="success_partial"`, real `spend > 0`, and the cancellation
   markers (`cancellation_indicator`, `cancel_phase`, `usage_source`)
   populated in `metadata`.

2. **Non-streaming cancel** during upstream wait → shield-and-wait
   succeeds (mock returns quickly), SpendLogs row records
   `usage_source="upstream_completed_after_cancel"` with `spend > 0`
   reflecting the real upstream usage we caught past the client
   disconnect.

3. **Zero-chunk cancel** (client gives up before first byte) →
   SpendLogs row exists with `usage_source="no_completion"` or the
   prompt-only fallback path, `spend > 0` (NOT the historical
   hardcoded 0.0).

Before Phase 1 all three of these were black holes — no SpendLogs row
at all, an orphaned Langfuse trace, and the upstream provider had
already billed us. This case proves the bleed is plugged.

## Tier

`mock-only` — uses the in-network `mock-anthropic` deployment with
`X-Mock-TTFT-Ms` + `X-Mock-Chunks` headers to deterministically
control stream pacing without real provider cost.

## What the fixture does

`e2e/cases/data/26_cancel_billing_success_partial.sh` runs three
probes and verifies the resulting SpendLogs rows:

| Probe | Request | Cancel via | Expected `status` | Expected `usage_source` |
|---|---|---|---|---|
| C1 | streaming `mock-anthropic`, TTFT=2000ms, ~50 chunks | curl `--max-time 4` (cancels after some chunks flushed) | `success_partial` | `tokenizer_estimate` or `upstream_truth` |
| C2 | non-stream `mock-anthropic`, full response in ~3s | curl `--max-time 1` (cancels before completion) | `success_partial` | `upstream_completed_after_cancel` (shield wait succeeds within 60s) |
| C3 | streaming `mock-anthropic`, TTFT=10000ms (cancel before first byte) | curl `--max-time 1` | `success_partial` | `no_completion` |

Each probe uses a unique sentinel API key (`sk-case26-c{N}-<nanos>`)
so the SpendLogs row is unambiguously identifiable via the hashed
`api_key` column.

For each probe the fixture polls `LiteLLM_SpendLogs` (up to 20s — the
async spend writer can lag, and the non-stream shield itself can
intentionally hold the request open for several seconds) and asserts:

- Exactly one row exists for that sentinel key
- `status == 'success_partial'`
- `metadata->>'cancellation_indicator' == 'client_disconnect'`
- `metadata->>'cancel_phase'` is one of the expected lifecycle values
- `metadata->>'usage_source'` matches the per-probe expectation above
- `spend > 0` (no more hardcoded zero on cancel)

## Why this is hard to test without a real proxy + DB

The cancel path traverses three independent pieces of plumbing:

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
   → `LiteLLM_SpendLogs.metadata` JSON column. Five hops; any one
   break and the markers vanish silently.

Unit tests cover each piece. This case proves the chain is wired.

## Reproducing manually (for debugging a future failure)

```bash
# Start the proxy with the mock provider
e2e/tools/proxy start --with-mock

# Provision a virtual key
KEY=$(e2e/tools/keys generate-virtual sk-test-cancel)

# Probe C1: streaming, cancel mid-stream
timeout 4 curl -N -X POST http://localhost:4011/v1/chat/completions \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -H "X-Mock-TTFT-Ms: 500" \
  -H "X-Mock-Chunks: 50" \
  -H "X-Mock-TPS: 5" \
  -d '{
    "model":"mock-anthropic",
    "messages":[{"role":"user","content":"Tell me about cancellation handling"}],
    "stream":true,
    "max_tokens":2000
  }'

# Verify in DB
docker exec litellm-e2e-db psql -U litellm -d litellm -c "
SELECT request_id, status, spend, 
       metadata::jsonb->>'cancellation_indicator',
       metadata::jsonb->>'cancel_phase',
       metadata::jsonb->>'usage_source'
FROM \"LiteLLM_SpendLogs\"
ORDER BY \"startTime\" DESC LIMIT 1;
"
```

Expect a row with `status='success_partial'`, `spend > 0`, and
`cancel_phase='streaming_partial'`.

## Cross-references

- `litellm/litellm_core_utils/cancel_finalize.py` — catch + dispatch
- `litellm/litellm_core_utils/cancel_billing.py` — partial cost + metadata bridge
- `litellm/proxy/hooks/proxy_track_cost_callback.py:125` — failure-fallback
  cost compute (was hardcoded 0.0)
- `tests/test_litellm/litellm_core_utils/test_cancel_finalize.py` — unit tests
- `tests/test_litellm/litellm_core_utils/test_cancel_billing.py` — unit tests

## Tier classification

C (universal bug fix — every LiteLLM proxy operator hits the
cancellation black hole) + D (the success_partial classification is a
billing-policy choice we want carried in our fork until upstream
agrees).
