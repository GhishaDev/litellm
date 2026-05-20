# Case 16 — `reset_budget_windows` background job runs without Prisma error

## Goal

The periodic `ResetBudgetJob.reset_budget_windows` tick must complete
without raising
`prisma.errors.MissingRequiredValueError: where.budget_limits.not: A value is required but not set`.

The bug: `prisma-client-python` does not support null-filtering on
`Json?` columns ([RobertCraigie/prisma-client-py#714](https://github.com/RobertCraigie/prisma-client-py/issues/714))
— the old `find_many(where={"budget_limits": {"not": None}})` call had
its `None` silently dropped during serialization and the query engine
rejected the request. Net effect on a proxy with any key or team that
has `budget_limits` set: the multi-window reset job blew up every tick
and **no per-key / per-team budget window ever reset**.

Verifies the fix for [BerriAI/litellm#26346](https://github.com/BerriAI/litellm/pull/26346)
(cherry-picked in our ship branch).

## Preconditions

- `e2e/tools/proxy status` reports `ready` (Postgres + litellm up)
- The proxy was started with
  `PROXY_BUDGET_RESCHEDULER_MIN_TIME=10` / `MAX_TIME=15` env vars
  (set by default in `e2e/_config/docker-compose.yml`). If you brought
  the proxy up with a custom override that left the upstream 10-min
  default in place, the case will SKIP — restart with
  `e2e/tools/proxy restart` to pick up the e2e defaults.
- No API keys required: the case never hits an upstream provider.

## Steps

```bash
PROXY="${PROXY_URL:-http://localhost:4011}"
MASTER="${MASTER_KEY:-sk-e2e-test}"
CONTAINER="${PROXY_CONTAINER:-litellm-e2e}"
SUFFIX="case16-$(date +%s%N)"

# 1. Mark the log cursor — we only grep logs from this point onward so
#    earlier (unrelated) failures aren't double-counted.
SINCE_TS=$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)

# 2. Create a key with `budget_limits` so the LiteLLM_VerificationToken
#    table has at least one row with `budget_limits IS NOT NULL`. Without
#    such a row the reset job's WHERE clause matches nothing and the bug
#    would never trigger.
KEY=$(curl -sS "$PROXY/key/generate" \
    -H "Authorization: Bearer $MASTER" -H "Content-Type: application/json" \
    -d "{\"key_alias\":\"$SUFFIX-key\",\"budget_limits\":[{\"max_budget\":100.0,\"budget_duration\":\"1d\"}]}" \
    | jq -r '.key')

# 3. Create a team with `budget_limits` for the LiteLLM_TeamTable side.
TEAM=$(curl -sS "$PROXY/team/new" \
    -H "Authorization: Bearer $MASTER" -H "Content-Type: application/json" \
    -d "{\"team_alias\":\"$SUFFIX-team\",\"budget_limits\":[{\"max_budget\":100.0,\"budget_duration\":\"1d\"}]}" \
    | jq -r '.team_id')

# 4. Wait for ≥ 2 reset_budget_job ticks
#    (interval=10–15s under the e2e override, so 35 s = 2–3 ticks).
sleep 35

# 5. Inspect proxy logs since the start of the case for either the
#    Prisma error message or the higher-level "Failed to reset budget
#    windows" wrapper our fix sites print on exception.
LOGS=$(docker logs --since "$SINCE_TS" "$CONTAINER" 2>&1)
echo "$LOGS" \
    | grep -E "MissingRequiredValueError|Failed to reset budget windows" \
    | head -20    # → must print NOTHING

# 6. Cleanup
curl -sS "$PROXY/key/delete" \
    -H "Authorization: Bearer $MASTER" -H "Content-Type: application/json" \
    -d "{\"keys\":[\"$KEY\"]}" > /dev/null
curl -sS "$PROXY/team/delete" \
    -H "Authorization: Bearer $MASTER" -H "Content-Type: application/json" \
    -d "{\"team_ids\":[\"$TEAM\"]}" > /dev/null
```

## Expected

- Step 2: `KEY` is a non-empty `sk-...` string
- Step 3: `TEAM` is a non-empty UUID
- Step 5: `grep` prints nothing — neither the raw Prisma exception nor
  the wrapper log line ever appears for the duration of the case.
- Step 6: cleanup returns 200; no orphaned keys/teams left behind.

## Failure modes

| Symptom | Cause |
|---|---|
| Step 5 prints `prisma.errors.MissingRequiredValueError: ... where.budget_limits.not ...` | The fix did not land — `reset_budget_windows` is still using `find_many(where={"budget_limits": {"not": None}})`. Re-verify `litellm/proxy/common_utils/reset_budget_job.py` uses `query_raw('... WHERE budget_limits IS NOT NULL')` for both the key and team paths. |
| Step 5 prints `Failed to reset budget windows for keys` but no `MissingRequiredValueError` | Some other exception in the reset path — read the surrounding traceback in the log block printed by step 5. |
| Step 2 or 3 returns a JSON error about `budget_limits` schema | API contract for `budget_limits` changed upstream. The current schema (BudgetLimitEntry in `litellm/proxy/_types.py`) requires `max_budget` + `budget_duration`. The endpoint docstrings still document `budget_limit` + `time_period` — that's wrong, ignore them. |
| `docker logs --since "$SINCE_TS"` is empty even after 35 s | Wrong container name. Confirm `PROXY_CONTAINER=litellm-e2e` (the compose container name in `e2e/_config/docker-compose.yml`). |
| Case skips with "reset interval too long" message | Proxy was started before the e2e `PROXY_BUDGET_RESCHEDULER_*` defaults were added — `e2e/tools/proxy restart` to pick them up. |
