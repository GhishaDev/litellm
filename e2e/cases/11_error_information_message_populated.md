# Case 11 — `error_information.error_message` must be populated on failure

## Goal

Regression guard for an observability bug: when a request fails (e.g.
401 with an invalid virtual key), the row written to
`LiteLLM_SpendLogs.metadata.error_information.error_message` is the
empty string, even though the proxy's HTTP response body and the
captured traceback both contain the full human-readable message.

Dashboard "LLM Failure" rows then look like:

```json
"error_information": {
    "error_code": "401",
    "error_class": "ProxyException",
    "error_message": "",
    "llm_provider": "",
    "traceback": "<855 chars of stack trace>"
}
```

Operations is left clicking individual rows and unpacking traceback
text just to find out whether the failure was auth, budget, rate
limit, or a hook fault. There is no automated alert tripwire for this
class of regression — once `error_message` goes silent, every failure
mode upstream becomes indistinguishable on the dashboard.

### Origin

Observed live in this e2e environment: dashboard polling
`GET /key/list` with a stale (post-restart, no longer in DB) session
token. 26 failures landed in `spend_logs` within 10 seconds, all with
`error_code="401"`, `error_class="ProxyException"`,
`error_message=""`, and `traceback` length 855 chars. Triage was only
possible by manually `psql`-ing the metadata JSON and extracting the
last stack frame.

## Preconditions

- `e2e/tools/proxy status` reports `ready`
- The e2e Postgres container is reachable as `litellm-e2e-db`
  (default for this harness)

No real provider key needed — the case deliberately uses an invalid
bearer token to force the auth-rejection path.

## Steps

```bash
bash e2e/cases/data/11_error_information_message_populated.sh
echo "exit=$?"
```

The script:

1. Generates a fresh sentinel bearer (`sk-case11-$(date +%s%N)`) so its
   SHA-256 hash is guaranteed unique to this run
2. POSTs `/v1/chat/completions` with that bearer — expects HTTP 401
3. Sleeps 2s for the async spend-logger to land
4. Queries the latest `LiteLLM_SpendLogs` row for that key hash
5. Asserts:
   - `error_information.error_code == "401"`
   - `error_information.error_class == "ProxyException"`
   - `error_information.error_message` non-empty AND contains
     `"Authentication Error"` or `"Invalid proxy server token"`
   - `error_information.traceback` length > 100 chars

## Expected

After the fix lands:

```
stored error_code:    '401'
stored error_class:   'ProxyException'
stored error_message: 'Authentication Error, Invalid proxy server token passed. ...'
stored traceback len: 855
PASS: error_information populated correctly
exit=0
```

## Current status — GREEN

On `fix/prometheus-prompt-cache-tokens` after the fix landed in
`litellm/litellm_core_utils/litellm_logging.py` at
`StandardLoggingPayloadSetup.get_error_information`:

```
stored error_message: 'Authentication Error, Invalid proxy server token passed. ...'
stored traceback len: 855
PASS: error_information populated correctly
exit=0
```

### Root cause (resolved)

`ProxyException` (`litellm/proxy/_types.py:3453`) sets
`self.message = str(message)` but does NOT call
`super().__init__(message)` and does NOT define `__str__`, so
`str(ProxyException(...))` returns the empty string.

`get_error_information` previously used `error_message = str(original_exception)`,
which silently dropped the human-readable message for every
`ProxyException` that reached the spend-logger.

### Fix

`get_error_information` now reads from `.message` attribute first,
falling back to `str(exc)` only when `.message` is absent. The
`.message` attribute is set uniformly by `ProxyException` and every
`litellm.exceptions.*` class, so the change is backward-compatible
with non-litellm exception types via the `str()` fallback.

### Companion unit test

`tests/test_litellm/litellm_core_utils/test_litellm_logging.py`:
- `test_get_error_information_prefers_message_attribute_over_str`
- `test_get_error_information_falls_back_to_str_when_no_message_attr`

## Failure modes

| Symptom | Cause |
|---|---|
| `FAIL: no spend_logs row found` | Async logger queue is backed up — bump the `sleep 2` to 5; or DB is not the one the proxy is wired to |
| `FAIL: expected HTTP 401, got 200` | The `sk-case11-...` sentinel collided with an existing key (effectively impossible with `%s%N` granularity, but rerun to be sure) |
| `FAIL: error_class != ProxyException` | A different exception class is being caught — likely a regression in auth_checks; the test should still be RED |
| Mixed PASS / FAIL across reruns | Reruns within the same nanosecond would collide; check that the sentinel hash is actually unique by tailing proxy logs |

## Notes

This case is **observability-only** — failed auth requests still
return 401 to the client and never reach a provider, so the bug is
not a billing or correctness issue. But the cost in production
triage time is real: a fleet of 401s with empty `error_message`
costs an operator ~10 minutes per incident vs. ~30 seconds when the
message is populated.

Treat the RED state as informational until the fix ships; do not
revert / pause this case to silence it.
