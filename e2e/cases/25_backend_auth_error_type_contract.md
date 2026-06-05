# Case 25 — Backend `error.type` contract on 401 / 403

## Goal

Lock in the wire contract that Wave 6a's `_classify_auth_failure`
established: every 401 / 403 response from the proxy carries a
structured `error.type` value drawn from `ProxyErrorTypes`. The
frontend SPA (PR #68's `networking.tsx` 401 redirect taxonomy) reads
this field to decide between "session expired → redirect to login" vs
"permission denied → inline error". If the backend silently drops the
field (e.g. a future upstream refactor consolidates auth-error paths
through a code path that doesn't call `_classify_auth_failure`), the
UI falls back to substring heuristics and the redirect decisions
silently drift.

## Background

Wave 6a (PR #56) added two pieces to
`litellm/proxy/auth/auth_exception_handler.py`:

- `_classify_auth_failure(e: Exception) -> ProxyErrorTypes` — inspects
  status code + message text and returns one of the four structured
  values listed below.
- `UserAPIKeyAuthExceptionHandler._handle_authentication_error` is the
  call site that wraps the classifier into `ProxyException.type`.

Wave 7 + PR #68 made the frontend honor these:

| `error.type` value | UI action |
|---|---|
| `auth_session_expired` | clear cookies, redirect to `/login` |
| `auth_invalid_credentials` | clear cookies, redirect to `/login` |
| `token_not_found_in_db` | clear cookies, redirect to `/login` |
| `auth_permission_denied` | inline toast, stay on page |
| `auth_error` (fallback) | substring heuristic on the `message` field |

Without this case, neither side of that contract is locked end-to-end.

## What the fixture does

`e2e/cases/data/25_backend_auth_error_type_contract.sh` runs four
probes against the running proxy and asserts each `error.type` value:

| Probe | Request | Expected `error.type` |
|---|---|---|
| A1 | `POST /v1/chat/completions` with no `Authorization` header | `auth_invalid_credentials` |
| A2 | `POST /v1/chat/completions` with `Authorization: Bearer notavalidkey` (no `sk-` prefix) | `auth_invalid_credentials` |
| A3 | `POST /v1/chat/completions` with `Authorization: Bearer sk-doesnotexist-case25` (well-formed but absent from `LiteLLM_VerificationTokenTable`) | `token_not_found_in_db` |
| A4 | Provision an `internal_user`-role virtual key via the master key, then `POST /key/generate` with that key (admin-only route) | `auth_permission_denied` |

`auth_session_expired` is intentionally not exercised here — driving it
deterministically requires a `duration: "1s"` key + a 2-second sleep,
which is fragile under load. The classifier's "expired" / "revoked" /
"key has been deleted" / "key has expired" markers are covered by
`tests/test_litellm/proxy/auth/test_auth_exception_handler.py` instead.

All four probes return HTTP 401 (not 403, even for permission denial —
that's an upstream quirk of how `auth_pipeline_failure` is raised).
The case asserts on `error.type`, not on the status code.

## Steps

```bash
e2e/tools/proxy start --with-mock     # if not already running
e2e/tools/run-all-cases --mock-only   # case 25 is Tier=mock
# Or run case 25 alone:
bash e2e/cases/data/25_backend_auth_error_type_contract.sh
```

## Expected outcome

```
PASS: A1 no-auth-header → error.type=auth_invalid_credentials
PASS: A2 malformed-key → error.type=auth_invalid_credentials
PASS: A3 bogus-sk-key → error.type=token_not_found_in_db
PASS: A4 internal-user-on-admin-route → error.type=auth_permission_denied
PASS: all 4 auth error.type contract probes hit expected values
```

## When this case will fail

- A future upstream PR routes some auth-pipeline exceptions around
  `UserAPIKeyAuthExceptionHandler._handle_authentication_error`, so
  `_classify_auth_failure` never runs and `error.type` defaults to a
  raw `auth_error` string. **Action**: re-route the new path through
  the classifier.
- The classifier's marker lists in
  `litellm/proxy/auth/auth_exception_handler.py` are pruned to match a
  message-text refactor and the four probe messages no longer match.
  **Action**: add the new markers or update the probes.
- Upstream removes the `error.type` field from `ProxyException`'s JSON
  serialization entirely. **Action**: restore the field; the UI's
  redirect taxonomy depends on it.
