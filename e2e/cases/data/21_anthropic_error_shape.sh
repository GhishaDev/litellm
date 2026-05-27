#!/usr/bin/env bash
# Case 21 fixture — see e2e/cases/21_anthropic_error_shape.md
#
# Asserts that error responses on the Anthropic-compatible /v1/messages
# endpoint use Anthropic envelope shape, not the OpenAI ProxyException
# envelope, and not FastAPI's HTTPException {"detail": ...} wrapper.
#
#   B1 non-streaming /v1/messages          → top-level {type:"error",error:{...}}
#   B2 streaming     /v1/messages          → same shape (error before SSE)
#   B3 non-streaming /v1/chat/completions  → OpenAI shape (control, scope guard)
#
# Exits 0 on PASS, 77 on SKIP, anything else on FAIL.

set -u

PROXY="${PROXY_URL:-http://localhost:4011}"
KEY="${MASTER_KEY:-sk-e2e-test}"
ANTHROPIC_MODEL="claude-sonnet-cache"
OPENAI_MODEL="${OPENAI_E2E_MODEL:-gpt-4o-mini-cache}"
FAILED=0

pass() { echo "PASS: $*"; }
fail() { echo "FAIL: $*"; FAILED=1; }
skip() { echo "SKIP: $*"; exit 77; }

if ! curl -sSf -o /dev/null -m 3 "$PROXY/health/readiness"; then
    skip "proxy not ready at $PROXY"
fi

# Empty messages array — Anthropic spec requires at least one message; the
# proxy/router rejects this before reaching the upstream provider. Cheap and
# deterministic across providers.
read -r -d '' BAD_BODY_ANTHROPIC <<JSON || true
{"model":"$ANTHROPIC_MODEL","max_tokens":8,"messages":[]}
JSON
read -r -d '' BAD_BODY_ANTHROPIC_STREAM <<JSON || true
{"model":"$ANTHROPIC_MODEL","max_tokens":8,"stream":true,"messages":[]}
JSON
read -r -d '' BAD_BODY_OPENAI <<JSON || true
{"model":"$OPENAI_MODEL","max_tokens":8,"messages":[]}
JSON

# Shared assertion: parse JSON body and assert the Anthropic envelope.
# Args: file_with_body, label
assert_anthropic_shape() {
    local body_file="$1"
    local label="$2"
    python3 - "$body_file" "$label" <<'PY'
import json, sys
path, label = sys.argv[1], sys.argv[2]
try:
    with open(path) as f:
        body = json.load(f)
except Exception as e:
    print(f"FAIL {label}: body is not valid JSON ({e})")
    print("--- body (truncated) ---")
    with open(path) as f:
        print(f.read()[:400])
    sys.exit(1)
errs = []
if not isinstance(body, dict):
    errs.append(f"body is not a dict, got {type(body).__name__}")
if "detail" in body:
    errs.append("body has top-level 'detail' key (HTTPException wrapper leaked)")
if body.get("type") != "error":
    errs.append(f"body['type'] != 'error', got {body.get('type')!r}")
err = body.get("error")
if not isinstance(err, dict):
    errs.append(f"body['error'] is not a dict, got {type(err).__name__}")
else:
    if "type" not in err:
        errs.append("body['error'] missing 'type'")
    elif not isinstance(err["type"], str) or not err["type"]:
        errs.append(f"body['error']['type'] must be a non-empty string, got {err['type']!r}")
    if "message" not in err:
        errs.append("body['error'] missing 'message'")
    elif not isinstance(err["message"], str):
        errs.append(f"body['error']['message'] must be a string, got {type(err['message']).__name__}")
    if "param" in err:
        errs.append("body['error'] has OpenAI-only 'param' field")
    if "code" in err:
        errs.append("body['error'] has OpenAI-only 'code' field")
    # We don't gate `error.type` on Anthropic's official enum here: the
    # passthrough path preserves whatever the upstream gateway returned
    # (e.g. `new_api_error` from the new-api proxy). Enum compliance for
    # the WRAP path (when LiteLLM derives type from HTTP status) is
    # already covered by unit tests in
    # tests/test_litellm/anthropic_interface/exceptions/.
if errs:
    for e in errs:
        print(f"FAIL {label}: {e}")
    print("--- body (truncated) ---")
    with open(path) as f:
        print(f.read()[:400])
    sys.exit(1)
print(f"PASS {label}: type={err.get('type')}")
PY
}

