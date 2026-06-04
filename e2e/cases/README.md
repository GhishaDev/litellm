# E2E Case Library

These runbooks describe one real-provider test scenario each. They are
designed to be **executed by Claude Code**: read the file, follow the
Steps, judge the Expected outcomes, and report.

Humans can execute them too — every step is a concrete shell command.

## Index

`Tier` column: `mock` = runnable under `e2e/tools/run-all-cases --mock-only`
(zero provider cost), `real` = requires a real provider that the mock
can't faithfully simulate, `both` = either path produces the same
verdict. See "Mock-only mode" section below.

| # | File | Tier | Provider | Metric verified | Needs DB |
|---|------|------|----------|-----------------|---|
| 01 | `01_prometheus_anthropic_creation_5m.md` | real | Anthropic | `litellm_prompt_cache_creation_tokens_metric{cache_ttl="5m"}` | — |
| 02 | `02_prometheus_anthropic_creation_1h.md` | real | Anthropic | `litellm_prompt_cache_creation_tokens_metric{cache_ttl="1h"}` | — |
| 03 | `03_prometheus_anthropic_read.md` | real | Anthropic | `litellm_prompt_cache_read_tokens_metric` | — |
| 04 | `04_prometheus_no_cache_baseline.md` | both | Anthropic | cache metrics unchanged when no `cache_control` | — |
| 05 | `05_prometheus_openai_read.md` | real | OpenAI | `litellm_prompt_cache_read_tokens_metric` (provider auto-cache) | — |
| 06 | `06_prometheus_openai_no_creation.md` | both | OpenAI | `creation_tokens_metric` never emits for OpenAI | — |
| 07 | `07_prometheus_endpoint_smoke.md` | mock | (any) | `/metrics` endpoint serves valid Prometheus text format | — |
| 08 | `08_prometheus_virtual_key_labels.md` | real | Anthropic | per-virtual-key `hashed_api_key` + `api_key_alias` labels | ✓ |
| 09 | `09_prometheus_per_team_isolation.md` | real | Anthropic | per-team `team` / `team_alias` label split | ✓ |
| 10 | `10_cost_breakdown_cache_missing.md` | mock | (none — direct calc) | `cost_breakdown.cache_read_cost` / `cache_creation_cost` not silently `None` | — |
| 11 | `11_error_information_message_populated.md` | mock | (none — invalid key) | `spend_logs.metadata.error_information.error_message` non-empty on failure | ✓ |
| 12 | `12_custom_pricing_must_honor_cache_tokens.md` | mock | (none — direct calc) | `custom_cost_per_token` short-circuit must include cache pricing (Bug #2 root cause) | — |
| 14 | `14_anthropic_response_usage_shape.md` | mock | Anthropic | non-streaming `/v1/messages` `usage` matches Anthropic spec (no OpenAI-flavored `total_tokens`); streaming + `/v1/chat/completions` shapes unchanged | — |
| 16 | `16_budget_reset_no_prisma_error.md` | mock | (none — proxy only) | `ResetBudgetJob.reset_budget_windows` background tick must not raise `prisma.errors.MissingRequiredValueError` on `Json?` null-filter. Regression for BerriAI/litellm#26346 | ✓ |
| 18 | `18_public_req_middleware.md` | mock | Anthropic | `litellm_extras.PublicReqMiddleware` keeps streaming responses incremental, strips `x-litellm-*` under `X-Public-Req: 1`, and rejects sensitive `/v1/models` query params | — |
| 21 | `21_anthropic_error_shape.md` | mock | (none — malformed body) | `/v1/messages` 4xx responses use Anthropic envelope (`{type:"error",error:{type,message}}`), no `{"detail":...}` wrapper, no OpenAI-only `param`/`code`; streaming-pre-SSE error path same shape; `/v1/chat/completions` stays OpenAI-shaped (scope guard) | — |
| 22 | `22_gemini_credential_custom_api_base.md` | mock | Gemini | `gemini/` provider with custom `api_base` (UI PR #24) — exercises `/v1beta/models/<m>:generateContent` against a non-Google host with `x-goog-api-key` | — |
| 23 | `23_mock_memory_pressure.md` | mock | mock (no real provider) | Memory amplification under streaming + large bodies + retries + slow callbacks. Reproduces the prod 12 GB OOM math (peak Δ +900 MB for 5×40MB concurrent; +1.5 GB with `num_retries:2` + 30% 503). Provider-cost-free | ✓ |

## How to invoke

### Run everything

```bash
e2e/tools/run-all-cases               # one PASS/FAIL/SKIP line per case
e2e/tools/run-all-cases --skip-paid   # only free cases (10, 12)
e2e/tools/run-all-cases --mock-only   # only cases tagged Tier=mock|both
                                       # (zero provider cost, CI-friendly)
```

Exit code is 0 iff every case PASSes (SKIPs allowed). Cost ~$0.05 for
the full suite, $0 for `--mock-only`.

### Mock-only mode

`--mock-only` runs every case in the table above whose `Tier` column is
`mock` or `both` — currently 15 of 23 — against the in-network mock
provider (no real API calls, no money spent).

**You must set up the env yourself first** (the runner refuses to start
with a clear error otherwise). Minimal `.env` overlay:

```bash
# e2e/.env — needed for --mock-only
ANTHROPIC_API_KEY=sk-mock-not-used
OPENAI_API_KEY=sk-mock-not-used
GEMINI_API_KEY=sk-mock-not-used
ANTHROPIC_API_BASE=http://mock:8080
OPENAI_API_BASE=http://mock:8080/v1
GEMINI_API_BASE=http://mock:8080/v1beta
# tuned so case 13's streaming_phase > 1000ms assertion passes:
MOCK_TTFT_MS=500
MOCK_TPS=50
MOCK_CHUNKS=200
```

Then:

```bash
e2e/tools/proxy stop
e2e/tools/proxy start --with-mock
e2e/tools/run-all-cases --mock-only
```

The runner preflights:

1. proxy is ready at `$PROXY_URL`,
2. `litellm-e2e-mock` sidecar is up,
3. `ANTHROPIC_API_BASE` inside the proxy container points at
   `http://mock:8080` (otherwise it'd hit a real Anthropic endpoint).

If any check fails it prints the exact `e2e/.env` + restart command and
exits 2. With all three green it skips Tier=real cases (1, 2, 3, 5, 8,
9, 19 — they need real provider semantics: prompt-cache tokens,
thinking-signature blocks, label-coverage paths) and runs the
remaining 14 against the mock in ~30 s. Provider cost: $0.

See `e2e/_config/mock_provider.py` for the mock contract (TTFT/TPS env
vars, `X-Mock-*` headers, `X-Mock-Tool-Call`, `X-Mock-Fail`).

### Drive a single case via Claude

Tell Claude:

> "Run case 01 and report"
> "Run all anthropic cases"
> "Run case 03 but with the haiku model"

Claude will read the file, execute the steps, surface the diffs, and
judge against Expected.

### Adding a new case to the runner

1. Drop fixture under `e2e/cases/data/NN_*.sh` or `NN_*.py`
2. Fixture must exit `0` on PASS, `77` on SKIP, anything else on FAIL
3. Add a `case_NN()` function in `e2e/tools/run-all-cases` plus the
   invocation at the bottom of the file
4. Update this index table above

## Common preconditions

- `e2e/.env` exists with the relevant API keys
- `e2e/tools/proxy status` exits 0 (proxy is running on port 4011)
- The Python env running `e2e/tools/metrics` has `prometheus_client`
  installed (litellm's own venv satisfies this)
