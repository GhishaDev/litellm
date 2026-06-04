# CLAUDE.md

Guidance for Claude Code (claude.ai/code) when working in this repo.
This is an internal fork of `BerriAI/litellm` pinned to a specific
upstream `vX.Y.Z-stable` tag, with internal fixes layered on top.
See **Current pinning** below for the active pin and branch names.

## Current pinning

This fork pins to one upstream `vX.Y.Z-stable` tag at a time and lives
in a parallel set of branches named after that pin. Today:

- **Upstream pin**: `v1.83.10-stable`
- **Ship branch**: `ship/v1.83.10`
- **Upstream-sync branch**: `internal/v1.83.10-stable`
- **Internal release tag pattern**: `v1.83.10-internal.N`
- **Latest release**: see `git tag -l 'v1.83.10-internal.*' --sort=-v:refname | head -1`

When the pin changes (version bump), update **this block** and the
Branching strategy table below. Every other reference in this file uses
"the ship branch" / "the upstream-sync branch" generically.

## Hot path — read this before doing anything

- Before writing an internal fix, check **Fork tier classification**.
  Tier C/D fixes default to upstream PRs first; only carry on the ship
  branch if upstream rejects or scheduling demands it.
- Every fix PR targets **the ship branch** (see Current pinning), not
  the upstream-sync branch (has 1700+ upstream-sync commits) and not
  `litellm_internal_staging` (pure upstream tracker).
- Run `uv run black .` before committing — CI enforces.
- Full-stack scenarios (DB schema, background jobs, real HTTP) go under
  `e2e/cases/NN_*.md`, not into bespoke `tests/` integration files.
- Proxy DB access uses Prisma model methods (`prisma_client.db.<model>`)
  only — no raw SQL.
- `LLMClientCache._remove_key()` must never close HTTP/SDK clients;
  in-flight requests still hold them.

## Agent constraints (this fork)

Do not take these actions autonomously without explicit instruction:

- Push to the ship branch, the upstream-sync branch, or any tag.
  Always work via a `fix/*` branch + PR.
- Run `scripts/release-tag.sh`. It has an interactive `[y/N]` prompt
  meant for the human releaser — suggest `! scripts/release-tag.sh v...`
  so they own the confirmation.
- Edit files under `litellm/proxy/_experimental/out/`. Those are
  upstream Next.js build artifacts; ignore the `git status` noise there.
- Skip pre-commit hooks (`--no-verify`), bypass signing, or amend an
  already-pushed commit.

## Fork tier classification

Every change to this repo belongs to one of four tiers. The tier
dictates where the change lives and whether to push it upstream.

| Tier | Description | Typical location | Upstream policy |
|---|---|---|---|
| **A** | Truly company-specific logic | `litellm_extras/` | Never push upstream |
| **B** | Internal infra / branding | `.github/workflows.*`, `Dockerfile`, `e2e/`, internal navbar version | Never push upstream |
| **C** | Universal bug fix | Touches `litellm/` core | **Default: submit PR to BerriAI/litellm.** Only carry on the ship branch if upstream rejects or scheduling demands it |
| **D** | Universal mechanism + company opinion | Touches `litellm/` core | **Default: submit upstream as a hook/config + carry our policy locally.** Make the mechanism configurable so upstream accepts it |

Every PR description must declare `Tier: A/B/C/D`. For Tier C and D,
PRs must also answer "Tried upstream first? (link or justification)".
This keeps the fork's universal-bug-fix delta as small as possible so
future version bumps stay cheap.

The PR template (`.github/pull_request_template.md`) enforces this with
a checkbox section.

## Upstream sync cadence

The pinned upstream stable line keeps receiving `.patch.N` releases
after we pin (e.g. `v1.83.10-stable.patch.1`). Upstream `main` moves
daily via nightly tags. Without a sync cadence, every version bump
becomes a multi-week project. The schedule below keeps drift bounded.

**Monthly** (first Monday):

- Run `scripts/upstream-sync-check.sh` to list missed patches on the
  pinned line, newer minor lines, `upstream/main` commit volume, and
  security-flagged subjects since our pin.
- Triage security fixes (filter for `[SECURITY]`, `fix(auth)`, CVE
  labels). Open `fix/*` PRs for each must-backport item.
- 30-minute review with one other engineer.

**Quarterly**:

- Evaluate whether to bump to a newer stable line (e.g. 1.87.x → 1.88.x).
  See "Cutting an internal release" and any version-bump runbook in
  `e2e/cases/` or `docs/`.
- Re-classify the carried ship-branch delta — any Tier C/D fix that
  has been upstreamed by someone else? Drop it.

**Never**:

- Sit on a pinned tag for more than 6 months without an explicit
  sustainability discussion. Beyond 12 months, the fork begins to
  permanently diverge.

