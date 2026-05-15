# Case 10 — Cost breakdown must include cache fields for cached prompts

## Goal

Regression guard for a real production bug: dashboard cost breakdown for
`claude-haiku-4-5-20251001` showed only `input_cost` + `output_cost`,
silently absorbing the entire cache portion of the bill. Hitting the
admin **"Reload Price Data"** endpoint repaired the breakdown without a
proxy restart — proving the bug lives in `litellm.model_cost` runtime
state, not the calc logic itself.

User-observed numbers (single Haiku 4.5 call,
`prompt_tokens=100191`, `completion_tokens=151`,
`cache_read=99774`, `cache_creation=416`):

| Field | Before reload | After reload |
|---|---|---|
| `total_cost` | $0.002689 | $0.011253 |
| `cache_read_cost` in breakdown | (missing) | $0.009977 |
| `cache_creation_cost` in breakdown | (missing) | $0.000520 |

Delta = $0.008565, exactly `99774*1e-7 + 416*1.25e-6` — the entire cache
charge.

### Root cause path

1. `litellm.model_cost["claude-haiku-4-5-20251001"]` lacked
   `cache_read_input_token_cost` / `cache_creation_input_token_cost` at
   runtime. Candidates:
   - Lagging upstream `model_prices_and_context_window.json` fetched at
     proxy startup
   - `register_model()` overwrote the entry via `_update_dictionary` from
     a dynamic source that did not carry cache fields
2. `litellm.get_model_info()` then returns those keys as `None` (or
   raises), inside the `try/except` block at
   `litellm/cost_calculator.py:1605-1632`
3. The `except Exception: pass` swallows the failure silently —
   `_cache_read_cost` and `_cache_creation_cost` stay `None`, so the
   breakdown dict stored in `spend_logs.cost_breakdown` omits them
4. Reload endpoint (`/reload_model_cost_map`) re-reads the JSON and
   `litellm.add_known_models()` re-merges the missing keys; the next
   request renders the full breakdown

This case asserts every link in that chain is healthy.

## Preconditions

- `e2e/tools/proxy status` reports `ready`
- `LITELLM_LOCAL_MODEL_COST_MAP=True` set in docker-compose (already is —
  ensures the bundled JSON is the source of truth so the test is
  deterministic regardless of upstream lag)

No `ANTHROPIC_API_KEY` needed: the case calls `litellm.completion_cost()`
directly against a fabricated `Usage` shape. It costs nothing and never
hits a provider.

## Steps

```bash
# 1. Ship the regression fixture into the running container
docker cp e2e/cases/data/10_cost_breakdown_cache_missing.py \
    litellm-e2e:/tmp/case10.py

# 2. Run the assertions
docker exec litellm-e2e python3 /tmp/case10.py
echo "exit=$?"
```

### Optional — negative control (proves the test detects the bug)

Strip the cache rate keys at runtime and confirm the script exits 1:

```bash
docker exec litellm-e2e python3 -c "
import os; os.environ['LITELLM_LOCAL_MODEL_COST_MAP'] = 'True'
import litellm
mc = litellm.model_cost['claude-haiku-4-5-20251001']
mc.pop('cache_read_input_token_cost', None)
mc.pop('cache_creation_input_token_cost', None)
from litellm.utils import get_model_info
try: get_model_info.cache_clear()
except Exception: pass
exec(open('/tmp/case10.py').read())
"; echo "exit=$?"
```

Note: the negative control mutates global state inside the container, so
it should be run **last** in a session, or followed by
`e2e/tools/proxy restart` to reset `litellm.model_cost`.

## Expected

### Happy path

- exit=0
- Output ends with:
  ```
  PASS: cache portion = 0.010497 (93.3% of total)
  PASS: all paths agree on total = 0.011253
  ```
- Three independent code paths all agree on the same total:
  manual math, `litellm.completion_cost()`, and the proxy's
  `response_cost_calculator()`

### Negative control

- exit=1
- First line: `FAIL: model_cost[...].cache_read_input_token_cost is None`
- Confirms the assertion fires the moment the cache keys disappear

## Failure modes

| Symptom | Likely cause |
|---|---|
| `FAIL: ...cache_read_input_token_cost is None` on the happy path | The bundled `model_prices_and_context_window.json` regressed — check `litellm/model_prices_and_context_window_backup.json` for the Haiku 4.5 entry |
| `FAIL: completion_cost mismatch` | Calc rounding or branch change in `cost_calculator.py`; rerun manual math from the printed rates |
| `FAIL: response_cost_calculator mismatch` but `completion_cost` ok | Proxy-side wrapping path (e.g. `_calc_with_usage_object`) diverged from the library path; bisect there |
| Script `ModuleNotFoundError: litellm` | Container is not the e2e proxy image — `e2e/tools/proxy rebuild` |
| `FAIL: test geometry weakened` | Someone changed `CACHE_READ`/`CACHE_CREATE` such that cache portion no longer dominates — restore the original Usage shape so the test stays a meaningful regression |

## When this case fires

Treat a happy-path failure as **release-blocking**. The user-visible
symptom is silent under-billing — there is no log line, no metric, no
alert. The only feedback signal in production is "the dashboard total
looks too small," which depends on someone noticing. This regression
case is the only automated tripwire we have for that failure mode until
the silent `except Exception: pass` at `cost_calculator.py:1605-1632` is
replaced with an explicit warning.
