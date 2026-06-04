#!/usr/bin/env bash
# Case 23 — memory pressure regression: streaming amplifier + retry policy.
#
# CI-friendly fixture (~10 s, no real provider). Sends N concurrent
# streaming requests through the mock and asserts the two regressions
# the production OOM investigation actually identified as load-bearing:
#
#   1. Each client request produced exactly ONE upstream provider call
#      (no retry amplification at default num_retries; verifies via
#      /__mock__/state). If a future change re-introduces auto-retry on
#      success — or a callback path is double-firing — this catches it.
#      This is the most important assertion: the production OOM math
#      was dominated by retries multiplying in-flight pipeline copies.
#
#   2. Post-burst residual RSS is bounded. After all requests complete +
#      callback queue drains, RSS should be within RESIDUAL_TOLERANCE_KB
#      of pre-burst baseline. Anything beyond = real Python-level
#      retention regression (streaming chunk list not cleared, callback
#      kwargs not released, etc.).
#
# We DON'T assert a specific peak RSS magnitude here — it's
# timing-dependent (peak hits during the parse+transform window which
# is shorter than our sampling resolution allows on slow CI hosts).
# The peak is REPORTED in the PASS line for the runbook, not asserted.
#
# The full production OOM math (5 × 40 MB bodies → +900 MB peak, 38 MB
# body × 56 concurrent → 12 GB) lives in the runbook (`23_...md`); this
# fixture is the LIGHT version that runs in CI without needing 1.5 GB
# of host RAM.

set -eu

PROXY_URL="${PROXY_URL:-http://localhost:4011}"
MASTER_KEY="${MASTER_KEY:-sk-e2e-test}"
MOCK_CONTAINER="${MOCK_CONTAINER:-litellm-e2e-mock}"
PROXY_CONTAINER="${PROXY_CONTAINER:-litellm-e2e}"

N_CONCURRENT=5
BODY_KB=200                  # ~200 KB per request body (light enough for CI)
RESIDUAL_TOLERANCE_KB=80000  # 80 MB residual is OK; > that = real leak

# 1. Mock + proxy must both be reachable. The runner pre-flights these
#    when invoked via --mock-only; standalone invocation should still fail
#    fast with a clear hint.
if ! docker exec "$MOCK_CONTAINER" python3 -c \
        "import urllib.request; urllib.request.urlopen('http://localhost:8080/healthz')" 2>/dev/null; then
    echo "FAIL: $MOCK_CONTAINER is not up. Start proxy with --with-mock."
    exit 1
fi

