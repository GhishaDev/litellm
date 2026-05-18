#!/usr/bin/env bash
# Regression fixture for Case 13 — passthrough streaming TTFT must be a real
# time-to-first-token, not a copy of total Duration.
#
# Makes a streaming request to /v1/messages (Anthropic passthrough), waits for
# the spend_logs row to flush, and asserts:
#
#   1. streaming_phase_ms = endTime - completionStartTime > 1000 ms
#      (server actually streamed; this catches the "1ms streaming phase"
#       symptom where completion_start_time gets set to end_time)
#   2. ttft_ms = completionStartTime - startTime > 300 ms
#      (first chunk wasn't recorded at request entry time either)
#   3. ttft_ms < 0.5 * duration_ms
#      (TTFT shouldn't dominate Duration on a long-enough stream)
#
# Side-by-side parity vs /v1/chat/completions: both paths' TTFTs should be
# within 3x of each other (cache miss + LLM warm-up varies).
#
# Cost ~ $0.005 per run.

set -eu

PROXY_URL="${PROXY_URL:-http://localhost:4011}"
DB_CONTAINER="${DB_CONTAINER:-litellm-e2e-db}"
DB_USER="${DB_USER:-litellm}"
DB_NAME="${DB_NAME:-litellm}"
MASTER_KEY="${MASTER_KEY:-sk-e2e-test}"

# Unique seed so we can find exactly these requests later if needed
SENTINEL=$(date +%s%N)
PROMPT="Write a 200-word, three-paragraph essay about the history of clocks. Sentinel=$SENTINEL. Do not stop early."

# ---------- A. /v1/messages stream (the path that had the bug) ----------
A_RESP=$(mktemp)
A_START=$(date +%s%3N)
curl -sS -N -X POST "$PROXY_URL/v1/messages" \
    -H "x-api-key: $MASTER_KEY" \
    -H "anthropic-version: 2023-06-01" \
    -H "Content-Type: application/json" \
    -d "$(jq -n --arg p "$PROMPT" \
        '{model: "claude-sonnet-cache", messages: [{role: "user", content: $p}], max_tokens: 400, stream: true}')" \
    > "$A_RESP"
A_END=$(date +%s%3N)
A_WALL=$((A_END - A_START))
A_BYTES=$(wc -c < "$A_RESP")
rm -f "$A_RESP"

# ---------- B. /v1/chat/completions stream (control / parity ref) ----------
B_RESP=$(mktemp)
B_START=$(date +%s%3N)
curl -sS -N -X POST "$PROXY_URL/v1/chat/completions" \
    -H "Authorization: Bearer $MASTER_KEY" \
    -H "Content-Type: application/json" \
    -d "$(jq -n --arg p "$PROMPT" \
        '{model: "claude-sonnet-cache", messages: [{role: "user", content: $p}], max_tokens: 400, stream: true, stream_options: {include_usage: true}}')" \
    > "$B_RESP"
B_END=$(date +%s%3N)
B_WALL=$((B_END - B_START))
B_BYTES=$(wc -c < "$B_RESP")
rm -f "$B_RESP"

echo "A (/v1/messages):           wall=${A_WALL}ms, bytes=${A_BYTES}"
echo "B (/v1/chat/completions):   wall=${B_WALL}ms, bytes=${B_BYTES}"

if [ "$A_BYTES" -lt 500 ] || [ "$B_BYTES" -lt 500 ]; then
    echo "FAIL: one of the streams returned <500 bytes — upstream rejected the request"
    exit 1
fi

