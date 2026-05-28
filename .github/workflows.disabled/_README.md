# Disabled workflows

GitHub Actions only loads workflows from `.github/workflows/`. Files in this
sibling directory **do not execute**. This is the convention this fork uses
to disable upstream-inherited workflows without deleting them, so they remain
available for diff/reference against upstream and can be re-enabled by moving
back to `.github/workflows/` if needed.

## Why disabled

The fork ships internal-only changes against a pinned upstream tag
(`v1.83.10-stable`) and runs **only release workflows** in CI:

| Active workflow | Trigger | Purpose |
|---|---|---|
| `release-docker.yml` | tag push `v*-internal.*` | Build + publish multi-arch Docker image to Docker Hub |
| `release-swr.yml` | tag push `v*-internal.*` / `v*-ghisha.*`, workflow_dispatch | Mirror image to Huawei Cloud SWR |

All other workflows were inherited from upstream and one of:

- Filter to `main` / `litellm_**` branches and skip our `ship/v1.83.10` PRs
  anyway (effectively dead weight on our actions UI / billing)
- Operate on upstream concerns (auto price update, daily staging branch,
  GitHub issue automation, supply-chain scoring, docs)
- Are workflow_call helpers only consumed by the above

Rather than maintain branch-filter overrides on 40+ files (each requires
a per-file diff and a maintenance burden when rebasing from upstream),
this fork chooses to disable them wholesale. Test discipline relies on:

1. Local `make lint` + `make test-unit` before commit (mandatory per CLAUDE.md)
2. Manual `vitest run` for UI changes
3. Local e2e via `e2e/tools/proxy start` + `e2e/tools/run-all-cases`

## To re-enable a workflow

```bash
git mv .github/workflows.disabled/<workflow>.yml .github/workflows/
```

That's it — GitHub Actions picks it up on next push.

## To re-enable a class of workflows (e.g. all unit tests)

The unit test workflows filter their `pull_request.branches` to:

```yaml
- 'main'
- 'litellm_internal_staging'
- 'litellm_oss_branch'
- 'litellm_**'
```

Note that `ship/v1.83.10` does NOT match `litellm_**` (literal glob). If you
re-enable them and want them to fire on PRs targeting `ship/v1.83.10`, add
`'ship/**'` to the branches list in each workflow first.
