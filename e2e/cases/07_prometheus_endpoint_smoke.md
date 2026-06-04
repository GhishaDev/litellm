# Case 07 — `/metrics` endpoint smoke test

## Goal

Quick sanity check that runs in <5 seconds and requires **no provider
key**: confirm the proxy exposes `/metrics`, that the response is the
standard Prometheus text format, and that our two new metric **HELP
lines** are registered (proves the new code path is in the running
image — not just the source tree).

## Preconditions

- `e2e/tools/proxy status` reports `ready`

## Steps

```bash
# 1. Raw HTTP — confirm 200 + content type
curl -sSI "$(e2e/tools/proxy url)/metrics" | head -3

# 2. Snapshot — proves the text parses cleanly
e2e/tools/metrics snapshot > /tmp/snap.json
jq 'keys | length' /tmp/snap.json
# (just a number; should be many dozens of metric names)

# 3. Look for the two new metric names in the HELP lines
curl -s "$(e2e/tools/proxy url)/metrics" | \
    grep -E "^# HELP litellm_prompt_cache_(read|creation)_tokens_metric"
```

## Expected

- Step 1: `HTTP/1.1 200 OK` and a content-type containing `text/plain`
- Step 2: snapshot key count > 30 (proxy emits many metrics; exact
  number depends on traffic)
- Step 3: **two** lines printed, one per new metric, each starting with
  `# HELP litellm_input_cached_tokens_metric` and
  `# HELP litellm_input_cache_creation_tokens_metric`

## What this proves vs doesn't

- Proves: image contains our fix; PrometheusLogger is initialized;
  metric names made it into the exposition text
- Does NOT prove: counter logic increments correctly — that's Cases 01-06

## Failure mode

If step 3 prints nothing, run `e2e/tools/proxy rebuild`. The container
was probably built from an older source tree.