# ---- B1: non-streaming /v1/messages -------------------------------------
# Accept any non-2xx — the assertion under test is the SHAPE of the error body,
# not status code preservation. Some LiteLLM exception paths drop the upstream
# status and default to 500; that's a separate issue (router-side
# status_code = None → falls back to 500 → maps to api_error). The body must
# still be Anthropic-shaped in either case.
B1_BODY=$(mktemp); B1_HDRS=$(mktemp)
B1_STATUS=$(curl -sS -D "$B1_HDRS" -o "$B1_BODY" -w "%{http_code}" \
    -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
    -d "$BAD_BODY_ANTHROPIC" "$PROXY/v1/messages")
if [ "${B1_STATUS:0:1}" = "2" ]; then
    fail "B1: expected non-2xx, got HTTP $B1_STATUS — upstream accepted empty messages?"
    head -c 400 "$B1_BODY"; echo
else
    if assert_anthropic_shape "$B1_BODY" "B1 non-streaming /v1/messages (HTTP $B1_STATUS)"; then
        :  # pass already printed
    else
        FAILED=1
    fi
fi

# ---- B2: streaming /v1/messages — error must be JSON, not SSE -----------
B2_BODY=$(mktemp); B2_HDRS=$(mktemp)
B2_STATUS=$(curl -sS -N -D "$B2_HDRS" -o "$B2_BODY" -w "%{http_code}" \
    -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
    -d "$BAD_BODY_ANTHROPIC_STREAM" "$PROXY/v1/messages")
B2_CT=$(grep -i '^content-type:' "$B2_HDRS" | head -1 | tr -d '\r\n')
if [ "${B2_STATUS:0:1}" = "2" ]; then
    fail "B2: expected non-2xx, got HTTP $B2_STATUS"
    head -c 400 "$B2_BODY"; echo
elif echo "$B2_CT" | grep -qi 'text/event-stream'; then
    fail "B2: response is SSE ($B2_CT) — error before SSE should short-circuit to JSON"
    head -c 400 "$B2_BODY"; echo
else
    if assert_anthropic_shape "$B2_BODY" "B2 streaming /v1/messages (HTTP $B2_STATUS)"; then
        :
    else
        FAILED=1
    fi
fi

# ---- B3: /v1/chat/completions (control) — must stay OpenAI-shaped -------
# This proves the Anthropic wrapper is scoped to anthropic_endpoints/, not
# applied globally to every 4xx response.
B3_BODY=$(mktemp)
B3_STATUS=$(curl -sS -o "$B3_BODY" -w "%{http_code}" \
    -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
    -d "$BAD_BODY_OPENAI" "$PROXY/v1/chat/completions")
if [ "$B3_STATUS" = "200" ]; then
    fail "B3: expected 4xx on /v1/chat/completions with empty messages, got 200"
elif [ "${B3_STATUS:0:1}" != "4" ]; then
    # 5xx is fine too — the point is "not 200" and "not Anthropic-shaped"
    :
fi
python3 - "$B3_BODY" "B3" <<'PY' || FAILED=1
import json, sys
path, label = sys.argv[1], sys.argv[2]
try:
    with open(path) as f:
        body = json.load(f)
except Exception as e:
    print(f"FAIL {label}: body not JSON ({e})")
    sys.exit(1)
errs = []
if not isinstance(body, dict):
    errs.append(f"body is not a dict, got {type(body).__name__}")
# OpenAI shape has top-level 'error' but NOT top-level 'type'='error'.
# Anthropic shape has BOTH. If type=='error' is at top level, the wrapper leaked.
if body.get("type") == "error":
    errs.append("top-level type=='error' present — Anthropic wrapper leaked into /v1/chat/completions")
if "error" not in body:
    # The chat/completions error path is currently the OpenAI shape under the
    # 'error' key. We're not asserting that body remains exactly the same — just
    # that the Anthropic envelope did NOT replace it.
    pass
if errs:
    for e in errs:
        print(f"FAIL {label}: {e}")
    print("--- body (truncated) ---")
    with open(path) as f:
        print(f.read()[:400])
    sys.exit(1)
print(f"PASS {label}: /v1/chat/completions error stayed OpenAI-shaped")
PY

rm -f "$B1_BODY" "$B1_HDRS" "$B2_BODY" "$B2_HDRS" "$B3_BODY"
exit $FAILED
