#!/usr/bin/env bash
# Case 33 — Real Anthropic streaming cancel → success_partial with
# real upstream-priced cost.
#
# The mock-based cases 26-32 verify the cancel-billing chain against a
# stdlib HTTP stub. This case is the *real* provider end-to-end check:
# proves the cursor=1 fix and the cancel-finalize plumbing both behave
# correctly when the chunk stream is from genuine Anthropic API
# infrastructure rather than the mock's deterministic SSE.
#
# Specifically catches regressions where:
#   - Real Anthropic's content_block_delta sequence has a structural
#     quirk the mock doesn't reproduce (extra event types,
#     server_tool_use blocks, citations, etc.) → cursor reset or chunk
#     reassembly silently fails.
#   - FallbackStreamWrapper's new chunk-accumulation on real long-tail
#     thinking streams leaks memory or duplicates chunks.
#   - The cost calculator runs on the partial response with a real
#     model id in the LiteLLM cost map → spend should be > 0 (not 0.0
#     because mock-anthropic is unpriced).
#
# Tier: real. Requires ANTHROPIC_API_KEY in e2e/.env. Cost: ~$0.01
# per run (Anthropic Sonnet ~1500 prompt + ~200 completion before
# cancel × $3/M input + $15/M output).

set -eu

PROXY_URL="${PROXY_URL:-http://localhost:4011}"
DB_CONTAINER="${DB_CONTAINER:-litellm-e2e-db}"
DB_USER="${DB_USER:-litellm}"
DB_NAME="${DB_NAME:-litellm}"
MASTER_KEY="${MASTER_KEY:-sk-e2e-test}"

# Tier=real preflight: skip if no Anthropic key.
ANTHROPIC_KEY="${ANTHROPIC_API_KEY:-}"
if [ -z "$ANTHROPIC_KEY" ]; then
    # Try to read from the proxy container so this case works against
    # a proxy started by e2e/tools/proxy (env file is mounted there).
    ANTHROPIC_KEY=$(docker exec litellm-e2e printenv ANTHROPIC_API_KEY 2>/dev/null || echo "")
fi
if [ -z "$ANTHROPIC_KEY" ]; then
    echo "SKIP: ANTHROPIC_API_KEY not set — case 33 needs real Anthropic credentials."
    exit 77
fi

USER_SENTINEL="case33-$(date +%s%N)"

# Build a prompt long enough that the response takes 5-10 seconds to
# fully stream — so a 3s curl --max-time gives Anthropic time to send
# message_start + content_block_delta chunks but NOT message_delta or
# message_stop. Exercises the cursor=1 + reassembly fallback path.
#
# Using a moderately long system prompt to also trigger prompt-cache
# creation tokens, which gives us a non-trivial cache_creation field
# in the row to assert on (extra signal beyond just completion_tokens).
LONG_USER="Write a comprehensive technical comparison of three programming languages: Go, Python, and Rust. For each language, cover: (1) memory model and garbage collection, (2) concurrency primitives, (3) typical compilation/execution speed, (4) ecosystem and tooling, (5) production deployment story. Provide concrete code examples for each section. Be thorough and detailed — write at least 1500 words."

echo "[33] real Anthropic streaming cancel..."

set +e
# 8s curl timeout: real Anthropic TTFT can be 2-4s on a 1500-token
# prompt; we need 8s to get past TTFT + a few seconds of streaming
# chunks before cancel. message_delta won't arrive in time (full
# generation would take 15-25s).
timeout 8 curl -sN -X POST "$PROXY_URL/v1/chat/completions" \
    -H "Authorization: Bearer $MASTER_KEY" \
    -H "Content-Type: application/json" \
    -d "$(python3 -c "
import json, sys
body = {
    'model': 'claude-sonnet-cache',
    'user': '$USER_SENTINEL',
    'stream': True,
    'max_tokens': 2000,
    'messages': [{'role': 'user', 'content': sys.argv[1]}],
}
print(json.dumps(body))
" "$LONG_USER")" > /tmp/case33.out 2>&1
RC=$?
set -e

BYTES=$(wc -c < /tmp/case33.out)
echo "  curl rc=$RC, bytes streamed: $BYTES"

# Anthropic TTFT varies (real network + provider load). If we got 0
# bytes the cancel hit before TTFT — we can still verify the cancel
# row was written (the prompt was billable input either way). If
# bytes > 0 but very small (< 100) AND curl exited cleanly (rc=0),
# that's a real upstream error response — fail explicitly so we
# don't silently mis-pass.
if [ "$RC" -eq 0 ] && [ "$BYTES" -lt 100 ]; then
    echo "FAIL: curl completed with $BYTES bytes — likely auth or model error"
    echo "--- response head ---"
    head -c 500 /tmp/case33.out
    echo "---"
    exit 1
fi

