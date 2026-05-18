#!/usr/bin/env bash
# Regression fixture for Case 03 — Anthropic prompt-cache READ on the
# second request increments litellm_prompt_cache_read_tokens_metric.
#
# Pattern:
#   1. First call with seed S + stable user_id U creates a cache entry
#      (cache_creation > 0).
#   2. Second call with the SAME seed S and SAME user_id U within the TTL
#      window hits the cache (cache_read > 0). The metric must increase
#      by at least the cache_read_input_tokens delta.
#
# Why the user_id matters
# -----------------------
# The corp Anthropic gateway (maasapi.*) round-robins anonymous requests
# across multiple upstream API keys, each with its own per-account cache
# namespace. So two byte-identical requests from "no one" land on
# different upstreams and cache_read never hits. Anthropic's official
# `user` / `metadata.user_id` field is what the gateway uses for sticky
# routing — same user_id → same upstream → cache shared. This is also
# the behavior visible in prod (only requests carrying device_id in the
# `end_user` field ever show cache_read>0 in spend_logs).
#
# Cost ~$0.002 per run.

set -eu

PROXY_URL="${PROXY_URL:-http://localhost:4011}"

snap() {
    local provider="$1"
    curl -sSL "$PROXY_URL/metrics" 2>/dev/null \
        | awk -v p="^litellm_prompt_cache_read_tokens_metric_total{.*api_provider=\"${provider}\"" \
            '$0 ~ p { for(i=1;i<=NF;i++) if($i+0==$i) val=$i } END { print (val ? val : 0) }'
}

SEED="case03-$$-$(date +%s)"
USER_ID="case03-user-$SEED"

# 1. First call — populates cache under this user_id's upstream account
echo "[case 03] populating cache (seed=$SEED, user_id=$USER_ID)..."
FIRST=$(e2e/tools/call --provider anthropic --cache ephemeral --ttl 5m \
    --prompt-tokens 1500 --seed "$SEED" --user-id "$USER_ID" 2>/dev/null)
FIRST_CC=$(echo "$FIRST" | jq -r '.response.usage.cache_creation_input_tokens // 0')
FIRST_CR=$(echo "$FIRST" | jq -r '.response.usage.cache_read_input_tokens // 0')
echo "  first call: cache_creation=$FIRST_CC, cache_read=$FIRST_CR"

if [ "$FIRST_CC" -lt 100 ]; then
    echo "FAIL: first call didn't create a cache entry (cache_creation=$FIRST_CC)"
    echo "  Either Anthropic rejected cache_control, or prompt was too short."
    exit 1
fi

# 2. Snapshot read metric before second call
BEFORE=$(snap anthropic)

# 3. Second call — same seed and user_id, should hit cache on the first try
sleep 3   # let the upstream cache entry settle
SECOND=$(e2e/tools/call --provider anthropic --cache ephemeral --ttl 5m \
    --prompt-tokens 1500 --seed "$SEED" --user-id "$USER_ID" 2>/dev/null)
SECOND_CR=$(echo "$SECOND" | jq -r '.response.usage.cache_read_input_tokens // 0')
SECOND_CC=$(echo "$SECOND" | jq -r '.response.usage.cache_creation_input_tokens // 0')
echo "  second call: cache_creation=$SECOND_CC, cache_read=$SECOND_CR"

if [ "$SECOND_CR" -eq 0 ]; then
    echo "FAIL: cache_read_input_tokens=0 on second call with sticky user_id."
    echo "  Either the gateway doesn't honor metadata.user_id for sticky LB"
    echo "  (different from the configuration prod uses), or the gateway"
    echo "  routed to a different upstream this time. Verify by bypassing"
    echo "  the proxy and hitting \$ANTHROPIC_API_BASE/v1/messages directly"
    echo "  with the same payload + metadata.user_id twice."
    exit 1
fi

# 4. Verify metric incremented by at least cache_read_input_tokens
sleep 2
AFTER=$(snap anthropic)
DELTA=$(awk "BEGIN { print $AFTER - $BEFORE }")

echo "  metric before: $BEFORE"
echo "  metric after:  $AFTER"
echo "  delta:         $DELTA  (expected >= $SECOND_CR)"

if awk "BEGIN { exit !($DELTA >= $SECOND_CR - 1) }"; then
    echo "PASS: cache_read=$SECOND_CR, metric_delta=$DELTA"
    exit 0
else
    echo "FAIL: metric delta $DELTA < usage.cache_read_input_tokens $SECOND_CR"
    exit 1
fi
