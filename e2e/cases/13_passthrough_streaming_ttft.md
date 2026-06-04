# Case 13 — Pass-through streaming TTFT must reflect real time-to-first-token

## Goal

Regression guard for two interacting bugs in
`litellm/proxy/pass_through_endpoints/streaming_handler.py` that
together collapsed `spend_logs.completionStartTime` onto
`spend_logs.endTime` (off by ~1ms of clock resolution, not literally
identical), making the streaming phase
(`endTime - completionStartTime`) round to roughly zero and TTFT
effectively soak up the entire request duration — for every streaming
request through any pass-through endpoint (`/v1/messages`,
`/vertex_ai/*`, `/gemini/*`, `/cohere/*`, etc.).

## Origin

Reported by an operator looking at the dashboard: for streaming calls
through `/v1/messages` (Anthropic pass-through), `Duration (s)` and
`TTFT (s)` columns showed near-identical values (off by ≤1 ms) —
including on multi-second completions where TTFT physically can't be
the entire request time.

## Root cause (verified end-to-end)

Two bugs in the shared `PassThroughStreamingHandler.chunk_processor`:

1. **`start_time` is captured too late.** The `start_time` argument
   handed to `chunk_processor` originates in
   `BaseAnthropicMessagesStreamingIterator.__init__`, which runs
   *after* the upstream HTTP response has already been received.
   So `SpendLogs.startTime` reflects "moment we started reading the
   stream", not "moment the client request entered the proxy" — the
   true TTFT window is silently subtracted from `Duration`.

2. **`completion_start_time` is never recorded.** The original chunk
   loop yielded bytes to the client and collected them for logging,
   but never noted when the first byte arrived. With
   `litellm_logging_obj.completion_start_time` left as `None`, the
   fallback at `litellm_logging.py:1834-1837` sets it to `end_time`
   — collapsing `completionStartTime` onto `endTime` and zeroing out
   the streaming phase.

Both bugs hide each other. Fixing only #2 leaves you with `TTFT ≈ 0`
and `Duration` deflated by ~TTFT; fixing only #1 leaves `TTFT ==
Duration`. Both must be fixed for the math to come out right.

## Numerical evidence (Anthropic claude-sonnet-4-6, 200-word stream)

| State | Duration | TTFT | streaming_phase | curl wall-clock |
|---|---|---|---|---|
| Bug present | 6528 ms | 6527 ms | 1 ms | 8769 ms |
| Bug #2 fixed only | 7698 ms | 14 ms | 7684 ms | 9893 ms |
| Both fixed | **8496 ms** | **2373 ms** | **6123 ms** | 8551 ms |
| Control (`/v1/chat/completions` transform, same model + prompt) | 8143 ms | 2071 ms | 6072 ms | 8214 ms |

After the fix, the pass-through path is within ~5% of the transform
path on the same upstream model — the two should report the same
streaming behavior because the underlying HTTP request is the same.

## Preconditions

- `e2e/tools/proxy status` reports `ready`
- `ANTHROPIC_API_KEY` set (this case uses a real ~200-word completion;
  cost ≈ $0.005 per run)

## Steps

```bash
bash e2e/cases/data/13_passthrough_streaming_ttft.sh
echo "exit=$?"
```

The fixture:

1. Sends a streaming POST to `/v1/messages` with `max_tokens=400` and a
   prompt that asks for a 200-word essay, so the stream runs for
   several seconds.
2. Sends an equivalent streaming POST to `/v1/chat/completions` as a
   parity reference for the transform path.
3. Waits 5 s for the async spend-logger to flush, then queries the
   two newest spend_logs rows.
4. Asserts on the `anthropic_messages` row:
   - `streaming_phase_ms > 1000`
   - `ttft_ms > 300`
   - `ttft_ms < duration_ms / 2`
5. Soft-warns if `anthropic_messages.ttft` differs from `acompletion.ttft`
   by more than 3× (provider cold-start variance can hit 2×, so this
   is informational, not a fail).

## Expected — GREEN (after fix lands)

```
A (/v1/messages):           wall=8551ms, bytes=4789
B (/v1/chat/completions):   wall=8214ms, bytes=5660

spend_logs rows:
  anthropic_messages      duration=  8496ms  ttft=  2373ms  streaming=  6123ms
  acompletion             duration=  8143ms  ttft=  2071ms  streaming=  6072ms

PASS: passthrough streaming TTFT is recorded correctly.
  /v1/messages    duration=8496ms ttft=2373ms streaming=6123ms
```

## Failure modes

| Symptom | Likely cause |
|---|---|
| `FAIL [assertion 1]: streaming_phase=1ms` | Bug #2 regressed — first-chunk arrival not recorded; `completion_start_time` fell back to `end_time` |
| `FAIL [assertion 2]: ttft=14ms` | Bug #1 regressed — `start_time` is captured at iterator `__init__` again, after upstream response; need the `litellm_logging_obj.start_time` override |
| `FAIL [assertion 3]: ttft > duration/2` | One of the two bugs has partially regressed; check both `chunk_processor` entry-time and first-chunk recording |
| `WARN: TTFT parity off — ...ratio=...` | Soft signal only — usually means the Anthropic path hit a cold cache on this run; rerun once to confirm |
| `FAIL: no anthropic_messages row` | Async spend logger backed up — increase the `sleep 5` to 10 |

## Cross-reference

- `litellm/proxy/pass_through_endpoints/streaming_handler.py` —
  fix lives at the top of `chunk_processor`'s `try:` block
- Case 12 — guards the cost-calc path for dashboard-added deployments
- Case 11 — guards observability for failure logging
- This case (13) — guards observability for streaming latency

All four (10/11/12/13) green means the cost + observability pipeline
is trustworthy end-to-end for both transform and pass-through paths.