# Poll for both rows up to 30s — async spend logger flushes after the stream
# completes and the anthropic_messages row tends to lag the acompletion row
# by a few seconds.
ROW=""
for _ in $(seq 1 30); do
    sleep 1
    ROW=$(docker exec "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -tA -F'|' -c "
SELECT
    call_type,
    EXTRACT(EPOCH FROM (\"endTime\" - \"startTime\")) * 1000,
    EXTRACT(EPOCH FROM (\"completionStartTime\" - \"startTime\")) * 1000,
    EXTRACT(EPOCH FROM (\"endTime\" - \"completionStartTime\")) * 1000
FROM \"LiteLLM_SpendLogs\"
WHERE \"startTime\" > NOW() - INTERVAL '60 seconds'
  AND call_type IN ('anthropic_messages', 'acompletion')
ORDER BY \"startTime\" DESC
LIMIT 2;
")
    # Need at least one anthropic_messages row to assert on
    if echo "$ROW" | grep -q '^anthropic_messages'; then
        break
    fi
done

if [ -z "$ROW" ]; then
    echo "FAIL: no spend_logs row found in last 20s — async logger may be backed up"
    exit 1
fi

echo
echo "spend_logs rows:"
echo "$ROW" | awk -F'|' '{ printf "  %-22s duration=%6.0fms  ttft=%6.0fms  streaming=%6.0fms\n", $1, $2, $3, $4 }'
echo

A_DUR=$(echo "$ROW" | awk -F'|' '$1=="anthropic_messages" {print int($2); exit}')
A_TTFT=$(echo "$ROW" | awk -F'|' '$1=="anthropic_messages" {print int($3); exit}')
A_STREAM=$(echo "$ROW" | awk -F'|' '$1=="anthropic_messages" {print int($4); exit}')

B_TTFT=$(echo "$ROW" | awk -F'|' '$1=="acompletion" {print int($3); exit}')

if [ -z "$A_DUR" ]; then
    echo "FAIL: no anthropic_messages row found in last 20s"
    exit 1
fi

FAIL=0

# Assertion 1: streaming_phase must be >1s for a 200-word stream
if [ "$A_STREAM" -lt 1000 ]; then
    echo "FAIL [assertion 1]: streaming_phase=${A_STREAM}ms is too short."
    echo "  Expected >1000ms for a ~200-word completion."
    echo "  Likely cause: completion_start_time fell back to end_time"
    echo "  (litellm_logging.py:1834-1837 fallback fires when chunk_processor"
    echo "   never records first-chunk arrival). See bug fix in"
    echo "   litellm/proxy/pass_through_endpoints/streaming_handler.py."
    FAIL=1
fi

# Assertion 2: TTFT must be >300ms (real provider latency floor)
if [ "$A_TTFT" -lt 300 ]; then
    echo "FAIL [assertion 2]: ttft=${A_TTFT}ms is too small."
    echo "  Expected >300ms — real Anthropic API latency is normally 1-3 seconds."
    echo "  Likely cause: start_time passed into chunk_processor came from the"
    echo "   streaming iterator __init__ (which runs after upstream HTTP"
    echo "   response received), so first-chunk arrival is microseconds after."
    echo "  See litellm_logging_obj.start_time override in chunk_processor."
    FAIL=1
fi

# Assertion 3: TTFT must not dominate Duration
HALF_DUR=$((A_DUR / 2))
if [ "$A_TTFT" -gt "$HALF_DUR" ]; then
    echo "FAIL [assertion 3]: ttft=${A_TTFT}ms > duration/2=${HALF_DUR}ms."
    echo "  For a long-enough stream, TTFT should be a small fraction of Duration."
    echo "  Likely cause: the same fallback as assertion 1 (TTFT collapsed to Duration)."
    FAIL=1
fi

# Soft sanity: parity with the OpenAI transform path
if [ -n "$B_TTFT" ] && [ "$B_TTFT" -gt 0 ]; then
    # Use awk for float-safe ratio (ttft can vary 1.5x easily between calls)
    RATIO=$(awk "BEGIN { printf \"%.2f\", $A_TTFT / $B_TTFT }")
    if awk "BEGIN { exit !($A_TTFT > $B_TTFT * 3 || $B_TTFT > $A_TTFT * 3) }"; then
        echo "WARN: TTFT parity off — anthropic_messages=${A_TTFT}ms vs acompletion=${B_TTFT}ms (ratio=${RATIO})"
        echo "  Not a strict fail (provider cold-start variance), but worth investigating."
    fi
fi

if [ "$FAIL" -eq 0 ]; then
    echo "PASS: passthrough streaming TTFT is recorded correctly."
    echo "  /v1/messages    duration=${A_DUR}ms ttft=${A_TTFT}ms streaming=${A_STREAM}ms"
    exit 0
else
    exit 1
fi
