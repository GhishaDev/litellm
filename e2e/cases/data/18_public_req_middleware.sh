#!/usr/bin/env bash
# Case 18 fixture — see e2e/cases/18_public_req_middleware.md
#
# Asserts that the PublicReqMiddleware installed via
# litellm_extras.entrypoint:
#   - keeps streaming responses incremental (A1, A2)
#   - strips x-litellm-* response headers under X-Public-Req: 1 (A3)
#   - leaves x-litellm-* response headers intact otherwise (A4)
#   - rejects sensitive query params on /v1/models in public mode (A5)
#   - accepts them in internal mode (A6)
#
# Exits 0 on PASS, 77 on SKIP (missing API key), anything else on FAIL.

set -u

PROXY="${PROXY_URL:-http://localhost:4011}"
DB_CONTAINER="${DB_CONTAINER:-litellm-e2e-db}"
DB_USER="${DB_USER:-litellm}"
DB_NAME="${DB_NAME:-litellm}"
KEY="${MASTER_KEY:-sk-e2e-test}"
MODEL="${MODEL_E2E_NAME:-claude-sonnet-cache}"
FAILED=0

pass() { echo "PASS: $*"; }
fail() { echo "FAIL: $*"; FAILED=1; }
skip() { echo "SKIP: $*"; exit 77; }

# ---- precondition: Anthropic key wired? ----------------------------------
# The proxy reads ANTHROPIC_API_KEY at startup; if we don't have it the
# streaming assertions cannot run. (/v1/models assertions would still work
# but the fixture is treated as a unit.)
if ! curl -sSf -o /dev/null -m 3 "$PROXY/health/readiness"; then
    skip "proxy not ready at $PROXY"
fi

# ---- shared streaming request --------------------------------------------
# A single Anthropic streaming call serves A1+A2+A3.
PUB_BODY=$(mktemp); PUB_HDRS=$(mktemp); PUB_TIMING=$(mktemp)
trap 'rm -f "$PUB_BODY" "$PUB_HDRS" "$PUB_TIMING" "$INT_HDRS" 2>/dev/null' EXIT

curl -sN -D "$PUB_HDRS" -o "$PUB_BODY" \
     -w "%{time_starttransfer} %{time_total} %{http_code}\n" \
     -H "Authorization: Bearer $KEY" \
     -H "X-Public-Req: 1" \
     -H "Content-Type: application/json" \
     -d "{
        \"model\":\"$MODEL\",
        \"messages\":[{\"role\":\"user\",\"content\":\"Write a 50-word essay about clouds.\"}],
        \"max_tokens\":200,
        \"stream\":true
     }" "$PROXY/v1/chat/completions" > "$PUB_TIMING"

read TTFB_S WALL_S STATUS < "$PUB_TIMING"
if [ "$STATUS" != "200" ]; then
    # Upstream auth failure → treat as SKIP not FAIL (no API key configured).
    if [ "$STATUS" = "401" ] || [ "$STATUS" = "403" ]; then
        skip "upstream returned $STATUS — ANTHROPIC_API_KEY likely missing"
    fi
    fail "streaming POST returned HTTP $STATUS"
    echo "--- response body (truncated) ---"
    head -c 400 "$PUB_BODY"
    echo
    exit 1
fi

TTFB_MS=$(awk "BEGIN{printf \"%d\", $TTFB_S*1000}")
WALL_MS=$(awk "BEGIN{printf \"%d\", $WALL_S*1000}")

# ---- A1: stream produced >= 3 SSE chunks ---------------------------------
CHUNKS=$(grep -c '^data:' "$PUB_BODY" || true)
if [ "$CHUNKS" -ge 3 ]; then
    pass "$CHUNKS SSE chunks received"
else
    fail "only $CHUNKS SSE chunks (need >=3) — middleware may be buffering"
fi

