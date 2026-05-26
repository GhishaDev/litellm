#!/usr/bin/env bash
# Case 20 fixture — see e2e/cases/20_returned_model_name.md
#
# Asserts that litellm_params.returned_model_name (per-deployment) rewrites
# the `model` field returned to the client across:
#   A1 non-streaming /v1/messages           (top-level model)
#   A2 streaming     /v1/messages           (message_start.message.model)
#   A3 non-streaming /v1/chat/completions   (top-level model)
#   A4 streaming     /v1/chat/completions   (chunk.model in first chunk)
#
# Exits 0 on PASS, 77 on SKIP, anything else on FAIL.

set -u

PROXY="${PROXY_URL:-http://localhost:4011}"
KEY="${MASTER_KEY:-sk-e2e-test}"
MODEL="claude-renamed"
EXPECTED="public-name-for-clients"
FAILED=0

pass() { echo "PASS: $*"; }
fail() { echo "FAIL: $*"; FAILED=1; }
skip() { echo "SKIP: $*"; exit 77; }

if ! curl -sSf -o /dev/null -m 3 "$PROXY/health/readiness"; then
    skip "proxy not ready at $PROXY"
fi

# Sanity: deployment present? If render didn't pick up the new block, fail
# fast with a clear message instead of confusing 400s later.
if ! curl -sSf -m 5 -H "Authorization: Bearer $KEY" "$PROXY/v1/models" \
        2>/dev/null | grep -q '"id"[[:space:]]*:[[:space:]]*"'"$MODEL"'"'; then
    skip "deployment '$MODEL' missing from /v1/models — proxy needs restart to re-render config from updated tools/proxy"
fi

# Tiny prompt to minimize cost.
read -r -d '' BODY_MESSAGES <<JSON || true
{"model":"$MODEL","max_tokens":8,"messages":[{"role":"user","content":"hi"}]}
JSON
read -r -d '' BODY_MESSAGES_STREAM <<JSON || true
{"model":"$MODEL","max_tokens":8,"stream":true,"messages":[{"role":"user","content":"hi"}]}
JSON
read -r -d '' BODY_CHAT <<JSON || true
{"model":"$MODEL","max_tokens":8,"messages":[{"role":"user","content":"hi"}]}
JSON
read -r -d '' BODY_CHAT_STREAM <<JSON || true
{"model":"$MODEL","max_tokens":8,"stream":true,"messages":[{"role":"user","content":"hi"}]}
JSON

# ---- A1: non-streaming /v1/messages -------------------------------------
A1_BODY=$(mktemp); A1_HDRS=$(mktemp)
A1_STATUS=$(curl -sS -D "$A1_HDRS" -o "$A1_BODY" -w "%{http_code}" \
    -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
    -d "$BODY_MESSAGES" "$PROXY/v1/messages")
if [ "$A1_STATUS" = "401" ] || [ "$A1_STATUS" = "403" ]; then
    skip "/v1/messages got HTTP $A1_STATUS — ANTHROPIC_API_KEY likely missing"
fi
if [ "$A1_STATUS" != "200" ]; then
    fail "non-streaming /v1/messages returned HTTP $A1_STATUS"
    echo "--- body (truncated) ---"
    head -c 400 "$A1_BODY"; echo
else
    A1_MODEL=$(python3 -c "import json,sys; print(json.load(open('$A1_BODY')).get('model',''))" 2>/dev/null)
    if [ "$A1_MODEL" = "$EXPECTED" ]; then
        pass "non-streaming /v1/messages model=$A1_MODEL"
    else
        fail "non-streaming /v1/messages expected model=$EXPECTED, got '$A1_MODEL'"
    fi
fi

# ---- A2: streaming /v1/messages — message_start.message.model -----------
A2_BODY=$(mktemp)
curl -sN -o "$A2_BODY" \
    -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
    -d "$BODY_MESSAGES_STREAM" "$PROXY/v1/messages"
# Parse the SSE stream in python: find the first data: line whose JSON has
# type == "message_start" and extract message.model. Robust to either
# json.dumps-style spacing (after our rewrite) or the upstream's compact
# form (no spaces).
A2_MODEL=$(python3 - <<'PY' "$A2_BODY"
import json, sys
path = sys.argv[1]
with open(path) as f:
    raw = f.read()
for event in raw.split("\n\n"):
    for line in event.split("\n"):
        s = line.lstrip()
        if not s.startswith("data:"):
            continue
        payload = s[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except Exception:
            continue
        if isinstance(obj, dict) and obj.get("type") == "message_start":
            msg = obj.get("message") or {}
            print(msg.get("model", ""))
            sys.exit(0)
print("")
PY
)
if [ "$A2_MODEL" = "$EXPECTED" ]; then
    pass "streaming /v1/messages message_start.message.model=$A2_MODEL"
else
    fail "streaming /v1/messages expected message_start.message.model=$EXPECTED, got '$A2_MODEL'"
    echo "--- first SSE bytes (truncated) ---"
    head -c 400 "$A2_BODY"; echo
fi

# ---- A3: non-streaming /v1/chat/completions -----------------------------
A3_BODY=$(mktemp)
A3_STATUS=$(curl -sS -o "$A3_BODY" -w "%{http_code}" \
    -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
    -d "$BODY_CHAT" "$PROXY/v1/chat/completions")
if [ "$A3_STATUS" != "200" ]; then
    fail "non-streaming /v1/chat/completions returned HTTP $A3_STATUS"
    head -c 400 "$A3_BODY"; echo
else
    A3_MODEL=$(python3 -c "import json,sys; print(json.load(open('$A3_BODY')).get('model',''))" 2>/dev/null)
    if [ "$A3_MODEL" = "$EXPECTED" ]; then
        pass "non-streaming /v1/chat/completions model=$A3_MODEL"
    else
        fail "non-streaming /v1/chat/completions expected model=$EXPECTED, got '$A3_MODEL'"
    fi
fi

# ---- A4: streaming /v1/chat/completions — first chunk's model -----------
A4_BODY=$(mktemp)
curl -sN -o "$A4_BODY" \
    -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
    -d "$BODY_CHAT_STREAM" "$PROXY/v1/chat/completions"
A4_MODEL=$(grep -m1 '^data: ' "$A4_BODY" | sed -E 's/^data:\s*//' | python3 -c "
import json, sys
line = sys.stdin.read().strip()
if not line or line == '[DONE]':
    print('')
    sys.exit(0)
try:
    d = json.loads(line)
    print(d.get('model', ''))
except Exception:
    print('')
" 2>/dev/null)
if [ "$A4_MODEL" = "$EXPECTED" ]; then
    pass "streaming /v1/chat/completions chunk.model=$A4_MODEL"
else
    fail "streaming /v1/chat/completions expected chunk.model=$EXPECTED, got '$A4_MODEL'"
    head -c 400 "$A4_BODY"; echo
fi

rm -f "$A1_BODY" "$A1_HDRS" "$A2_BODY" "$A3_BODY" "$A4_BODY"
exit $FAILED
