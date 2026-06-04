#!/usr/bin/env bash
# Case 19 fixture — see e2e/cases/19_anthropic_thinking_signature_retry_gate.md
#
# Verifies the opt-in gate for Anthropic 400 'Invalid signature in thinking
# block':
#   A1: upstream actually surfaces the canonical signature error (probe)
#   A2: default OFF → 400 propagates, no x-litellm-thinking-stripped header
#   A3: header opt-in → 200 with x-litellm-thinking-stripped: true
#   A4: Prometheus counter increments
#
# Exits 0 on PASS, 77 on SKIP, anything else on FAIL.

set -u

PROXY="${PROXY_URL:-http://localhost:4011}"
KEY="${MASTER_KEY:-sk-e2e-test}"
MODEL="${MODEL_E2E_NAME:-claude-sonnet-cache}"
FAILED=0

pass() { echo "PASS: $*"; }
fail() { echo "FAIL: $*"; FAILED=1; }
skip() { echo "SKIP: $*"; exit 77; }

if ! curl -sSf -o /dev/null -m 3 "$PROXY/health/readiness"; then
    skip "proxy not ready at $PROXY"
fi

# /v1/messages body with a fabricated thinking signature. Real Anthropic
# (and pass-through gateways) reject with status=400,
# error.message containing "Invalid `signature` in `thinking` block".
read -r -d '' BODY <<JSON || true
{
  "model": "$MODEL",
  "max_tokens": 64,
  "messages": [
    {"role": "user", "content": "Say hi."},
    {"role": "assistant", "content": [
      {"type": "thinking", "thinking": "test-only fabricated thinking", "signature": "AAAA-not-a-real-signature-AAAA"},
      {"type": "text", "text": "Hi!"}
    ]},
    {"role": "user", "content": "Now say hi again."}
  ]
}
JSON

is_signature_error() {
    # Mirror litellm's matcher: lowercase containing all of
    # invalid + signature + thinking + block.
    local body_lower
    body_lower=$(tr '[:upper:]' '[:lower:]' < "$1")
    grep -q "invalid" <<<"$body_lower" \
        && grep -q "signature" <<<"$body_lower" \
        && grep -q "thinking" <<<"$body_lower" \
        && grep -q "block" <<<"$body_lower"
}

PROBE_BODY=$(mktemp); PROBE_HDRS=$(mktemp)
A3_BODY=""; A3_HDRS=""
trap 'rm -f "$PROBE_BODY" "$PROBE_HDRS" "${A3_BODY:-}" "${A3_HDRS:-}" 2>/dev/null' EXIT

# ---- A1: probe upstream actually surfaces the signature error -----------
PROBE_STATUS=$(curl -sS -D "$PROBE_HDRS" -o "$PROBE_BODY" -w "%{http_code}" \
    -H "Authorization: Bearer $KEY" \
    -H "Content-Type: application/json" \
    -d "$BODY" "$PROXY/v1/messages")

if [ "$PROBE_STATUS" = "401" ] || [ "$PROBE_STATUS" = "403" ]; then
    skip "probe got HTTP $PROBE_STATUS — ANTHROPIC_API_KEY likely missing/wrong"
fi
if [ "$PROBE_STATUS" != "400" ]; then
    echo "--- probe response (truncated) ---"
    head -c 600 "$PROBE_BODY"
    echo
    skip "probe returned HTTP $PROBE_STATUS, expected 400 — upstream may have accepted the malformed signature; cannot drive retry path"
fi
if ! is_signature_error "$PROBE_BODY"; then
    echo "--- probe response (truncated) ---"
    head -c 600 "$PROBE_BODY"
    echo
    skip "probe got 400 but body does not match Anthropic signature-error shape — upstream reshapes the error; case 19 cannot run against this backend"
fi
pass "upstream surfaces canonical 'Invalid signature in thinking block' (HTTP 400)"

# ---- A2: default OFF propagates the 400 verbatim ------------------------
# Same request without any override; reuse the probe response (it WAS the
# default-OFF case).
A2_BODY="$PROBE_BODY"
A2_HDRS="$PROBE_HDRS"

if grep -iq '^x-litellm-thinking-stripped:' "$A2_HDRS"; then
    fail "default-OFF response has x-litellm-thinking-stripped header (must only appear on opt-in success)"
    grep -i '^x-litellm-thinking-stripped' "$A2_HDRS" | sed 's/^/    /'
else
    pass "default OFF — HTTP 400, no x-litellm-thinking-stripped header"
fi

# ---- snapshot Prometheus counter BEFORE the strip retry -----------------
COUNTER_NAME="litellm_anthropic_thinking_signature_retry_total"

read_counter_sum() {
    local out
    # -L: /metrics 307-redirects to /metrics/. Auth header in case the
    # endpoint is gated (require_auth_for_metrics_endpoint).
    out=$(curl -sSfL -m 5 -H "Authorization: Bearer $KEY" \
        "$PROXY/metrics" 2>/dev/null || true)
    if [ -z "$out" ]; then
        echo "0"
        return
    fi
    # Sum every labeled series for the counter (any model/outcome).
    awk -v name="$COUNTER_NAME" '
        $1 == name && !seen_help { next }
        index($0, name) == 1 && $0 !~ /^#/ {
            v = $NF
            sum += v
        }
        END { printf("%d", sum + 0) }
    ' <<<"$out"
}

BEFORE=$(read_counter_sum)

# ---- A3: header opt-in triggers strip + retry ---------------------------
A3_BODY=$(mktemp); A3_HDRS=$(mktemp)
A3_STATUS=$(curl -sS -D "$A3_HDRS" -o "$A3_BODY" -w "%{http_code}" \
    -H "Authorization: Bearer $KEY" \
    -H "x-litellm-strip-thinking-on-signature-error: 1" \
    -H "Content-Type: application/json" \
    -d "$BODY" "$PROXY/v1/messages")

if [ "$A3_STATUS" != "200" ]; then
    fail "header opt-in returned HTTP $A3_STATUS, expected 200 (strip retry should have succeeded)"
    echo "--- response (truncated) ---"
    head -c 600 "$A3_BODY"
    echo
else
    if grep -iq '^x-litellm-thinking-stripped: *true' "$A3_HDRS"; then
        pass "header opt-in — HTTP 200, x-litellm-thinking-stripped: true present"
    else
        fail "header opt-in returned 200 but x-litellm-thinking-stripped header missing"
        echo "--- response headers (x-litellm-* only) ---"
        grep -i '^x-litellm-' "$A3_HDRS" | sed 's/^/    /'
    fi
fi

# ---- A4: Prometheus counter incremented ---------------------------------
# The counter is bumped from PrometheusLogger.async_log_success_event, which
# runs on the async logging path AFTER the HTTP response is returned. Poll up
# to 15s for the increment rather than assuming a fixed flush delay.
AFTER="$BEFORE"
for _ in $(seq 1 15); do
    sleep 1
    AFTER=$(read_counter_sum)
    [ "$((AFTER - BEFORE))" -ge 1 ] && break
done
DELTA=$((AFTER - BEFORE))
if [ "$DELTA" -ge 1 ]; then
    pass "$COUNTER_NAME +$DELTA (before=$BEFORE after=$AFTER)"
else
    fail "$COUNTER_NAME did not increment within 15s (before=$BEFORE after=$AFTER); strip retry should have logged outcome=success"
fi

exit $FAILED
