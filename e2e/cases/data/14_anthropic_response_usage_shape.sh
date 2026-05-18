#!/usr/bin/env bash
# Regression fixture for Case 14 — non-streaming /v1/messages response.usage
# must match the Anthropic spec, not leak OpenAI's `total_tokens`.
#
# Three assertions:
#   1. Non-streaming /v1/messages: usage must NOT contain `total_tokens`,
#      AND must contain `input_tokens` + `output_tokens` (sanity, so we
#      catch the case where the strip went too far).
#   2. Streaming /v1/messages message_delta.usage: must NOT contain
#      `total_tokens` (this has always been the Anthropic shape; we
#      codify it so a future change can't re-introduce divergence).
#   3. /v1/chat/completions (OpenAI shape): usage SHOULD contain
#      `total_tokens` (proves the strip is scoped to the Anthropic
#      passthrough endpoint, not global).
#
# Cost ~$0.001 per run (3 tiny calls, max_tokens=3 each).

set -eu

PROXY_URL="${PROXY_URL:-http://localhost:4011}"
MASTER_KEY="${MASTER_KEY:-sk-e2e-test}"

FAIL=0

# ----------------------------------------------------------------- 1
echo "[case 14] non-streaming /v1/messages..."
NS_RESP=$(curl -sS -X POST "$PROXY_URL/v1/messages" \
    -H "x-api-key: $MASTER_KEY" \
    -H "anthropic-version: 2023-06-01" \
    -H "Content-Type: application/json" \
    -d '{"model":"claude-sonnet-cache","messages":[{"role":"user","content":"hi"}],"max_tokens":3}')

NS_HAS_TOTAL=$(echo "$NS_RESP" | jq 'has("usage") and (.usage | has("total_tokens"))')
NS_HAS_INPUT=$(echo "$NS_RESP" | jq 'has("usage") and (.usage | has("input_tokens"))')
NS_HAS_OUTPUT=$(echo "$NS_RESP" | jq 'has("usage") and (.usage | has("output_tokens"))')
echo "  usage.total_tokens present?  $NS_HAS_TOTAL  (want: false)"
echo "  usage.input_tokens present?  $NS_HAS_INPUT  (want: true)"
echo "  usage.output_tokens present? $NS_HAS_OUTPUT (want: true)"

if [ "$NS_HAS_TOTAL" = "true" ]; then
    echo "FAIL [1]: non-streaming /v1/messages still has usage.total_tokens"
    echo "  Anthropic spec doesn't define this field. See"
    echo "  litellm/proxy/anthropic_endpoints/endpoints.py:"
    echo "  _strip_total_tokens_from_anthropic_response should strip it."
    FAIL=1
fi
if [ "$NS_HAS_INPUT" != "true" ] || [ "$NS_HAS_OUTPUT" != "true" ]; then
    echo "FAIL [1]: usage is missing input_tokens or output_tokens — strip went too far"
    FAIL=1
fi

# ----------------------------------------------------------------- 2
echo
echo "[case 14] streaming /v1/messages..."
STREAM_OUT=$(curl -sS -N -X POST "$PROXY_URL/v1/messages" \
    -H "x-api-key: $MASTER_KEY" \
    -H "anthropic-version: 2023-06-01" \
    -H "Content-Type: application/json" \
    -d '{"model":"claude-sonnet-cache","messages":[{"role":"user","content":"hi"}],"max_tokens":3,"stream":true}')

# Pull the message_delta event's data JSON
MD_USAGE=$(echo "$STREAM_OUT" | awk '
    /^data: \{.*"type":"message_delta"/ {
        sub(/^data: /, "")
        print
        exit
    }
')
if [ -z "$MD_USAGE" ]; then
    echo "FAIL [2]: no message_delta event in streaming response"
    FAIL=1
else
    SD_HAS_TOTAL=$(echo "$MD_USAGE" | jq '.usage | has("total_tokens") // false')
    echo "  message_delta.usage.total_tokens present? $SD_HAS_TOTAL (want: false)"
    if [ "$SD_HAS_TOTAL" = "true" ]; then
        echo "FAIL [2]: streaming message_delta.usage has total_tokens"
        echo "  Anthropic streaming spec doesn't define this field either."
        FAIL=1
    fi
fi

# ----------------------------------------------------------------- 3
echo
echo "[case 14] OpenAI-shape /v1/chat/completions (control: should keep total_tokens)..."
OAI_RESP=$(curl -sS -X POST "$PROXY_URL/v1/chat/completions" \
    -H "Authorization: Bearer $MASTER_KEY" \
    -H "Content-Type: application/json" \
    -d '{"model":"claude-sonnet-cache","messages":[{"role":"user","content":"hi"}],"max_tokens":3}')

OAI_HAS_TOTAL=$(echo "$OAI_RESP" | jq 'has("usage") and (.usage | has("total_tokens"))')
echo "  /v1/chat/completions usage.total_tokens present? $OAI_HAS_TOTAL (want: true)"
if [ "$OAI_HAS_TOTAL" != "true" ]; then
    echo "FAIL [3]: /v1/chat/completions lost total_tokens — strip is too aggressive."
    echo "  The strip should only apply to the Anthropic /v1/messages endpoint."
    FAIL=1
fi

echo
if [ "$FAIL" -eq 0 ]; then
    echo "PASS: /v1/messages usage shape matches Anthropic spec; /v1/chat/completions unchanged"
    exit 0
else
    exit 1
fi