## Development workflow

```bash
# Install
make install-dev          # core dev deps
make install-proxy-dev    # proxy with full feature set
make install-test-deps    # full local test env + Prisma client

# Run tests
make test-unit            # tests/test_litellm with 4 workers
make test-integration     # everything except unit
uv run pytest tests/path/to/test_file.py -v
uv run pytest tests/path/to/test_file.py::test_function -v

# Lint / format
make lint                 # Ruff + MyPy + Black + circular-import + import-safety
make format               # Black only
uv run black .            # MANDATORY before commit
uv run python script.py   # for non-test scripts
```

## Branching strategy

Branch names are derived from the current pin (see Current pinning).
The examples below use the current pin `v1.83.10`.

| Branch / tag | Purpose | Stays clean? |
|---|---|---|
| `<pin>-stable` tag — e.g. `v1.83.10-stable` | Immutable upstream pin | yes — never moves |
| `ship/<pin>` — e.g. `ship/v1.83.10` | Long-term ship — advances only via merged `fix/*` PRs | yes |
| `internal/<pin>-stable` — e.g. `internal/v1.83.10-stable` | **Optional** bump-preview sandbox. Test-merge `upstream/main` here before a version bump to surface conflicts. Not required for routine sync — that's what `scripts/upstream-sync-check.sh` is for | no — accumulates upstream merges if used |
| `litellm_internal_staging` | Pure upstream tracker for `BerriAI/litellm` | tracks upstream |
| `fix/<name>` | Per-bug feature branch | yes — merged into the ship branch |

```bash
SHIP=ship/v1.83.10   # see Current pinning for the active ship branch
git checkout -b fix/<name> "$SHIP"
gh pr create --base "$SHIP" --head fix/<name>
```

The ship branch only moves when a `fix/*` PR merges, so it stays
exactly `TAG + merged fixes`. Fixes never have to rebase against moving
upstream. Routine awareness of upstream drift comes from
`scripts/upstream-sync-check.sh` (see Upstream sync cadence); the
`internal/<pin>-stable` sandbox is opt-in — use it before a version
bump, not as a continuous mirror.

## Cutting an internal release

Always use `scripts/release-tag.sh`. Never `git tag` by hand.

```bash
# After fix/* PR merged into the ship branch and local ship is up-to-date:
scripts/release-tag.sh <pin>-internal.N   # e.g. v1.83.10-internal.8
```

Tag format is enforced: `^v[0-9]+\.[0-9]+\.[0-9]+-internal\.[0-9]+$`.
`N` is monotonically increasing within a pin — don't reset, skip, or
reuse. On a version bump, `N` restarts from 1 under the new pin. Find
the current latest:

```bash
# See Current pinning for the active <pin> prefix
git tag -l 'v1.83.10-internal.*' --sort=-v:refname | head -1
```

Pushing the tag triggers `.github/workflows/release-docker.yml` →
multi-arch image published as:
- `zsk2026/litellm:vX.Y.Z-internal.N`
- `zsk2026/litellm:vX.Y.Z-stable` (rolling pointer to latest `internal.N`)

## Architecture notes

- Provider transformations live in `litellm/llms/<provider>/` and inherit
  from `litellm/llms/base.py`. Adding a provider = new subdir + base
  subclass + input/output transforms.
- `Router` (`litellm/router.py`, sync-friendly) vs `proxy_server`
  (`litellm/proxy/proxy_server.py`, async FastAPI) — never call sync
  Router methods from async proxy code.
- Internal code in this fork lives under `litellm_extras/` to keep
  upstream `litellm/` untouched. Don't import `litellm_extras` from
  inside `litellm/`.
- UI is a Next.js build under `litellm/proxy/_experimental/out/` —
  committed to git by upstream, occasionally changes format (`.html` ↔
  `/index.html`). Add `litellm/proxy/_experimental/out/` to
  `.git/info/exclude` locally to silence the noise.

## Code style

- Black formatter, Ruff linter, MyPy type checker.
- Pydantic v2 for data validation; type hints required on public APIs.
- **Avoid imports within methods** — module-level imports only. Inline
  imports hide dependencies and break static analysis. Only exception:
  breaking a circular import where unavoidable.
- Prefer `{**original, "key": new_value}` over `dict(obj)` + mutation.
- Guard at resolution time: when resolving an optional via fallback
  chain (`a or b or ""`), raise immediately if the resolved result
  being empty is an error. Don't pass empty strings or sentinels
  downstream.
- Extract complex comprehensions that call into the DB/manager into a
  named helper — don't inline them.
- FastAPI handlers mixing required and optional params: mark required
  ones with `= Query(...)` / `= Form(...)` explicitly. Otherwise you'll
  get silent 422s when the required param is missing.