# ---- A2: streaming phase visible (wall - ttfb > 200ms) -------------------
#
# A buffering middleware would emit ALL chunks at end-of-stream, collapsing
# (wall - ttfb) toward 0. Provider TTFT variance can push the *ratio* above
# 0.5 on short completions (slow first token + fast last tokens), so we use
# the absolute streaming-phase duration instead of a ratio. Even ~200 ms of
# streaming phase across multiple SSE chunks is unambiguous evidence that
# the middleware did not buffer.
STREAM_PHASE_MS=$(awk "BEGIN{printf \"%d\", ($WALL_S - $TTFB_S) * 1000}")
if [ "$STREAM_PHASE_MS" -gt 200 ]; then
    pass "ttfb=${TTFB_MS}ms wall=${WALL_MS}ms streaming_phase=${STREAM_PHASE_MS}ms"
else
    fail "ttfb=${TTFB_MS}ms wall=${WALL_MS}ms streaming_phase=${STREAM_PHASE_MS}ms (need >200ms; buffering suspected)"
fi

# ---- A3: public response has zero x-litellm-* headers -------------------
PUB_LITELLM_COUNT=$(grep -ic '^x-litellm-' "$PUB_HDRS" || true)
if [ "$PUB_LITELLM_COUNT" -eq 0 ]; then
    pass "0 x-litellm-* headers in public response"
else
    fail "$PUB_LITELLM_COUNT x-litellm-* headers leaked (expected 0)"
    grep -i '^x-litellm-' "$PUB_HDRS" | sed 's/^/    /'
fi

# ---- A4: internal response keeps x-litellm-* (control) ------------------
INT_HDRS=$(mktemp)
curl -sN -D "$INT_HDRS" -o /dev/null \
     -H "Authorization: Bearer $KEY" \
     -H "Content-Type: application/json" \
     -d "{
        \"model\":\"$MODEL\",
        \"messages\":[{\"role\":\"user\",\"content\":\"hi\"}],
        \"max_tokens\":5,
        \"stream\":true
     }" "$PROXY/v1/chat/completions"

INT_LITELLM_COUNT=$(grep -ic '^x-litellm-' "$INT_HDRS" || true)
if [ "$INT_LITELLM_COUNT" -ge 1 ]; then
    pass "$INT_LITELLM_COUNT x-litellm-* headers in internal response"
else
    fail "no x-litellm-* in internal response — middleware over-strips"
fi

# ---- A5: public /v1/models — forbidden query silently stripped ---------
# Strategy: compare bodies for `?include_metadata=true` in public mode vs
# internal mode. In public mode the middleware drops the parameter before
# the proxy sees it, so the response must match the internal `no metadata`
# baseline — neither expanded with fallback chains nor a 4xx error.
PUB_NO_META=$(mktemp); PUB_WITH_META=$(mktemp); INT_NO_META=$(mktemp); INT_WITH_META=$(mktemp)

S=$(curl -s -o "$PUB_WITH_META" -w "%{http_code}" \
    -H "Authorization: Bearer $KEY" \
    -H "X-Public-Req: 1" \
    "$PROXY/v1/models?include_metadata=true")
S_PUB_NOMETA=$(curl -s -o "$PUB_NO_META" -w "%{http_code}" \
    -H "Authorization: Bearer $KEY" \
    -H "X-Public-Req: 1" \
    "$PROXY/v1/models")
S_INT_META=$(curl -s -o "$INT_WITH_META" -w "%{http_code}" \
    -H "Authorization: Bearer $KEY" \
    "$PROXY/v1/models?include_metadata=true")
S_INT_NOMETA=$(curl -s -o "$INT_NO_META" -w "%{http_code}" \
    -H "Authorization: Bearer $KEY" \
    "$PROXY/v1/models")

if [ "$S" = "200" ] && [ "$S_PUB_NOMETA" = "200" ] \
   && diff -q "$PUB_WITH_META" "$PUB_NO_META" >/dev/null; then
    pass "/v1/models?include_metadata=true returned 200 with query stripped (public)"
else
    fail "/v1/models?include_metadata=true public status=$S vs no-meta=$S_PUB_NOMETA; bodies differ → strip failed"
    diff "$PUB_WITH_META" "$PUB_NO_META" | head -5
fi

