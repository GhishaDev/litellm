# Case 23 — Mock-driven memory pressure: streaming, retries, big bodies

## Goal

End-to-end harness for **memory / retry / timeout / callback** scenarios
without spending a cent on real providers. Uses the in-network mock
provider (`e2e/_config/mock_provider.py`, compose profile `mock`) which
serves OpenAI- and Anthropic-shape responses with controllable TTFT, TPS,
chunk count, failure injection, and slow callback drain.

This case is the canonical reproducer for the production analysis we did
on the 12 GB pod OOM that triggered at 50 RPM / 6 minutes / 17 large
requests per pod. The math + diagnosis are persisted as the "Expected"
outputs below — re-running this case in CI verifies the same memory
amplification curves still hold against the current ship branch.

## Background

Production hit OOM under a combination of:

- **TTFT 15s + TPS 30** on the upstream → each streaming request stays
  in-flight ~65 s, so 50 RPM means ~56 concurrent in-flight at steady
  state (Little's Law).
- **Body sizes up to ~40 MB** (base64 images + pasted documents + long
  conversation history + tool_results), each amplified 4-5× by litellm's
  transform pipeline (json.loads → Pydantic → provider format → httpx
  serialize).
- **Retries on 5xx burst**: `num_retries: 2` default × 30% upstream 503
  rate → average 3.2 provider calls per client request → in-flight
  memory effectively tripled.
- **Custom HTTP callback** (`callback_settings + generic_api`) loading
  `StandardLoggingPayload` with full `messages` and `response` —
  unbounded `log_queue` if the callback consumer is slow.

The mock reproduces every one of these knobs.

## Preconditions

- `e2e/tools/proxy` available (mock has no provider key requirement).
- Postgres ephemeral DB up (handled by `proxy start`).
- 1.5 GB free RAM on the host (mock pushes RSS to ~2 GB at peak).
- `apk add gdb` inside the container if you want to inspect the
  GenericAPILogger queue or take memory snapshots via gdb-injected
  PyRun_SimpleString. The case below does **not** require gdb — RSS
  delta from `/proc/1/status` is enough for pass/fail.

## Steps

### 1. Start the proxy with the mock profile

```bash
e2e/tools/proxy start --with-mock
```

This auto-registers two model_list entries in
`e2e/_config/.litellm.rendered.yaml`:

- `mock-openai`     → `openai/mock-model`        → `http://mock:8080/v1`
- `mock-anthropic`  → `anthropic/mock-claude`    → `http://mock:8080`

Verify they show up:

```bash
curl -sS -H "Authorization: Bearer sk-e2e-test" \
  http://localhost:4011/v1/models | python3 -m json.tool | grep -E '"id"'
```

Should list both `mock-openai` and `mock-anthropic` alongside the normal
real-provider entries.

### 2. Baseline single request — verify the mock path works

```bash
curl -sN --max-time 30 -X POST \
  http://localhost:4011/v1/chat/completions \
  -H "Authorization: Bearer sk-e2e-test" \
  -H "Content-Type: application/json" \
  -d '{"model":"mock-anthropic","stream":true,"messages":[{"role":"user","content":"hi"}],"mock_chunks":3,"mock_chunk_chars":10}'
```

Should stream 3 chunks back. (`mock_chunks=3` overrides the env
default per-request. The endpoint accepts both OpenAI- and
Anthropic-shape `messages`; litellm transforms accordingly.)

### 3. Memory pressure: 5 × 40 MB concurrent bodies

Build a 40 MB request body (text-only payload that survives the
OpenAI-shape validator — the memory characteristics are identical to a
multimodal body of the same size):

```bash
python3 - <<'PY'
import json
def fake(n_chars):
    para = "Lorem ipsum dolor sit amet, consectetur adipiscing elit. "
    return (para * (n_chars // len(para) + 1))[:n_chars]
history = []
for _ in range(15):
    history.append({"role": "user", "content": fake(300_000)})
    history.append({"role": "assistant", "content": fake(200_000)})
text_blocks = [fake(533_000) for _ in range(40)]
current = ("DOC:\n" + fake(8_000_000) + "\n\nATTACHMENTS:\n"
           + "\n---\n".join(text_blocks)
           + "\n\nTOOL_RESULT: " + fake(5_000_000))
payload = {
    "model": "mock-anthropic", "stream": True, "max_tokens": 2000,
    "messages": history + [{"role": "user", "content": current}],
    "mock_chunks": 50, "mock_chunk_chars": 20, "mock_ttft_ms": 5000, "mock_tps": 30,
}
open("/tmp/big40.json","wb").write(json.dumps(payload).encode())
PY
ls -lh /tmp/big40.json   # ≈ 40 MB
```

Capture baseline RSS:

```bash
BASE_KB=$(docker exec litellm-e2e bash -c 'grep VmRSS /proc/1/status' | awk '{print $2}')
echo "baseline: $((BASE_KB/1024)) MB"
```

Fire 5 concurrent and capture peak via a background sampler:

```bash
rm -f /tmp/trace.log /tmp/done
( while [ ! -f /tmp/done ]; do
    rss=$(docker exec litellm-e2e bash -c "grep VmRSS /proc/1/status" 2>/dev/null | awk '{print $2}')
    [ -n "$rss" ] && echo "$(date +%s.%3N) $rss" >> /tmp/trace.log
    sleep 0.3
  done ) &

for i in 1 2 3 4 5; do
  curl -sN --max-time 60 -X POST \
    http://localhost:4011/v1/chat/completions \
    -H "Authorization: Bearer sk-e2e-test" -H "Content-Type: application/json" \
    --data-binary @/tmp/big40.json -o /tmp/resp_$i.txt &
done
wait
sleep 5
touch /tmp/done
sleep 1
```

Compute peak Δ vs baseline:

```bash
python3 - <<PY
v = [int(l.split()[1]) for l in open("/tmp/trace.log") if l.strip()]
base, peak = v[0], max(v)
print(f"baseline {base/1024:.0f} MB  peak {peak/1024:.0f} MB  delta +{(peak-base)/1024:.0f} MB")
PY
```

### 4. (Optional) Retry amplification

To see how `num_retries` × upstream 5xx interact, restart the mock with
`MOCK_FAIL_RATE=0.3` and confirm the response stream still completes
after retries (the mock returns 503 with an Anthropic-style error body
that litellm classifies as retryable):

```bash
e2e/tools/proxy stop
MOCK_FAIL_RATE=0.3 e2e/tools/proxy start --with-mock
# Fire same 5 concurrent — observe peak now climbs higher
```

Mock will receive ~3 POSTs per client request when retries are on
(`docker logs litellm-e2e-mock 2>&1 | grep -c 'POST /v1/messages'`).

### 5. (Optional) Slow callback / Langfuse queue retention

Demonstrates the GenericAPILogger / Langfuse async sink filling up
under a slow consumer:

```bash
e2e/tools/proxy stop
MOCK_CALLBACK_DELAY=8 e2e/tools/proxy start --with-mock
# Configure success_callback / langfuse to point at http://mock:8080
# and observe queue retention as documented in mock_provider.py header.
```

## Expected

After step 3 (5 × 40 MB concurrent, default ship config):

| Metric                          | Approx value | Verdict |
|---------------------------------|--------------|---------|
| baseline RSS (post gc+trim)     | 600 ± 100 MB | informational |
| peak Δ during burst             | **+900 ± 200 MB** | matches production OOM math |
| end Δ before gc                 | +300 ± 100 MB | streaming chunks + callback refs |
| end Δ after `gc.collect() + malloc_trim(0)` | +200 ± 100 MB | pymalloc arena fragmentation |

Per-request amplification = peak Δ / total body bytes ≈ **4-5×**.

After step 4 (retries on, 30% 503):

- mock POST count ≈ 3 × client request count
- peak Δ ≈ **+1500 ± 300 MB** (~65 % higher than no-retry baseline)
- end Δ after gc+trim ≈ **+800 ± 200 MB** (≈ 3 × no-retry retention)

If these numbers drift > 30 % in either direction, **something changed**
in the streaming / transform / retry / callback path — investigate
before merging.

## Why this case lives in e2e (not unit / load tests)

- Full stack: uvicorn body parse → FastAPI dispatch → litellm router →
  provider transform → httpx send → streaming chunk merge → callback
  drain. Unit mocks can't catch the cumulative memory cost.
- Real HTTP, real cgroups, real glibc — `MALLOC_ARENA_MAX` /
  `LD_PRELOAD=libjemalloc` A/B requires actual containers.
- Provider-cost-free: mock makes this safe to run in CI.

## See also

- `e2e/_config/mock_provider.py` — full endpoint + env-var contract
- `e2e/_config/docker-compose.yml` — mock service (profile `mock`)
- `e2e/tools/proxy` — `--with-mock` flag, `render_config(with_mock=True)`
