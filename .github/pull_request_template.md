<!--
  Internal fork PR template. See `CLAUDE.md` → "Fork tier classification".
  Pick the tier first — it determines whether this PR should target
  the ship branch or upstream BerriAI/litellm.
-->

## Tier classification

- [ ] **A** — Company-specific logic (`litellm_extras/` only)
- [ ] **B** — Internal infra / branding (CI, Dockerfile, e2e, internal navbar version)
- [ ] **C** — Universal bug fix in `litellm/` core
- [ ] **D** — Universal mechanism + company opinion in `litellm/` core

**If Tier C or D, did you try upstream first?**

- [ ] Yes — upstream PR/issue: `<link>`
- [ ] No — justification: `<reason this must land internally before / instead of upstream>`

## Conflict resolutions (if any cherry-pick conflicted)

See CLAUDE.md → "Conflict resolution discipline".

For every file you resolved a merge conflict on, list:

- file path + strategy: `manual 3-way` / `--ours` / `--theirs` / `N/A`
- For `--ours` / `--theirs`: paste `git diff <pin>..HEAD --stat -- <file>` output
- Smoke evidence: `python3 -c "import ..."` / `npm run build` exit codes

Skip this section only if the PR has no cherry-picks or no conflicts.

## Relevant issues

<!-- e.g. "Fixes #000" -->

## Pre-Submission checklist

**Please complete all items before asking a LiteLLM maintainer to review your PR**

- [ ] I have Added testing in the [`tests/test_litellm/`](https://github.com/BerriAI/litellm/tree/main/tests/test_litellm) directory, **Adding at least 1 test is a hard requirement** - [see details](https://docs.litellm.ai/docs/extras/contributing_code)
- [ ] My PR passes all unit tests on [`make test-unit`](https://docs.litellm.ai/docs/extras/contributing_code)
- [ ] My PR's scope is as isolated as possible, it only solves 1 specific problem
- [ ] I have requested a Greptile review by commenting `@greptileai` and received a **Confidence Score of at least 4/5** before requesting a maintainer review

## Delays in PR merge?

If you're seeing a delay in your PR being merged, ping the LiteLLM Team on [Slack (#pr-review)](https://join.slack.com/t/litellmossslack/shared_invite/zt-3o7nkuyfr-p_kbNJj8taRfXGgQI1~YyA).

## CI (LiteLLM team)

> **CI status guideline:**
>
> - 50-55 passing tests: main is stable with minor issues.
> - 45-49 passing tests: acceptable but needs attention
> - <= 40 passing tests: unstable; be careful with your merges and assess the risk.

- [ ] **Branch creation CI run**  
       Link:

- [ ] **CI run for the last commit**  
       Link:

- [ ] **Merge / cherry-pick CI run**  
       Links:

## Screenshots / Proof of Fix

<!-- Include screenshots, screen recordings, or log output demonstrating that your changes work as expected.
     For bug fixes: show reproduction before the fix and passing behavior after.
     For new features: show the feature working end-to-end.
     For UI changes: include before/after screenshots. -->

## Type

<!-- Select the type of Pull Request -->
<!-- Keep only the necessary ones -->

🆕 New Feature
🐛 Bug Fix
🧹 Refactoring
📖 Documentation
🚄 Infrastructure
✅ Test

## Changes
