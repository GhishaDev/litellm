# Case 34 — Time-weighted routing: per-band weight selection + delegate fallback

## Goal

End-to-end verification of `litellm_extras/time_weighted_router.py`:

1. **In-band weighted pick** — three mock deployments under the same
   `model_name`, configured with disjoint `time_weights.bands` such
   that exactly **one** is non-zero at fixture wall-clock time. After
   N requests, **100%** of traffic lands on that deployment.

2. **Delegate path untouched** — a sibling `model_name` with NO
   `time_weights` on any deployment routes via the original LiteLLM
   dispatcher (`simple-shuffle` / configured strategy). Confirms the
   `_model_uses_time_weights()` gate does not regress non-opt-in
   models.

3. **Hot reload via PATCH** — `PATCH /model/{id}/update` changes
   `model_info.time_weights.bands` so a different deployment becomes
   the hot one. Next request immediately follows the new band, without
   proxy restart. Verifies the DB-backed reload chain
   (`clear_cache` → `proxy_config.add_deployment` → reinstall).

4. **Blocked deployment is excluded** — set `blocked: true` on the
   currently-hot deployment via PATCH; traffic falls to the next band
   non-zero deployment if any, or uniform among remaining if all
   resolved weights are 0. Verifies that our strategy composes
   correctly with `_filter_blocked_deployments`.

## Tier

`mock-only` — uses `mock-anthropic` deployments labeled as three
distinct GLM-style accounts. No real provider cost.

## What the fixture does

`e2e/cases/data/34_time_weighted_routing.py` —

| Step | Action | Expected result |
|---|---|---|
| 1 | Register 3 deployments under `model_name=tw-pool`, all pointing at `mock-anthropic` with distinct `model_info.id`. Configure bands so that **only** `glm-acc-1` has non-zero weight at the current wall-clock minute. | `/v2/model/info` returns the 3 deployments. |
| 2 | Send 30 requests to `tw-pool` and capture which `model_info.id` served each (read from the response headers `x-litellm-model-id` or via SpendLogs). | All 30 land on `glm-acc-1`. |
| 3 | Register a 4th deployment under `model_name=plain-pool` (no `time_weights`). Send 30 requests. | Traffic uniform / per LiteLLM's default strategy — not all on one deployment. |
| 4 | `PATCH /model/{id}/update` `glm-acc-1` to zero its weight in the active band; `PATCH` `glm-acc-2` to take a non-zero weight in the active band. Re-issue 10 requests. | All 10 land on `glm-acc-2`. |
| 5 | `PATCH glm-acc-2 blocked=true`. Re-issue 10 requests. | Traffic falls to `glm-acc-3` (if its fallback chain assigns a weight), or uniform among the survivors. |
| 6 | Unblock `glm-acc-2`; verify traffic returns according to the bands. | Mirrors step 4 distribution. |

The fixture computes the "active band" dynamically: it reads
`datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Shanghai"))` and
constructs a band that contains the current minute. This avoids
tightly coupling the test to a clock and means the fixture is
deterministic without needing to monkey-patch `datetime.now`.

## Anti-flake notes

- Test asserts `count == N` (not `count >= 0.9*N`) for the
  "100% on one deployment" probes, because the configured weights
  make the selection deterministic (others are weight 0). If anyone
  introduces a "minimum weight floor" the assertion will catch it.
- Step 3 (delegate path) checks **at least 2 distinct deployment ids**
  appeared rather than a strict distribution, so the test is robust
  to LiteLLM's default routing strategy changes upstream.

## Why this case exists

The unit test
`tests/test_litellm/router_strategy/test_time_weighted_router.py`
covers the strategy in isolation with a `_SpyRouter`. This e2e case
is what proves:

- `litellm_extras.install()` was actually invoked during
  `proxy_startup_event` and the strategy is bound on the live Router
- The PATCH model endpoint reaches our reinstall hook in
  `_update_llm_router` and the new bands take effect immediately
- `_filter_blocked_deployments` and `_filter_cooldown_deployments`
  are still applied in front of our strategy (we did not bypass them)
- The delegate path is intact so models that don't opt in see zero
  behavioral change

## How to run

```bash
proxy start --with-mock              # local 4011, mock-anthropic at :4012
e2e/tools/run-all-cases --mock-only  # case 34 runs as part of the suite
```

Or in isolation:

```bash
PROXY_URL=http://localhost:4011 MASTER_KEY=sk-e2e-test \
    python3 e2e/cases/data/34_time_weighted_routing.py
```

Exit codes: `0` PASS, `77` SKIP (proxy/mock unreachable), anything
else FAIL.
