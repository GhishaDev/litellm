# LiteLLM E2E Test Harness — Claude-driven

This directory is a **toolkit + runbook library** for end-to-end testing
of the litellm proxy against real provider APIs.

**Philosophy**: Claude Code drives the test sessions. Scripts are
single-purpose Unix tools; runbooks (`cases/*.md`) describe scenarios.
No pytest, no framework lock-in. Tools also work fine when invoked by
a human.

```
e2e/
├── README.md                   ← you are here
├── .env.example                ← copy to .env, fill in keys
├── _config/
│   └── docker-compose.yml      ← builds litellm from local source + Postgres
├── tools/
│   ├── proxy                   ← lifecycle: start | stop | status | logs | rebuild | url
│   ├── call                    ← issue one chat-completions request, output JSON
│   ├── metrics                 ← /metrics: snapshot | diff | get
│   ├── keys                    ← virtual key lifecycle: new | info | delete | hash
│   └── teams                   ← team lifecycle: new | info | delete
└── cases/
    ├── README.md               ← index of test scenarios
    └── 01..07_*.md             ← runbooks Claude executes
```

## One-time setup

```bash
# 1. Provide API keys (and optionally base URLs / model overrides)
cp e2e/.env.example e2e/.env
$EDITOR e2e/.env
```

`e2e/.env` supports the following keys (all optional except API keys for
providers you intend to exercise):

| Key | Purpose | Default |
|---|---|---|
| `ANTHROPIC_API_KEY` | Anthropic credential | — (required for case 01-04) |
| `OPENAI_API_KEY` | OpenAI credential | — (required for case 05-06) |
| `ANTHROPIC_API_BASE` | Gateway / region-pinned endpoint | `https://api.anthropic.com` |
| `OPENAI_API_BASE` | Gateway / Azure / self-hosted | `https://api.openai.com/v1` |
| `MODEL_ANTHROPIC_SONNET` | Full litellm model id | `anthropic/claude-3-5-sonnet-20241022` |
| `MODEL_ANTHROPIC_HAIKU` | Full litellm model id | `anthropic/claude-3-5-haiku-20241022` |
| `MODEL_OPENAI` | Full litellm model id | `openai/gpt-4o-mini` |
| `E2E_PROXY_PORT` | Host port for the proxy | `4011` |

Each `MODEL_*` value must include the provider prefix
(`anthropic/`, `openai/`, `bedrock/`, `vertex_ai/`, `openrouter/`, ...).
This lets you point the same `model_name` slot at a non-default routing
path without touching code.

```bash
# 2. Pre-build the image (subsequent starts are instant)
e2e/tools/proxy rebuild             # ~3-5 min first time. Subsequent source-only
                                    # changes — use `proxy build` instead (30-90s).
```

The Python interpreter that runs the tools must have
`prometheus_client` available. Easiest: use the litellm dev venv
(`make install-dev` or `uv run python e2e/tools/metrics ...`).

The proxy config is **generated** from `.env` at `proxy start` time and
written to `e2e/_config/.litellm.rendered.yaml` (gitignored). Edit `.env`
+ rerun `e2e/tools/proxy restart` to pick up changes — never edit the
rendered file by hand.

### Postgres (always-on, ephemeral)

`proxy start` brings up a Postgres 16 container alongside litellm so DB-backed
features (virtual keys, teams, spend logs) work out of the box. The DB is
**ephemeral** — every `proxy stop` (or `restart`) wipes data. This keeps test
runs reproducible and prevents stale virtual keys from poisoning later cases.

If you need persistence (e.g. to attach a debugger to spend logs), edit
`e2e/_config/docker-compose.yml` and add a `volumes:` block under the `db`
service.

## Typical session

```bash
# 1. Boot proxy
e2e/tools/proxy start

# 2. Sanity smoke test (no API key needed)
# → see e2e/cases/07_prometheus_endpoint_smoke.md

# 3. Drive a real test (Claude reads the runbook and executes)
# → see e2e/cases/01_prometheus_anthropic_creation_5m.md

# 4. When done
e2e/tools/proxy stop
```

## How Claude uses this

Tell Claude:

> "Run case 01 against the running proxy and report what you see."

Claude will:
1. `cat e2e/cases/01_prometheus_anthropic_creation_5m.md`
2. Execute the Steps via the Bash tool
3. Compare actual against Expected
4. Surface diffs, judge pass/fail, debug if needed

Because Claude is the orchestrator, it can:
- Adapt mid-test (e.g. retry with a longer prompt if `cache_creation=0`)
- Cross-check provider responses against metric deltas
- Open a logs tail when something looks off
- Decide to skip cases that don't apply to your account

## Adding new cases

1. Drop a new markdown file under `cases/` following the existing
   Goal / Preconditions / Steps / Expected shape.
2. If the case uses a new metric / endpoint, the existing 3 tools may
   already cover it. Only add a new tool when the same logic is needed
   in ≥ 2 cases.
3. Update `cases/README.md` index.

## Tools reference (cheat sheet)

```bash
# Proxy
e2e/tools/proxy start                # boot (idempotent) — brings up db + litellm
e2e/tools/proxy stop                 # tear down (wipes db)
e2e/tools/proxy status               # exit 0 if ready
e2e/tools/proxy logs --tail 100 -f   # follow logs
e2e/tools/proxy build                # cached rebuild + recreate (30-90s; default for source edits)
e2e/tools/proxy rebuild              # --no-cache rebuild + recreate (~3-5 min; for Dockerfile/dep changes)
e2e/tools/proxy url                  # prints e.g. http://localhost:4011

# Run the full case suite (PASS/FAIL/SKIP summary, exit 0 if all pass)
e2e/tools/run-all-cases               # ~$0.05 in provider cost
e2e/tools/run-all-cases --skip-paid   # only free cases (10, 12)

# Make a call (full response JSON on stdout)
e2e/tools/call --provider anthropic --cache ephemeral --ttl 5m
e2e/tools/call --provider anthropic --cache none
e2e/tools/call --provider openai --prompt-tokens 1800 --seed run42
e2e/tools/call --provider anthropic --api-key sk-...  # use virtual key
e2e/tools/call --provider anthropic --user-id user-42 # sticky upstream LB
                                                       # for gateways that
                                                       # route by user_id

# Metrics
e2e/tools/metrics snapshot                              # → JSON
e2e/tools/metrics get litellm_prompt_cache_read_tokens_metric
e2e/tools/metrics get litellm_prompt_cache_read_tokens_metric \
    --label api_provider=anthropic
e2e/tools/metrics diff before.json after.json \
    --metric litellm_prompt_cache_creation_tokens_metric \
    --label cache_ttl=5m

# Virtual keys (needs DB)
e2e/tools/keys new --alias my-key --models claude-sonnet-cache --duration 30m
e2e/tools/keys hash sk-...           # print sha256 → matches `hashed_api_key` label
e2e/tools/keys delete --key sk-...

# Teams (needs DB)
e2e/tools/teams new --alias team-foo --max-budget 10
e2e/tools/teams delete --team-id <uuid>
```

## Cost discipline

These tests call real provider APIs. Per-case cost is < $0.01 with
default prompt sizes, but adds up if you `loop` recklessly. The case
runbooks are intentionally short — one or two calls each.

## Out of scope

- Load / concurrency testing (see `tests/load_tests/`)
- Per-virtual-key isolation (needs Postgres; add when needed)
- CI automation (these cost money; run on-demand only)
