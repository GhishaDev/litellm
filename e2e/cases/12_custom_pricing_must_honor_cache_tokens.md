# Case 12 — Deployment UUID entry in `litellm.model_cost` must not silently strip cache pricing

## Goal

Regression guard for the **actual** production bug that triggered the
"cache pricing only correct after clicking Reload Price Data" report.

### Root cause (verified end-to-end against the running proxy)

Three pieces interact:

1. **Router register-on-startup**
   (`litellm/router.py:7230-7237`)

   ```python
   _model_id = deployment.model_info.id
   if _model_id is not None:
       _model_info_dict = deployment.model_info.model_dump(exclude_none=True)
       for field in CustomPricingLiteLLMParams.model_fields.keys():
           field_value = deployment.litellm_params.get(field)
           if field_value is not None:
               _model_info_dict[field] = field_value
       litellm.register_model(model_cost={_model_id: _model_info_dict})
   ```

   For each deployment loaded from DB / config, the router writes an
   entry into `litellm.model_cost` keyed by the deployment's **UUID**.
   The dict is `model_info.model_dump(exclude_none=True)` merged with
   any custom-pricing keys in `litellm_params`. When the dashboard
   `/model/new` form was used to add the model — that form exposes
   only `input_cost_per_token` and `output_cost_per_token` — and if
   `deployment.model_info` did not have static-map cache rates merged
   in by the time `_create_deployment` ran, **the UUID entry written
   into `litellm.model_cost` permanently lacks
   `cache_read_input_token_cost` / `cache_creation_input_token_cost`**.

2. **Cost calc prefers the UUID over the bare model name**
   (`litellm/cost_calculator.py:661-672`)

   ```python
   if custom_pricing is True:
       if router_model_id is not None and router_model_id in litellm.model_cost:
           entry = litellm.model_cost[router_model_id]
           if entry.get("input_cost_per_token") is not None or ...:
               return_model = router_model_id     # ← UUID wins
           else:
               return_model = model
   ```

   Because the deployment has `input_cost_per_token` set,
   `custom_pricing` is `True`. Because the router wrote a UUID entry
   in step 1, it is found in `model_cost`. The model name handed to
   `cost_per_token` is therefore the UUID — and `cost_per_token`
   reads the partial UUID entry, finds `cache_*_input_token_cost = None`,
   and drops cache tokens from the bill.

3. **"Reload Price Data" is a coincidental band-aid**
   (`litellm/proxy/proxy_server.py:13319`)

   ```python
   litellm.model_cost = new_model_cost_map     # ← whole-dict replacement
   ```

   The reload endpoint replaces `litellm.model_cost` wholesale with
   the freshly-fetched static JSON. This **incidentally evicts every
   deployment-UUID entry** the router previously wrote. On the next
   request, `_select_model_name_for_cost_calc` finds the UUID no
   longer present, falls through to the bare model name, hits the
   complete static-map row, and bills correctly.

   Reload is **not** repopulating cache fields. It is clearing the
   stale partial entry so the lookup falls through.

### Why "Reload fixes some requests but not all"

- **Single-process effect, multi-worker fleet** — the reload endpoint
  only updates `litellm.model_cost` in the worker that handled the
  HTTP POST. Other workers (and other machines) read a
  `force_reload=True` flag in `LiteLLM_Config` and try to reload on
  their next 10-second poll. Whichever worker gets there first clears
  the flag back to `False` (`proxy_server.py:5192-5213`), so **any
  worker that polls later than that loses the broadcast and never
  reloads**.
- **Re-registration overwrites the fix** — the periodic DB-sync /
  config-reload task calls `_create_deployment` again, re-running
  step 1 above. That writes the partial UUID entry back into
  `litellm.model_cost` and the bug returns on the affected worker.
- **Load balancing splits the symptom** — chat requests are spread
  across all workers, so some calls hit a freshly-reloaded worker
  (correct bill) and some hit a stale worker (under-billed). End
  result: dashboard shows partial recovery, never full.

### Numerical impact (verified)

For the user-reported Usage (`claude-haiku-4-5-20251001`,
`prompt=100191`, `cache_read=99774`, `cache_creation=416`):

| Path | Total | Notes |
|---|---|---|
| UUID-path with partial entry (worker before reload, or after re-sync) | **$0.000756** | cache portion silently dropped |
| Bare-model-name path (worker right after reload) | **$0.011253** | correct |

Delta: **$0.010497 missing** per request, about **−93%** of the bill.
Across many cached requests this is significant revenue lost.

## Preconditions

- `e2e/tools/proxy status` reports `ready`
- `LITELLM_LOCAL_MODEL_COST_MAP=True` set in docker-compose
- No provider key needed — the case calls `response_cost_calculator`
  directly with a fabricated `Usage` and a sentinel deployment UUID
  registered via `litellm.register_model`

