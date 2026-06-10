#!/usr/bin/env bash
# Case 35 — Prometheus metric routing for cancellations.
#
# Phase 3 + PR #82 fixed the metric gap where cancellations going
# through proxy_logging_obj.post_call_failure_hook (zero-chunk streaming
# cancel, shield_timeout, no_completion) were:
#   - incrementing litellm_llm_api_failed_requests_metric_total
#     (polluting failure-rate alerts)
#   - NOT incrementing litellm_spend_metric_total
#     (under-counting cancel revenue)
#
# This case validates the corrected ROUTING TOPOLOGY against the real
# callback dispatch chain — no Counter mocking, no
# async_log_failure_event monkey-patching (both of which would tend
# toward theater tests).
#
# What we can test in mock mode
# -----------------------------
# The mock provider's model name (`mock-claude`) isn't in the litellm
# cost map, so the spend metric VALUE stays 0 regardless of whether
# Layer 2's `litellm_spend_metric.inc(amount=cost)` fires (you can't
# distinguish "Layer 2 fired with cost=0" from "Layer 2 didn't fire at
# all" by inspecting the counter value alone). What we CAN observe
# deterministically is the failure-counter discrimination:
#
#   - cancel  → failed_requests counter MUST NOT increment
#   - 503     → failed_requests counter MUST increment by 1
#
# That two-probe discrimination is the load-bearing assertion: it
# proves Layer 2 is branching on the cancellation_indicator marker.
#
# Spend-VALUE verification (Layer 1's compute_prompt_only_cost
# populating SLP.response_cost) is locked elsewhere:
#   - tests/test_litellm/litellm_core_utils/test_litellm_logging.py
#     ::TestFailureHandlerCancelCost — real Anthropic Haiku in the cost
#     map, asserts response_cost > 0
#   - e2e/cases/data/33_real_anthropic_cancel.sh — real provider, real
#     billed spend on the SpendLogs row

set -eu

PROXY_URL="${PROXY_URL:-http://localhost:4011}"
DB_CONTAINER="${DB_CONTAINER:-litellm-e2e-db}"
DB_USER="${DB_USER:-litellm}"
DB_NAME="${DB_NAME:-litellm}"
MASTER_KEY="${MASTER_KEY:-sk-e2e-test}"
MOCK_CONTAINER="${MOCK_CONTAINER:-litellm-e2e-mock}"

if ! docker exec "$MOCK_CONTAINER" python3 -c \
        "import urllib.request; urllib.request.urlopen('http://localhost:8080/healthz')" 2>/dev/null; then
    echo "SKIP: $MOCK_CONTAINER not up (run with --with-mock)"
    exit 77
fi

# Sum a Prometheus counter series across all labels matching a
# substring filter. The mock e2e environment is single-tenant so
# filtering on `model="mock-claude"` is enough to isolate this case's
# traffic from any background series.
sum_metric() {
    local metric="$1" filter_substr="${2:-}"
    curl -sSL "$PROXY_URL/metrics" 2>/dev/null \
        | awk -v m="^${metric}{" -v f="$filter_substr" '
            $0 ~ m {
                if (f == "" || index($0, f) > 0) {
                    sum += $NF + 0
                }
            }
            END { printf "%.6f\n", sum }
        '
}

USER_A="case35-cancel-$(date +%s%N)"
USER_B="case35-failure-$(date +%s%N)"
MODEL_FILTER='model="mock-claude"'

# ------------------------------------------------------------------ baseline
F0=$(sum_metric litellm_llm_api_failed_requests_metric_total "$MODEL_FILTER")
echo "[35] failed_requests baseline (model=mock-claude): $F0"

# ------------------------------------------------------------------ PROBE A: cancel
echo "[35] Probe A — streaming cancel..."
timeout 3 curl -sN -X POST "$PROXY_URL/v1/chat/completions" \
    -H "Authorization: Bearer $MASTER_KEY" \
    -H "Content-Type: application/json" \
    -H "X-Mock-TTFT-Ms: 500" \
    -H "X-Mock-Chunks: 80" \
    -H "X-Mock-TPS: 15" \
    -d "{
      \"model\":\"mock-anthropic\",
      \"user\":\"$USER_A\",
      \"messages\":[{\"role\":\"user\",\"content\":\"explain partial cancellation in detail\"}],
      \"stream\":true,
      \"max_tokens\":2000
    }" > /dev/null 2>&1 || true