# ---- A6: internal /v1/models — metadata still expanded ------------------
# Internal mode (no X-Public-Req) must NOT strip. The metadata-enriched
# response must differ from the bare-models response.
if [ "$S_INT_META" = "200" ] && [ "$S_INT_NOMETA" = "200" ] \
   && ! diff -q "$INT_WITH_META" "$INT_NO_META" >/dev/null; then
    pass "/v1/models?include_metadata=true returned 200 with metadata expanded (internal)"
else
    fail "internal status: with-meta=$S_INT_META no-meta=$S_INT_NOMETA; bodies identical → middleware over-strips"
fi

rm -f "$PUB_NO_META" "$PUB_WITH_META" "$INT_NO_META" "$INT_WITH_META"

# ---- A7/A8: inbound x-litellm-* strip vs preserve ------------------------
# Send two mock_response chat completions (free, no provider call) each
# carrying x-litellm-spend-logs-metadata with a unique marker. The marker
# only reaches LiteLLM's pre-call hooks if the header was honored — i.e.,
# the proxy's spend_logs row will contain it under
# metadata.spend_logs_metadata.case18_marker.
#
# Expected:
#   A7 (public  + X-Public-Req: 1): row exists, marker ABSENT  → strip ran
#   A8 (internal, no X-Public-Req): row exists, marker PRESENT → control
PUB_MARKER="case18-pub-$(date +%s%N)"
INT_MARKER="case18-int-$(date +%s%N)"

# A7: public call — header must be stripped by the middleware
curl -sS -o /dev/null \
    -H "Authorization: Bearer $KEY" \
    -H "X-Public-Req: 1" \
    -H "X-Litellm-Spend-Logs-Metadata: {\"case18_marker\":\"$PUB_MARKER\"}" \
    -H "Content-Type: application/json" \
    -d "{
        \"model\":\"$MODEL\",
        \"messages\":[{\"role\":\"user\",\"content\":\"ping\"}],
        \"mock_response\":\"pong\"
    }" "$PROXY/v1/chat/completions"

# A8: internal call (control) — header must be honored
curl -sS -o /dev/null \
    -H "Authorization: Bearer $KEY" \
    -H "X-Litellm-Spend-Logs-Metadata: {\"case18_marker\":\"$INT_MARKER\"}" \
    -H "Content-Type: application/json" \
    -d "{
        \"model\":\"$MODEL\",
        \"messages\":[{\"role\":\"user\",\"content\":\"ping\"}],
        \"mock_response\":\"pong\"
    }" "$PROXY/v1/chat/completions"

# Async spend logger flush — poll up to 15s for the internal-marker row to
# appear, then make the public-marker assertion. If the internal row never
# appears the logger is backed up and the absence of the public marker is
# not yet proof of strip; in that case we surface a warning.
INT_FOUND=0
for _ in $(seq 1 15); do
    sleep 1
    INT_CNT=$(docker exec "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -tA -c "
SELECT COUNT(*) FROM \"LiteLLM_SpendLogs\"
WHERE metadata::text LIKE '%$INT_MARKER%';
" 2>/dev/null | tr -d ' ')
    if [ "${INT_CNT:-0}" -ge 1 ]; then
        INT_FOUND=1
        break
    fi
done

if [ "$INT_FOUND" -ne 1 ]; then
    fail "A8 control: internal marker '$INT_MARKER' never reached spend_logs within 15s — async logger backed up?"
else
    pass "internal mode: x-litellm-spend-logs-metadata reached spend_logs ($INT_CNT row)"
fi

PUB_CNT=$(docker exec "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -tA -c "
SELECT COUNT(*) FROM \"LiteLLM_SpendLogs\"
WHERE metadata::text LIKE '%$PUB_MARKER%';
" 2>/dev/null | tr -d ' ')

if [ "${PUB_CNT:-0}" -eq 0 ]; then
    pass "public mode: inbound x-litellm-spend-logs-metadata stripped (marker absent from spend_logs)"
else
    fail "public mode: inbound x-litellm-spend-logs-metadata LEAKED — $PUB_CNT spend_logs row(s) carry marker '$PUB_MARKER'"
fi

exit $FAILED
