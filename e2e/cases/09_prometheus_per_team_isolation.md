# Case 09 — Per-team Prometheus label isolation

## Goal

Two teams + one key per team. Each team makes a request. Verify
Prometheus metric samples are **split by `team` label** — i.e. the
counters increment on distinct series, not aggregated under
`team='None'`. This is the load-bearing assumption for any per-team
billing dashboard.

## Preconditions

- Postgres + litellm up (`e2e/tools/proxy status` reports `ready`)
- `ANTHROPIC_API_KEY` set

## Steps

```bash
NOW=$(date +%s)
TEAM_A_ALIAS="team-a-$NOW"
TEAM_B_ALIAS="team-b-$NOW"

# 1. Create two teams
e2e/tools/teams new --alias "$TEAM_A_ALIAS" --models claude-sonnet-cache \
    > /tmp/team_a.json
e2e/tools/teams new --alias "$TEAM_B_ALIAS" --models claude-sonnet-cache \
    > /tmp/team_b.json
TEAM_A_ID=$(jq -r '.response.team_id' /tmp/team_a.json)
TEAM_B_ID=$(jq -r '.response.team_id' /tmp/team_b.json)
echo "team_a=$TEAM_A_ID  team_b=$TEAM_B_ID"

# 2. Mint a key per team
e2e/tools/keys new --alias "key-a-$NOW" --team-id "$TEAM_A_ID" \
    --models claude-sonnet-cache --duration 30m > /tmp/key_a.json
e2e/tools/keys new --alias "key-b-$NOW" --team-id "$TEAM_B_ID" \
    --models claude-sonnet-cache --duration 30m > /tmp/key_b.json
KEY_A=$(jq -r '.response.key' /tmp/key_a.json)
KEY_B=$(jq -r '.response.key' /tmp/key_b.json)

# 3. Snapshot baseline
e2e/tools/metrics snapshot > /tmp/m_before_09.json

# 4. Each team makes a cached request — unique seed AND unique user_id
#    per team. seed keeps the cache prefix distinct; user_id keeps the
#    gateway's sticky-LB upstream account distinct, so the two teams'
#    cache namespaces don't collide on the corp Anthropic gateway.
e2e/tools/call --provider anthropic --cache ephemeral --ttl 5m \
    --prompt-tokens 1500 --seed "$TEAM_A_ALIAS" --api-key "$KEY_A" \
    --user-id "$TEAM_A_ALIAS-user" \
    > /tmp/call_09a.json
e2e/tools/call --provider anthropic --cache ephemeral --ttl 5m \
    --prompt-tokens 1500 --seed "$TEAM_B_ALIAS" --api-key "$KEY_B" \
    --user-id "$TEAM_B_ALIAS-user" \
    > /tmp/call_09b.json
echo "team_a usage:"; jq '.response.usage.prompt_tokens_details' /tmp/call_09a.json
echo "team_b usage:"; jq '.response.usage.prompt_tokens_details' /tmp/call_09b.json

# 5. Snapshot after
sleep 1
e2e/tools/metrics snapshot > /tmp/m_after_09.json

# 6. Verify TWO distinct series — one per team_alias
echo "--- team_a delta ---"
e2e/tools/metrics diff /tmp/m_before_09.json /tmp/m_after_09.json \
    --metric litellm_input_cache_creation_tokens_metric \
    --label team_alias=$TEAM_A_ALIAS
echo "--- team_b delta ---"
e2e/tools/metrics diff /tmp/m_before_09.json /tmp/m_after_09.json \
    --metric litellm_input_cache_creation_tokens_metric \
    --label team_alias=$TEAM_B_ALIAS

# 7. Cleanup
e2e/tools/keys delete --key "$KEY_A" > /dev/null
e2e/tools/keys delete --key "$KEY_B" > /dev/null
e2e/tools/teams delete --team-id "$TEAM_A_ID" > /dev/null
e2e/tools/teams delete --team-id "$TEAM_B_ID" > /dev/null
```

## Expected

- Both `teams new` calls return 200 with valid `team_id`s
- Both `keys new` calls return 200 and the keys are scoped to their teams
- Step 6 produces **two separate diff rows**:
  - one with `team_alias=team-a-<ts>`, delta = team A's `cache_creation_tokens`
  - one with `team_alias=team-b-<ts>`, delta = team B's `cache_creation_tokens`
- No row has `team_alias='None'` for these test calls

## Failure modes

| Symptom | Cause |
|---|---|
| Both deltas land under `team='None'` | proxy isn't propagating `user_api_key_team_id` into the StandardLoggingPayload metadata |
| `teams new` 4xx | DB migration not complete; check `e2e/tools/proxy logs` |
| `keys new` 403 "team not found" | typo in `--team-id`; double-check `team_id` extraction from JSON |
| Cache read pollution (one team reads the other's cache) | seeds collided — verify `$TEAM_A_ALIAS != $TEAM_B_ALIAS` and both are passed as `--seed` |