# Let the failure_hook + SpendLogs write + Prometheus inc settle.
sleep 4

F_AFTER_A=$(sum_metric litellm_llm_api_failed_requests_metric_total "$MODEL_FILTER")
DELTA_A=$(python3 -c "print(int(round(float('$F_AFTER_A') - float('$F0'))))")
echo "[35] failed_requests after probe A: $F_AFTER_A (delta=$DELTA_A)"

# ------------------------------------------------------------------ PROBE B: forced 503 failure (control)
echo "[35] Probe B — forced upstream 503 (control)..."
curl -sS -X POST "$PROXY_URL/v1/chat/completions" \
    -H "Authorization: Bearer $MASTER_KEY" \
    -H "Content-Type: application/json" \
    -H "X-Mock-Fail: 503" \
    -d "{
      \"model\":\"mock-anthropic\",
      \"user\":\"$USER_B\",
      \"messages\":[{\"role\":\"user\",\"content\":\"control probe\"}],
      \"stream\":false,
      \"max_tokens\":50
    }" > /dev/null 2>&1 || true

sleep 4

F_AFTER_B=$(sum_metric litellm_llm_api_failed_requests_metric_total "$MODEL_FILTER")
DELTA_B=$(python3 -c "print(int(round(float('$F_AFTER_B') - float('$F_AFTER_A'))))")
echo "[35] failed_requests after probe B: $F_AFTER_B (delta=$DELTA_B)"

# ------------------------------------------------------------------ DB-side row checks
ROW_A=""; ROW_B=""
for i in $(seq 1 20); do
    sleep 1
    ROW_A=$(docker exec "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -tA -F'|' -c "
SELECT COALESCE(status,''), COALESCE(metadata::jsonb->>'cancellation_indicator','')
FROM \"LiteLLM_SpendLogs\" WHERE end_user = '$USER_A'
ORDER BY \"startTime\" DESC LIMIT 1;
" 2>/dev/null | head -1)
    ROW_B=$(docker exec "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -tA -F'|' -c "
SELECT COALESCE(status,''), COALESCE(metadata::jsonb->>'cancellation_indicator','')
FROM \"LiteLLM_SpendLogs\" WHERE end_user = '$USER_B'
ORDER BY \"startTime\" DESC LIMIT 1;
" 2>/dev/null | head -1)
    if [ -n "$ROW_A" ] && [ -n "$ROW_B" ]; then break; fi
done

IFS='|' read -r STATUS_A IND_A <<< "$ROW_A"
IFS='|' read -r STATUS_B IND_B <<< "$ROW_B"
echo "[35] DB row A (cancel):   status=$STATUS_A ind=$IND_A"
echo "[35] DB row B (failure):  status=$STATUS_B ind=$IND_B"

OK=1

# === Cancel must NOT pollute the failure counter (the keystone assertion) ===
if [ "$DELTA_A" -ne 0 ]; then
    echo "FAIL [A.metric]: cancel polluted litellm_llm_api_failed_requests_metric_total"
    echo "      delta=$DELTA_A expected=0 (cancel is not a system failure)"
    echo "      → Layer 2 branch in prometheus.async_log_failure_event isn't suppressing cancels"
    OK=0
fi

# === Cancel must produce a proper DB row ===
if [ "$STATUS_A" != "success" ] || [ "$IND_A" != "client_disconnect" ]; then
    echo "FAIL [A.db]: expected status=success + cancellation_indicator=client_disconnect"
    echo "      got status=$STATUS_A ind=$IND_A"
    OK=0
fi

# === Failure-side discrimination (the control: real 503 DOES increment) ===
if [ "$DELTA_B" -lt 1 ]; then
    echo "FAIL [B.metric]: forced 503 did NOT increment failed_requests_metric"
    echo "      delta=$DELTA_B expected >= 1"
    echo "      → Layer 2 might be over-suppressing — non-cancel failures must still count"
    OK=0
fi
if [ "$STATUS_B" != "failure" ] || [ -n "$IND_B" ]; then
    echo "FAIL [B.db]: expected status=failure + no cancellation marker"
    echo "      got status=$STATUS_B ind='$IND_B'"
    OK=0
fi

if [ $OK -eq 1 ]; then
    echo "PASS: cancel suppressed from failed_requests_metric; real 503 still counts as failure"
    exit 0
else
    exit 1
fi
