# Upstream PR Queue

Living list of fork-carried changes that should be submitted to
`BerriAI/litellm`. Maintained alongside CLAUDE.md's **Fork tier
classification** policy:

> Tier C (universal bug fix) defaults to upstream PR.
> Tier D (universal mechanism + company opinion) defaults to upstream
> as a hook/config + carry policy locally.

Goal: keep the fork's `ship/<pin>` delta small enough that future
version bumps stay cheap (see CLAUDE.md > Upstream sync cadence).

## Upstream workflow recap

For each PR (full rules in `~/.claude/projects/.../memory/reference_upstream_pr_workflow.md`):

1. Open PR against `litellm_oss_branch` (NOT `main`) — fork-originated
   PRs to `main` are auto-rejected by `.github/workflows/guard-main-branch.yml`.
2. Head from `GhishaDev/litellm` (the org fork). Push to remote
   `fork-zsk` (URL `https://github.com/songkuan-zheng/litellm.git`
   redirects to `GhishaDev`); `gh pr create --head GhishaDev:<branch>`.
3. Rebase onto `upstream/litellm_oss_branch` before pushing — that
   branch lags `main` by ~100+ commits.
4. Run `pip install --user --break-system-packages 'black==26.3.1'`
   then `python3 -m black --check --target-version py310 <changed_files>`
   before committing.
5. After opening, comment `@greptileai` to trigger the Confidence Score
   review (template requirement, must be ≥ 4/5 before maintainer review).

## Queue

Status legend:
- `OPEN <#NN>` — PR filed against `BerriAI/litellm`, awaiting review
- `MERGED <#NN>` — upstream merged; drop the local carry on next version bump
- `READY` — code complete on ship branch, PR not yet filed
- `ISSUE-FIRST` — needs a GitHub issue + design discussion before opening PR (Tier D)

### Tier C — submit as-is

| Carry on ship | Upstream | Title | Notes |
|---|---|---|---|
| PR #58 (Wave 6c) | **OPEN #29748** | `fix(proxy): apply user.models filter on /v1/models + /v1/model/info + /v2/model/info` | Closes upstream issue #26420. Includes follow-up `team_id=None` test fix + perf override + 5 edge-case tests. **Backported to ship via PR #72.** |
| PR #49 (Wave 4) | READY | Three independent fixes: Gemini 429 body→status code mapping; preserve `ProxyException.error_message`; strip `total_tokens` from `/v1/messages` | Split into 3 separate upstream PRs — each is unambiguous and can be merged independently. Start with Gemini 429 (most obviously a bug). |
| PR #57 (Wave 6b) | READY | `fix(router): backfill model_cost from static map when deployment-level missing`; `fix(proxy): redact deployment debug names in exception messages` | Two semantic fixes, can split or bundle. Cost backfill is the higher-value piece. |
| PR #52 (Wave 5c) | READY | `fix(anthropic): extend transform_to_anthropic_error to cover broader status code set` | Smallest diff in the queue — builds on upstream's existing helper. Good warm-up after #29748. |
| PR #60 + #67 (Wave 6e + Layer 1 fix) | READY | `fix(proxy): record TTFT on passthrough streaming path + apply per-deployment overrides before fast_path SSE short-circuit` | The fast_path piece (#67) is a regression introduced by upstream PR #28289 — file directly with that PR's commit referenced. Pair with passthrough TTFT (#60). |
| PR #48 (Wave 3 small UI fixes) | READY | Three UI fixes: Gemini provider `api_base` field on credential form; credential-form reset on close; Mode badge rendering on model_list | All independent. File as a single PR with 3 commits since they all touch the credential form area. |
| fix/billing-accuracy-phase-1 (Phase 1 — first commit) | READY | `fix(streaming): reset Anthropic message_start cursor (output_tokens=1) when no message_delta arrives` | Independent, smallest-blast-radius bug in the cancel-billing series. `stream_chunk_builder_utils.py` cursor=1 escape valve. Pure Tier C. Can be filed BEFORE Phase 3 dogfood completes. |
| fix/billing-accuracy-phase-1 (Phase 1 — black-hole catch) | READY | `feat(cancel): catch asyncio.CancelledError in streaming + non-stream proxy paths` | The Phase 1 "BaseException slipping through `except Exception`" hole. SpendLogs row + Langfuse trace closure for cancelled requests. Tier C; mechanism only — the billing strategy stays in our metadata derivation (see Phase 3 row below). |

