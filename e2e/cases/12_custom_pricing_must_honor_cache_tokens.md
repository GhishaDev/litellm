# Case 12 — Router must backfill cost fields from canonical entry for known models

## Goal

Guard the behavior of `Router._backfill_cost_fields_from_canonical`:
when a deployment is registered via the dashboard `/model/new` form
(or DB sync) with only `input_cost_per_token` and
`output_cost_per_token` set, the **deployment-UUID entry** in
`litellm.model_cost` must be augmented with the missing
`CustomPricingLiteLLMParams` fields (`cache_read_input_token_cost`,
`cache_creation_input_token_cost`, etc.) pulled from the canonical
static entry for the bare model name.

Without backfill, the cost calculator's custom-pricing path
(`_select_model_name_for_cost_calc` at `cost_calculator.py:661-672`)
prefers the deployment-UUID entry and silently drops cache pricing,
under-billing cache-heavy requests by ~93%.

### Why this is needed (the original symptom)

`/model/new` exposes only two pricing fields. The user-reported
`cleanedLitellmParams` dump confirmed it: input/output rates set,
cache rates absent. Operators experience the gap as:

> "Cache pricing breakdown looks correct after I click Reload Price
> Data, but only on some requests. Reload doesn't stick."

What Reload was actually doing: replacing
`litellm.model_cost` wholesale, which incidentally **evicted the
deployment-UUID entries** the router had registered. The next
request fell through to the bare model name, hit the canonical
entry, and billed correctly — for that worker, until the next DB
sync re-registered the deployment with the same partial dict, or
until the request load-balanced to a worker that never received
the reload broadcast.

Backfill closes the gap deterministically: every worker, at
registration time, fills the missing fields from the canonical
entry. No more reload-as-fix and no more per-worker drift.

### Numerical impact (user's prod Usage shape)

For `claude-haiku-4-5-20251001`, `prompt=100191`, `cache_read=99774`,
`cache_creation=416`:

| State | Total | Notes |
|---|---|---|
| Pre-fix (UUID entry missing cache rates) | **$0.000756** | cache portion silently dropped |
| Post-fix (canonical fields backfilled) | **$0.011253** | math correct |

Restored revenue: **+$0.010497 per request** (93% of the bill).

## Preconditions

- `e2e/tools/proxy status` reports `ready`
- `LITELLM_LOCAL_MODEL_COST_MAP=True` (already set in docker-compose)
- No DB, no provider, no real API key — pure in-process Router test.

## Steps

```bash
docker cp e2e/cases/data/12_custom_pricing_must_honor_cache_tokens.py \
    litellm-e2e:/tmp/c12.py
docker exec litellm-e2e python3 /tmp/c12.py
echo "exit=$?"
```

The fixture:

1. Reads the canonical static entry for `claude-haiku-4-5-20251001`
   as the expected baseline.
2. Builds a `Router(model_list=[...])` with a single deployment whose
   `litellm_params` carries only `input_cost_per_token` /
   `output_cost_per_token` (exactly what `/model/new` produces).
3. Asserts `litellm.model_cost[<deployment_uuid>]` has
   `cache_read_input_token_cost` and
   `cache_creation_input_token_cost` populated after Router
   registration.
4. Runs `response_cost_calculator(custom_pricing=True,
   router_model_id=<uuid>)` for the user-reported Usage shape and
   asserts the total equals the static-map baseline within $0.0001.

## Expected — GREEN

```
After Router(model_list=...) registration:
  UUID entry exists:                 True
  input_cost_per_token               = 1e-06
  output_cost_per_token              = 5e-06
  cache_read_input_token_cost        = 1e-07
  cache_creation_input_token_cost    = 1.25e-06

expected total (correct cache billing)  = $0.011253
actual total via UUID path              = $0.011253399999999998

PASS: UUID-path total agrees with static-map total within $0.0001
```

## Where the fix lives

- `litellm/router.py` — `_backfill_cost_fields_from_canonical`
  staticmethod, invoked from both `_create_deployment` (init-time
  path used by `set_model_list`) and `add_deployment` (runtime path
  used by `/model/new` + DB sync).
- Unit tests pinning the three scopes (known model backfill, user
  override wins, unknown model leaves fields absent):
  `tests/test_litellm/test_router_backfill_cost_fields.py`.

## Failure modes

| Symptom | Cause |
|---|---|
| `FAIL: cache_read_input_token_cost is None` after Router init | The backfill helper isn't being called from one of the register sites — check `_create_deployment` and `add_deployment` |
| `FAIL: cost calc disagrees by ~-93%` | The UUID entry was registered with partial data; backfill ran but didn't reach this field — verify `CustomPricingLiteLLMParams.model_fields.keys()` covers cache_* |
| `FAIL: static entry incomplete in the bundled JSON` | The canonical baseline for `claude-haiku-4-5-20251001` regressed — see case 10 |
| Cost off by tiny amounts (< $1e-6) | Floating-point rounding, not a regression — tolerance is $1e-4 |

## Cross-reference

- **Case 10** — guards the static `model_cost` entry has full pricing
  (the canonical source the backfill copies *from*).
- **Case 11** — guards observability for failure logging
  (`error_information.error_message`).
- **This case (12)** — guards the deployment-UUID path produced by
  Router registration.

All three GREEN means the cost-breakdown pipeline is trustworthy
end-to-end, with no operator intervention required.

## Design rationale (why backfill, not "fix the short-circuit")

The cost calculator has a `custom_pricing` short-circuit at
`cost_calculator.py:326-335` and a UUID-preference branch at
`661-672`. Both are intentional: when a deployment supplies custom
pricing, that pricing wins.

The actual gap is upstream of cost calc: the **registered UUID
entry is incomplete** for known models, because the dashboard form
doesn't expose every CustomPricingLiteLLMParams field. Two valid
fixes were considered:

1. **Cost calc fallback** — if UUID entry lacks a cache rate, fall
   back to the bare model name's static entry. Rejected because it
   adds a runtime lookup on every request and conflates "user
   omitted" with "user wants zero".

2. **Router-side backfill at registration** *(chosen)* — fill missing
   fields once at register-time, so every worker's UUID entry is
   complete and the cost calc reads a single source of truth. Aligns
   with what `model_info` merge already does for the dashboard's
   display path; eliminates the inconsistency.

Backfill **only fills slots the user left blank**; any value
explicitly set in `litellm_params` still wins. Unknown / custom
models with no static entry pass through unchanged.