# Poll for the row. Real Anthropic + cost calc + spend log batch can
# take 5-10s total.
ROW=""
for i in $(seq 1 30); do
    sleep 1
    ROW=$(docker exec "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -tA -F'|' -c "
SELECT
    COALESCE(status, ''),
    COALESCE(completion_tokens::text, '0'),
    COALESCE(prompt_tokens::text, '0'),
    COALESCE(spend::text, '0'),
    COALESCE(metadata::jsonb->>'cancellation_indicator', ''),
    COALESCE(metadata::jsonb->>'cancel_phase', ''),
    COALESCE(metadata::jsonb->>'delivery_status', ''),
    COALESCE(metadata::jsonb->>'billing_status', '')
FROM \"LiteLLM_SpendLogs\"
WHERE end_user = '$USER_SENTINEL'
ORDER BY \"startTime\" DESC LIMIT 1;
" 2>/dev/null | head -1)
    [ -n "$ROW" ] && break
done

if [ -z "$ROW" ]; then
    echo "FAIL: no SpendLogs row for $USER_SENTINEL after 30s"
    exit 1
fi

IFS='|' read -r STATUS COMPL PROMPT SPEND IND PHASE DEL BIL <<< "$ROW"
echo "  row: status=$STATUS prompt=$PROMPT completion=$COMPL spend=$SPEND ind=$IND phase=$PHASE delivery=$DEL billing=$BIL"

OK=1

# Status taxonomy — binary-status: status="success" + marker, NOT the
# legacy "success_partial" sentinel. Cancel row classified by
# cancellation_indicator + derived delivery/billing dimensions.
if [ "$STATUS" != "success" ]; then
    echo "FAIL: expected status=success, got '$STATUS'"
    OK=0
fi
if [ "$IND" != "client_disconnect" ]; then
    echo "FAIL: expected cancellation_indicator=client_disconnect, got '$IND'"
    OK=0
fi
if [ "$PHASE" != "streaming_partial" ]; then
    echo "FAIL: expected cancel_phase=streaming_partial, got '$PHASE'"
    OK=0
fi

# Derived taxonomy. delivery_status / billing_status depend on whether
# bytes actually reached the client. With BYTES>0 we expect both
# partial; with BYTES=0 (cancel hit before TTFT) we expect both none.
if [ "${BYTES:-0}" -gt 0 ]; then
    if [ "$DEL" != "partial" ]; then
        echo "FAIL: expected delivery_status=partial (bytes>0), got '$DEL'"
        OK=0
    fi
    if [ "$BIL" != "partial" ]; then
        echo "FAIL: expected billing_status=partial (bytes>0), got '$BIL'"
        OK=0
    fi
else
    if [ "$DEL" != "none" ]; then
        echo "FAIL: expected delivery_status=none (bytes=0), got '$DEL'"
        OK=0
    fi
fi

# If we got streamed bytes, completion_tokens MUST exceed 1. If
# it's exactly 1, the cursor=1 fix isn't being applied to real
# Anthropic streams (a critical regression — the whole point of
# PR #1). If we got 0 bytes (TTFT > our budget), tolerate
# completion=0 — the cancel still fired before generation started.
if [ "${BYTES:-0}" -gt 0 ] && [ "${COMPL:-0}" -le 1 ]; then
    echo "FAIL: bytes=$BYTES but completion_tokens=$COMPL — cursor=1 fix"
    echo "      may have regressed against the real Anthropic streaming"
    echo "      protocol. Mock tests pass because the mock's chunk shape"
    echo "      exactly matches the fix's heuristic, but real Anthropic may"
    echo "      emit additional events that throw off saw_non_cursor_completion."
    OK=0
fi

# prompt_tokens must be > 0, reflecting real Anthropic's
# message_start.input_tokens reaching the row (NOT the local
# tokenizer fallback). Empirically our ~1500-char prompt comes in at
# 80-100 Anthropic tokens — much less than chars/4 because of
# vocabulary efficiency, but well above 0.
if [ "${PROMPT:-0}" -le 0 ]; then
    echo "FAIL: prompt_tokens=$PROMPT — Anthropic's message_start.input_tokens"
    echo "      should be > 0 (we sent a non-trivial user message). A 0 here"
    echo "      indicates the metadata bridge didn't carry the upstream"
    echo "      input_tokens through to the SpendLogs row."
    OK=0
fi

# spend > 0 only if we got bytes (partial response to cost). When
# bytes=0 the cost calc has nothing to price; spend=0 is fine and
# the row still proves the cancel taxonomy applied.
if [ "${BYTES:-0}" -gt 0 ]; then
    if ! python3 -c "import sys; sys.exit(0 if float('${SPEND:-0}') > 0 else 1)" 2>/dev/null; then
        echo "FAIL: spend=$SPEND with bytes=$BYTES — cost calc didn't price"
        echo "      the partial response. Either anthropic/claude-sonnet*"
        echo "      isn't in the cost map (model id mismatch) or the cost"
        echo "      callback ran before reassembly."
        OK=0
    fi
fi

if [ $OK -eq 1 ]; then
    echo "PASS: real Anthropic cancel billed at \$$SPEND with $COMPL completion_tokens"
    exit 0
else
    exit 1
fi