# 2. Build a request payload of ~$BODY_KB. text-only so it survives the
#    OpenAI-compat validator.
PAYLOAD=$(mktemp)
trap 'rm -f "$PAYLOAD"' EXIT
python3 - "$PAYLOAD" "$BODY_KB" <<'PY'
import json, sys
out_path, kb = sys.argv[1], int(sys.argv[2])
para = "Lorem ipsum dolor sit amet, consectetur adipiscing elit. "
filler = (para * (kb * 1024 // len(para) + 1))[: kb * 1024]
body = {
    "model": "mock-anthropic",
    "stream": True,
    "max_tokens": 200,
    "messages": [{"role": "user", "content": filler}],
}
with open(out_path, "w") as f:
    json.dump(body, f)
PY

# 3a. Drain any in-flight upstream calls leaked from prior cases (e.g. case 20
#     A4 returns [DONE] to the client well before the mock-side handler
#     finishes writing chunks). Without this guard, the leaked stream finishes
#     during our burst window and bumps the counter, producing a false
#     6-of-5 fail. Bounded wait — ~5s is more than enough for any sane
#     in-flight stream.
for _ in $(seq 1 50); do
    in_flight=$(docker exec "$MOCK_CONTAINER" python3 -c \
        "import urllib.request,json; print(json.load(urllib.request.urlopen('http://localhost:8080/__mock__/state'))['in_flight'])" 2>/dev/null)
    [ "$in_flight" = "0" ] && break
    sleep 0.1
done

# 3b. Reset mock counters so the assertion is unambiguous.
docker exec "$MOCK_CONTAINER" python3 -c \
    "import urllib.request; urllib.request.urlopen('http://localhost:8080/__mock__/reset')" >/dev/null

# 4. Snapshot baseline RSS (proxy PID 1 == litellm worker).
baseline_kb=$(docker exec "$PROXY_CONTAINER" awk '/VmRSS:/{print $2}' /proc/1/status)

# 5. Fire N concurrent streaming requests.
mkdir -p /tmp/case23_resp
rm -f /tmp/case23_resp/*
for i in $(seq 1 $N_CONCURRENT); do
    curl -sN --max-time 30 -X POST "$PROXY_URL/v1/chat/completions" \
        -H "Authorization: Bearer $MASTER_KEY" \
        -H "Content-Type: application/json" \
        -H "X-Mock-Chunks: 80" \
        -H "X-Mock-TTFT-Ms: 200" \
        -H "X-Mock-Chunk-Chars: 30" \
        --data-binary @"$PAYLOAD" \
        -o "/tmp/case23_resp/r_${i}.txt" &
done

# 6. Sample RSS midway through the burst (around t+1s; TTFT 200ms +
#    streaming ~2.5 s = window of 3 s).
sleep 1
peak_kb=$(docker exec "$PROXY_CONTAINER" awk '/VmRSS:/{print $2}' /proc/1/status)

# 7. Wait for all curls.
wait
sleep 1  # give the streaming wrapper a beat to finalize callback queueing.

# 8. Verify every request actually streamed.
ok=0
for f in /tmp/case23_resp/r_*.txt; do
    grep -q '"finish_reason"' "$f" && ok=$((ok + 1))
done
if [ "$ok" -ne "$N_CONCURRENT" ]; then
    echo "FAIL [3]: only $ok / $N_CONCURRENT responses completed"
    exit 1
fi

# 9. Pull mock counters. Expected: exactly N anthropic stream 200s.
state=$(docker exec "$MOCK_CONTAINER" python3 -c \
    "import urllib.request, json; print(json.dumps(json.load(urllib.request.urlopen('http://localhost:8080/__mock__/state'))))")
upstream_calls=$(echo "$state" \
    | python3 -c "import json, sys; d = json.load(sys.stdin); print(d['requests'].get('anthropic:stream:200', 0))")

if [ "$upstream_calls" -ne "$N_CONCURRENT" ]; then
    echo "FAIL [1]: client sent $N_CONCURRENT but mock saw $upstream_calls upstream calls."
    echo "  Likely cause: a retry policy was re-introduced (router num_retries),"
    echo "  or a callback path is double-firing."
    echo "  mock /__mock__/state: $state"
    exit 1
fi

# 10. Wait for callback queue to drain + assert residual is bounded.
# 2 s covers GenericAPILogger's default 5 s periodic_flush start — combined
# with the 1 s post-curl sleep above we're roughly at flush boundary.
sleep 2
end_kb=$(docker exec "$PROXY_CONTAINER" awk '/VmRSS:/{print $2}' /proc/1/status)
residual_kb=$((end_kb - baseline_kb))
delta_peak_kb=$((peak_kb - baseline_kb))
if [ "$residual_kb" -gt "$RESIDUAL_TOLERANCE_KB" ]; then
    echo "FAIL [2]: residual RSS = ${residual_kb} kB after burst end, > ${RESIDUAL_TOLERANCE_KB} kB tolerance."
    echo "  This points at a real Python-level retention regression (callback queue,"
    echo "  streaming chunk list, Pydantic copies). Inspect with gdb + tracemalloc."
    exit 1
fi

cat <<EOF
PASS: ${N_CONCURRENT} concurrent ${BODY_KB} KB streaming bodies
  baseline RSS     = ${baseline_kb} kB
  peak RSS (t+1s)  = ${peak_kb} kB  (mid-burst delta +${delta_peak_kb} kB; informational)
  end RSS (post)   = ${end_kb} kB   (residual +${residual_kb} kB ≤ ${RESIDUAL_TOLERANCE_KB})
  upstream calls   = ${upstream_calls} / ${N_CONCURRENT} (exactly one per client request, no retry amplification)
EOF
exit 0