### Tier D — issue first, then mechanism PR

These ship internal opinions about defaults. Mechanism (the hook/option)
belongs upstream; the value of the default stays in `litellm_extras/`
or `ship/<pin>` config.

| Carry on ship | Upstream | Title | Notes |
|---|---|---|---|
| PR #56 (Wave 6a) | ISSUE-FIRST | Structured `error.type` taxonomy on auth failures (`auth_session_expired` / `auth_invalid_credentials` / `auth_permission_denied` / `token_not_found_in_db`) | Largest strategy impact. **Prerequisite for #68 (UI 401 redirect family).** File issue first laying out the 4-value enum + why UI needs structured discriminator vs. message string. |
| PR #54 (Wave 5a) | ISSUE-FIRST | Add `cache_ttl` label to `litellm_input_cache_creation_tokens_metric` (Anthropic 5m vs 1h split) | Mechanism = label; opinion = whether `prompt_cache_*_tokens_metric` should be dropped. File issue documenting why per-TTL bucketing matters for cost attribution. |
| PR #53 (Wave 5b) | ISSUE-FIRST | Per-deployment `returned_model_name` override | Mechanism = the field on `model_list[].litellm_params`. Existing upstream `_override_openai_response_model` is per-call, not per-deployment. Issue should reference this gap. |
| PR #59 (Wave 6d) | ISSUE-FIRST | Two related opt-ins: thinking-signature retry flag; `anthropic_beta_overrides` per-deployment | Combined Anthropic-features PR. The `anthropic_beta_overrides` part needs the Bedrock-gateway use case in the issue. |
| fix/billing-accuracy-phase-1 (Phase 3 — cancel taxonomy) | ISSUE-FIRST | Cancel-billing taxonomy: orthogonal `delivery_status` + `billing_status` in metadata, derived from existing cancel markers | **Upstream-friendly: StandardLoggingPayloadStatus stays binary (matches upstream)**; all new fields are additive Optional metadata. Issue should propose the 7-row semantic mapping (normal / streaming-partial / shield-success / shield-timeout / zero-chunk / cancel-upstream-error / failure) + the derivation helper as the single source of truth. Dogfood for one internal release before filing the issue. Tier D because we're shipping our derivation as the default policy. See `e2e/cases/data/26-33_*.sh` + the spend_tracking_utils derivation function for spec. |

### Dependent PRs (file after a prerequisite lands)

| Carry on ship | Depends on | Title | Notes |
|---|---|---|---|
| PR #68 (UI 401 redirect re-layer) | Tier D #56 (auth taxonomy) | `fix(ui): wire 401/403 error.type lookup to redirect on session expiry` | UI piece consuming `_classify_auth_failure`'s emitted `error.type`. File only after upstream accepts #56's backend taxonomy — otherwise the UI lookup table has nothing to read. |

## Out of scope (Tier A/B — never upstream)

For reference only. Do NOT submit:

- All `.github/workflows/*` (internal CI)
- `e2e/` directory (internal black-box harness)
- `scripts/release-tag.sh`, `scripts/upstream-sync-check.sh` (internal tooling)
- `litellm_extras/` (company-specific logic)
- `CLAUDE.md`, `AGENTS.md`, `MEMORY.md`, this file (`UPSTREAM_PR_QUEUE.md`)
- `e2e/_config/mock_provider.py` (our test mock — upstream has its own)
- UI navbar version display (`litellm/proxy/_experimental/...` cosmetic)

## How to update this file

- When a Tier C/D PR is filed upstream: change `READY` → `OPEN #NNNNN`
- When a Tier D issue is opened: change `ISSUE-FIRST` → `ISSUE #NNNNN`, add a URL
- When upstream merges: change to `MERGED #NNNNN`, then on the next version
  bump remove the row entirely (the local carry is gone)
- New ship-branch carries to consider for upstream: add a row under the
  right Tier, link the ship PR #, write one line of notes
- Last reviewed / refreshed: keep a `_Last reviewed: YYYY-MM-DD_` note
  at the very bottom so quarterly reviews can spot stale entries

_Last reviewed: 2026-06-10 (added fix/billing-accuracy-phase-1 candidates after Phase 3 refactor landed + real-Anthropic case 33 verified)._