## Steps

```bash
docker cp e2e/cases/data/12_custom_pricing_must_honor_cache_tokens.py \
    litellm-e2e:/tmp/c12.py
docker exec litellm-e2e python3 /tmp/c12.py
echo "exit=$?"
```

The fixture:
1. Reads the static `claude-haiku-4-5-20251001` entry as the correct baseline
2. `litellm.register_model({<uuid>: {input/output rates only, no cache fields}})`
   to mimic what `router.py:7237` does for a `/model/new`-added deployment
3. Builds a `Usage` with `cache_read=99774`, `cache_creation=416`
4. Calls `response_cost_calculator(custom_pricing=True, router_model_id=<uuid>)`
   — exactly the proxy's logging-path shape
5. Asserts the result equals the static-map baseline within $0.0001

## Expected (after the fix lands)

```
expected total (correct cache billing)  = $0.011253
actual total via UUID path              = $0.011253
PASS: UUID-path total agrees with static-map total within $0.0001
exit=0
```

## Current status — RED

On `fix/prometheus-prompt-cache-tokens` and v1.83.10:

```
expected total (correct cache billing)  = $0.011253
actual total via UUID path              = $0.000756
FAIL: cost calc via UUID path disagrees with static-map total by $-0.010497 (-93.3%)
exit=1
```

## Suggested fix

`litellm/router.py:7230-7237` — when writing the UUID entry, merge
cache rate fields from the static map for the bare model name when
the deployment's litellm_params doesn't supply them:

```python
_model_id = deployment.model_info.id
if _model_id is not None:
    _model_info_dict = deployment.model_info.model_dump(exclude_none=True)

    # NEW: backfill cache rate fields from the static model_cost map
    # for the bare model name. The dashboard /model/new form does not
    # surface these, so without this step a UUID-keyed entry will
    # silently strip cache pricing.
    bare_model_name = deployment.litellm_params.get("model")
    if bare_model_name and bare_model_name in litellm.model_cost:
        static_entry = litellm.model_cost[bare_model_name]
        for cache_field in (
            "cache_read_input_token_cost",
            "cache_read_input_token_cost_above_200k_tokens",
            "cache_creation_input_token_cost",
            "cache_creation_input_token_cost_above_1hr",
            "cache_creation_input_token_cost_above_200k_tokens",
        ):
            if _model_info_dict.get(cache_field) is None:
                value = static_entry.get(cache_field)
                if value is not None:
                    _model_info_dict[cache_field] = value

    # existing override loop unchanged — litellm_params still wins
    for field in CustomPricingLiteLLMParams.model_fields.keys():
        field_value = deployment.litellm_params.get(field)
        if field_value is not None:
            _model_info_dict[field] = field_value

    litellm.register_model(model_cost={_model_id: _model_info_dict})
```

Properties of this fix:

- **Deterministic** — every worker writes the same UUID entry, regardless
  of whether reload has been triggered or how the deployment was added
- **Reload-free** — no operator action required; correctness comes from
  the registration step itself
- **Preserves user overrides** — `litellm_params` cache rates still
  win if explicitly supplied (e.g. by an enterprise customer with
  negotiated discount cache pricing)
- **Multi-worker safe** — every worker independently does the merge
  using its own local `litellm.model_cost`; no cross-worker
  coordination needed

A second, **separate** fix is also indicated for the reload
broadcast — the `force_reload` boolean flag at
`proxy_server.py:5192-5213` should be replaced by a monotonic
`last_reload_at` timestamp so all workers definitely observe the
reload. Track this as a separate task; it's out of scope for the
case 12 assertion.

## Failure modes

| Symptom | Cause |
|---|---|
| Current: `FAIL: ...disagrees by -93.3%` | Expected — the fix has not landed yet |
| `FAIL: register_model didn't write the UUID entry` | `register_model` API changed signatures; update the test setup |
| `FAIL: UUID entry has cache rates already` | Some upstream code is now auto-merging cache rates at register time — the bug may already be fixed; verify with a fresh `/model/new` round-trip and update this case to GREEN, or extend the simulated litellm_params with cache_field=None overrides to keep the test meaningful |
| `FAIL: static {MODEL} entry incomplete` | Bundled JSON regressed for the baseline model — see case 10 |

## Cross-reference

- Case 10 — guards the bare model-name path (static map cache fields
  present)
- Case 11 — guards observability (`error_information.error_message`
  not silenced)
- This case (12) — guards the deployment-UUID path (the one the user
  actually triggered)

All three must be GREEN for cache billing on dashboard-added
deployments to be trustworthy without operator intervention.