## Test discipline

Tests live in `tests/test_litellm/` (unit), `tests/llm_translation/`
(per-provider integration), `tests/proxy_unit_tests/` (proxy), and
`tests/load_tests/`. Full-stack scenarios go in `e2e/cases/`.

- **Write assertions from the spec, not the impl.** For new features,
  the `e2e/cases/NN_*.md` runbook IS the spec — write it before the
  fixture and the impl. For bug fixes, the issue's repro steps are the
  spec. Tests reverse-engineered from controller code can never expose
  a doc-vs-impl gap because they were generated from the gap.
- **Lock known doc-vs-impl gaps with `pytest.mark.xfail(strict=True)`**,
  never `pytest.mark.skip` or a `TODO` comment. Skipped tests vanish
  from CI signal and rot. `xfail(strict=True)` keeps the gap visible
  AND flips to a build failure (XPASS) the moment the impl catches up,
  forcing cleanup. Include a `reason=` that points at the upstream
  issue or internal ticket. Pair with a plain `test_*_current_behavior`
  that pins the wrong-but-current behavior so drift surfaces too:
  ```python
  def test_x_current_behavior():
      assert actual == BUGGY_VALUE  # codifies the bug

  @pytest.mark.xfail(strict=True, reason="BerriAI/litellm#NNNNN")
  def test_x_correct_behavior():
      assert actual == EXPECTED_VALUE  # flips XPASS when impl catches up
  ```
- **"Test exposed a bug" ≠ "bug is fixed".** Adding a failing test (or
  a strict-xfail) documents a gap; it does not close one. Fix the impl
  in the same PR, or call out the deferral in the PR summary
  ("exposes #N, fix deferred to #M").
- Keep monkeypatch stubs in sync with real signatures. When a function
  gains a new optional param, update every `fake_*` / `stub_*` to accept
  it (even as `**kwargs`). Stale stubs fail with `unexpected keyword
  argument` and mask real bugs.
- Test all branches of name→ID resolution: (1) name resolves and UUID
  allowed, (2) name resolves but UUID not allowed, (3) name doesn't
  resolve. The silent-fallback path is where access-control bugs hide.
- Always add tests when introducing a new entity type — if existing
  test files cover other entity types, add corresponding cases.

## Proxy database access

Use Prisma model methods. **Never raw SQL** (`execute_raw`/`query_raw`).

- Client: `prisma_client.db.<model>` with `.upsert`/`.find_many`/
  `.find_unique`/`.update`/`.update_many`.
- **No N+1 queries.** Batch-fetch with `{"in": ids}` and distribute
  in-memory.
- Batch writes via `create_many`/`update_many`/`delete_many` (these
  return counts only; `update_many`/`delete_many` no-op silently on
  missing rows). Multiple writes to the same table in `batch_()` →
  order by primary key to avoid deadlocks.
- Push filter/sort/group/aggregate work into SQL. Verify Prisma
  generates expected SQL — e.g. prefer `group_by` over
  `find_many(distinct=...)` (the latter does client-side processing).
- For results > ~10 MB, paginate. Prefer cursor-based pagination
  (`skip` is O(n)). Always include explicit `order`.
- Use `select` on wide tables to fetch only needed columns. Downstream
  code must not access unselected fields.
- Check index coverage in `schema.prisma`. Prefer extending an
  existing index over adding a new one (unless `@@unique`). Only add
  indexes for large/frequent queries.
- **Schema changes must update all four `schema.prisma` copies**
  (`schema.prisma`, `litellm/proxy/`, `litellm-proxy-extras/`,
  `litellm-js/spend-logs/` for SpendLogs) plus a migration under
  `litellm-proxy-extras/litellm_proxy_extras/migrations/`.

## HTTP client cache safety

**`LLMClientCache._remove_key()` must not call `close()` / `aclose()`
on evicted clients** — in-flight requests still hold them, and closing
mid-flight raises `RuntimeError: Cannot send a request, as the client
has been closed.` after the 1-hour TTL expires. Cleanup happens at
shutdown via `close_litellm_async_clients()`.

## MCP OAuth / OpenAPI transport mapping

- `TRANSPORT.OPENAPI` is a UI-only concept. The backend only accepts
  `"http"`, `"sse"`, or `"stdio"`. Map to `"http"` before any API call
  (including pre-OAuth temp-session calls).
- FastAPI validation errors return `detail` as `[{loc, msg, type}, ...]`.
  Error extractors must handle: array (map `.msg`), string, nested
  `{error: string}`, and a fallback.
- If an MCP server has `authorization_url` stored, skip OAuth discovery
  (`_discovery_metadata`) — the server URL for OpenAPI MCPs is the spec
  file, not the API base, and fetching it causes timeouts.
