# Case 33 — Real Anthropic streaming cancel → partial-delivery SpendLogs row

## Goal

End-to-end verification of the Phase 1/2/3 cancel-billing chain
against the *real* Anthropic API, not the in-network mock. Mock-based
cases 26-32 prove the plumbing wires together correctly; case 33
proves it survives contact with genuine Anthropic streaming protocol
quirks and real cost-map pricing.

## Why this can't be a mock case

The mock provider emits a fixed SSE shape:
`message_start → content_block_start → content_block_delta × N → content_block_stop → message_delta → message_stop`

Real Anthropic streams can include:
- Multiple parallel `content_block_*` blocks (thinking + text + tool_use interleaved)
- `signature_delta` events for thinking signatures
- Per-block `cache_control` markers in message_start
- Service-tier tags that affect `usage_object` shape
- Cache creation token details (`ephemeral_5m_input_tokens` vs
  `ephemeral_1h_input_tokens`)

Any of these can break the cursor=1 reset heuristic (PR #1) or the
chunk reassembly path (PR #2) in subtle ways that the mock doesn't
reproduce.

## What the fixture exercises

`e2e/cases/data/33_real_anthropic_cancel.sh`:

1. POST a streaming `/v1/chat/completions` request to the proxy with
   `model=claude-sonnet-cache` (mapped to real
   `anthropic/claude-sonnet-4-6` in the rendered config) and a ~1500-
   token prompt + `max_tokens=2000`.
2. Kill the client at 3 seconds via `timeout 3 curl ...`. Anthropic's
   thinking-model long-tail makes it very likely that
   `message_delta` won't have arrived by then — exercises the cursor
   reset path on a real chunk stream.
3. Poll `LiteLLM_SpendLogs` for the row keyed by the per-run sentinel
   in the OpenAI `user` field (lands in the `end_user` column).
4. Assert:
   - `status = 'success'` (Phase 3 taxonomy: cancel is not a system failure)
   - `metadata.cancellation_indicator = 'client_disconnect'`
   - `metadata.cancel_phase = 'streaming_partial'`
   - `metadata.delivery_status = 'partial'` (derived — chunks reached client)
   - `metadata.billing_status = 'partial'` (derived — partial response was priced)
   - `completion_tokens > 1` — proves PR #1's cursor reset triggered
     against the real Anthropic stream and the partial response was
     reassembled with a real-looking token count, not the cursor=1
     placeholder
   - `prompt_tokens > 0` — Anthropic's `message_start.input_tokens`
     reached the row, not the local tokenizer fallback
   - `spend > 0` — the cost map lookup found
     `anthropic/claude-sonnet-*` and the cost callback ran on the
     partial response

## What a failure means

| Failure mode | Likely cause |
|---|---|
| `status` is `failure` not `success` | Cancel was misclassified as failure — recheck `proxy_track_cost_callback._is_cancel` detection of CancelledError |
| `cancellation_indicator` empty | Cancel markers not propagating — recheck `enrich_request_metadata_with_cancel_markers` for new endpoint variants |
| `delivery_status` is `none` despite bytes streamed | `bytes_delivered_to_client` not populated — check `async_streaming_data_generator`'s `chunks_yielded` counter and `mark_logging_obj_cancelled` call sites |
| `billing_status` is `none` despite completion_tokens > 0 | Derivation rule mismatched — check `_derive_delivery_billing_status` against the 7-row semantic mapping |
| `completion_tokens = 1` | Cursor=1 reset isn't firing for real Anthropic; `saw_non_cursor_completion` heuristic broken |
| `completion_tokens = 0` | `stream_chunk_builder` failed to reassemble — chunks list empty (could be `FallbackStreamWrapper` not accumulating, or Logging.streaming_chunks ref drift) |
| `prompt_tokens = 0` | Anthropic `message_start.usage` not reaching the cost calc; check the metadata bridge |
| `spend = 0` | Either the model id leaked through to a cost-map miss, or the cost calc fired before reassembly |

## Tier

`real` — requires `ANTHROPIC_API_KEY` set in `e2e/.env`. Costs roughly
$0.01 per run (Anthropic Sonnet pricing: 1500 input × $3/M + ~200
output × $15/M ≈ $0.0075).

When `ANTHROPIC_API_KEY` is unset the fixture exits 77 (SKIP) so it's
safe in `--mock-only` runs.

## How to run

```bash
# 1. Make sure ANTHROPIC_API_KEY is in e2e/.env
# 2. Start the proxy WITHOUT --with-mock (real provider mode)
e2e/tools/proxy stop
e2e/tools/proxy start

# 3. Run case 33 directly (or the full suite)
bash e2e/cases/data/33_real_anthropic_cancel.sh
# OR
e2e/tools/run-all-cases     # runs everything; case 33 sits at the end
```

## Cross-references

- `litellm/litellm_core_utils/streaming_chunk_builder_utils.py` —
  cursor=1 reset (PR #1)
- `litellm/litellm_core_utils/cancel_finalize.py` — catch + dispatch
- `litellm/litellm_core_utils/cancel_billing.py` — markers bridge
- `litellm/proxy/common_request_processing.py` — `/v1/messages` catch
  + non-stream disconnect watcher (Phase 2)
- `litellm/proxy/spend_tracking/spend_tracking_utils.py:_derive_delivery_billing_status`
  — single source of truth for the orthogonal taxonomy (Phase 3)
- `e2e/cases/26_cancel_billing_partial.md` — mock-only companion that
  covers the same three sub-scenarios

## Tier classification

Operates against a real provider for the cancel-billing PRs (Tier C
universal bug fix + Tier D opinionated billing mechanism). Catches
real-provider regressions the mock suite cannot.
