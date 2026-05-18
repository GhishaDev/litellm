# E2E Case Library

These runbooks describe one real-provider test scenario each. They are
designed to be **executed by Claude Code**: read the file, follow the
Steps, judge the Expected outcomes, and report.

Humans can execute them too — every step is a concrete shell command.

## Index

| # | File | Provider | Metric verified | Needs DB |
|---|------|----------|-----------------|---|
| 01 | `01_prometheus_anthropic_creation_5m.md` | Anthropic | `litellm_prompt_cache_creation_tokens_metric{cache_ttl="5m"}` | — |
| 02 | `02_prometheus_anthropic_creation_1h.md` | Anthropic | `litellm_prompt_cache_creation_tokens_metric{cache_ttl="1h"}` | — |
| 03 | `03_prometheus_anthropic_read.md` | Anthropic | `litellm_prompt_cache_read_tokens_metric` | — |
| 04 | `04_prometheus_no_cache_baseline.md` | Anthropic | cache metrics unchanged when no `cache_control` | — |
| 05 | `05_prometheus_openai_read.md` | OpenAI | `litellm_prompt_cache_read_tokens_metric` (provider auto-cache) | — |
| 06 | `06_prometheus_openai_no_creation.md` | OpenAI | `creation_tokens_metric` never emits for OpenAI | — |
| 07 | `07_prometheus_endpoint_smoke.md` | (any) | `/metrics` endpoint serves valid Prometheus text format | — |
| 08 | `08_prometheus_virtual_key_labels.md` | Anthropic | per-virtual-key `hashed_api_key` + `api_key_alias` labels | ✓ |
| 09 | `09_prometheus_per_team_isolation.md` | Anthropic | per-team `team` / `team_alias` label split | ✓ |
| 10 | `10_cost_breakdown_cache_missing.md` | (none — direct calc) | `cost_breakdown.cache_read_cost` / `cache_creation_cost` not silently `None` | — |
| 11 | `11_error_information_message_populated.md` | (none — invalid key) | `spend_logs.metadata.error_information.error_message` non-empty on failure | ✓ |
| 12 | `12_custom_pricing_must_honor_cache_tokens.md` | (none — direct calc) | `custom_cost_per_token` short-circuit must include cache pricing (Bug #2 root cause) | — |

## How to invoke

### Run everything

```bash
e2e/tools/run-all-cases               # one PASS/FAIL/SKIP line per case
e2e/tools/run-all-cases --skip-paid   # only free cases (10, 12)
```

Exit code is 0 iff every case PASSes (SKIPs allowed). Cost ~$0.05 for
the full suite.

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