- `client_id` is optional in `/authorize` — if the server has stored
  `client_id` in credentials, use that. Never require callers to
  re-supply it.

## MCP credential storage

- OAuth and BYOK credentials share `litellm_mcpusercredentials`,
  distinguished by `"type"` in the JSON payload (`"oauth2"` vs plain
  string). When deleting OAuth credentials, check type first to avoid
  deleting a BYOK credential for the same `(user_id, server_id)` pair.
- Pass raw `expires_at` timestamps to the client — never `None` for
  expired credentials. The frontend computes the "Expired" display
  state from the timestamp.
- Catch `RecordNotFoundError` (not bare `except Exception`) for
  "already deleted" in credential delete endpoints.

## Browser storage safety (UI)

**Never write LiteLLM access tokens or API keys to `localStorage`** —
use `sessionStorage` only. `localStorage` survives browser close and is
readable by any injected script (XSS).

Shared utility functions (e.g. `extractErrorMessage`) belong in
`src/utils/` — never define inline in hooks or duplicate across files.

## UI component library

New UI work uses `antd`. We are migrating off `@tremor/react` — do not
introduce new `Badge`/`Text`/`Card`/`Grid`/`Title` imports from
`@tremor/react` in any new or modified file. Use `antd` equivalents:
`Tag` for labels, `Typography.Text`/`Typography.Title`/
`Typography.Paragraph` for textual content (avoid plain `<span>`/`<p>`/
`<h*>` when Typography fits), `Card` from `antd`. `antd` has no
`"yellow"` Tag color — use `"gold"`.

## UI / backend consistency

When wiring a new UI entity to an existing backend endpoint, verify the
backend contract (single value vs. array, required vs. optional) and
match UI controls — e.g. single-select dropdown when the backend
accepts a single value, not a multi-select.

## Setup wizard (`litellm/setup_wizard.py`)

- Single `SetupWizard` class with `@staticmethod` methods. No
  module-level functions except `run_setup_wizard()` and pure helpers
  (color, ANSI).
- Validate credentials via `litellm.utils.check_valid_key(model, api_key)`,
  not a custom completion call.
- Don't hardcode provider env-key names or model lists. Add a
  `test_model` field per provider entry to drive `check_valid_key`;
  set to `None` for providers that can't be validated with a single
  key (Azure, Bedrock, Ollama).

## CI supply-chain safety

These rules apply to every download in CI — binaries, install scripts,
language version managers, package repos. No exceptions.

- **Never pipe a remote script into a shell** (`curl ... | bash`).
  Download to a file, verify SHA-256, then install.
- **Pin every external tool to a specific version** with a full URL.
  No `latest` / `stable` — those silently change under you.
- **Verify checksums on downloaded binaries.** Use the provider's
  `.sha256` sidecar if available, otherwise compute and hardcode.
- Prefer reusable CircleCI `commands:` so a tool is installed/verified
  in exactly one place, referenced everywhere with `- install_<tool>`.
- Don't add tools just because they were there. Audit each external
  dependency on every CI touch — remove if a shell one-liner or an
  in-image tool can replace it.

## Enterprise features

Enterprise-only code in `enterprise/`. Optional features enable via env
vars; separate licensing and authentication.

## Database migrations

Prisma handles schema migrations. Migration files auto-generate with
`prisma migrate dev`. Always test migrations against both PostgreSQL
and SQLite.

## Troubleshooting: DB schema out of sync after proxy restart

`litellm-proxy-extras` runs `prisma migrate deploy` on startup using
**its own** bundled migrations, which may lag behind schema changes in
the current worktree. Symptoms: `Unknown column`, `Invalid prisma
invocation`, missing data on new fields.

Diagnose: `\d "TableName"` in psql vs `schema.prisma` — missing columns
confirm.

Fix:
1. **Permanent** — `prisma migrate dev --name <description>` in the
   worktree. The generated file is picked up by `prisma migrate deploy`
   on next startup.
2. **Local dev** — `psql -d litellm -c "ALTER TABLE ... ADD COLUMN IF
   NOT EXISTS ..."` after each proxy start. Dev-only, not production.
3. **PyPI install** — if `litellm-proxy-extras` is installed from PyPI,
   its migration directory must include the new file. Update the
   package or apply the migration manually until the next release.

## GitHub templates

- Bug reports: `.github/ISSUE_TEMPLATE/bug_report.yml` — what happened
  vs. expected + log output + LiteLLM version.
- Feature requests: `.github/ISSUE_TEMPLATE/feature_request.yml` —
  describe + motivation + use case.
- PRs: `.github/pull_request_template.md` — at least 1 test in
  `tests/litellm/`; `make test-unit` must pass.
