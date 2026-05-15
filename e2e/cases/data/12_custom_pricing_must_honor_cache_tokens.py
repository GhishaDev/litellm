"""
Regression fixture for Case 12 — Router must backfill cost fields from
the canonical static `litellm.model_cost` entry when registering a
deployment-UUID model_cost row, so that cost calculation against the
UUID resolves to the correct total even when the user didn't supply
cache rates (which the dashboard /model/new form doesn't expose).

End-to-end flow exercised:
  1. Build a `Deployment` mimicking what the dashboard `/model/new`
     produces — `litellm_params.input_cost_per_token` /
     `output_cost_per_token` set, but no cache rate fields, and
     `litellm_params.model="claude-haiku-4-5-20251001"` (known upstream).
  2. Call `Router.add_deployment(deployment)` — the same code path
     hit by /model/new and DB-sync.
  3. Assert `litellm.model_cost[<deployment_uuid>]` has both
     `cache_read_input_token_cost` and
     `cache_creation_input_token_cost` populated (backfilled from
     the static entry for `claude-haiku-4-5-20251001`).
  4. Run `response_cost_calculator(custom_pricing=True,
     router_model_id=<uuid>)` for the user-reported Usage shape and
     assert the total equals the static-map baseline.

Before the router backfill landed, step 3 found `None` and step 4
under-billed by ~93%. With backfill, the cost calc through the
custom_pricing/UUID path returns the same total as the bare model
name path — no more "Reload Price Data" workaround required.

Run via Case 12 runbook (docker exec); exit non-zero on regression.
"""

import os
import sys

os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

import litellm
from litellm import ModelResponse
from litellm.cost_calculator import response_cost_calculator
from litellm.router import Router
from litellm.types.utils import PromptTokensDetailsWrapper, Usage

DEPLOYMENT_UUID = "case12-539b1c62-ac07-47ae-8987-29426984bb55"
MODEL = "claude-haiku-4-5-20251001"
PROVIDER = "anthropic"

PROMPT_TOKENS = 100191
COMPLETION_TOKENS = 151
CACHE_READ = 99774
CACHE_CREATE = 416


def fail(msg: str) -> None:
    print(f"FAIL: {msg}")
    sys.exit(1)


# Correct baseline from the static map
static_entry = litellm.model_cost.get(MODEL, {})
input_rate = static_entry.get("input_cost_per_token")
output_rate = static_entry.get("output_cost_per_token")
cache_read_rate = static_entry.get("cache_read_input_token_cost")
cache_create_rate = static_entry.get("cache_creation_input_token_cost")
if None in (input_rate, output_rate, cache_read_rate, cache_create_rate):
    fail(
        f"static {MODEL} entry incomplete in the bundled JSON — case 12 "
        f"relies on it for the baseline. See case 10."
    )

non_cache_prompt = PROMPT_TOKENS - CACHE_READ - CACHE_CREATE
expected_total = (
    non_cache_prompt * input_rate
    + COMPLETION_TOKENS * output_rate
    + CACHE_READ * cache_read_rate
    + CACHE_CREATE * cache_create_rate
)

# Drop any stale UUID entry left by a previous run so the test is
# reproducible. (e2e harness Postgres is ephemeral, but litellm.model_cost
# is per-process and survives across pytest runs in the same container.)
litellm.model_cost.pop(DEPLOYMENT_UUID, None)

# Build a Router with a single deployment whose litellm_params mimics the
# dashboard /model/new output — only input/output rates, no cache fields.
router = Router(
    model_list=[
        {
            "model_name": "case12-claude-haiku",
            "litellm_params": {
                "model": MODEL,
                "custom_llm_provider": PROVIDER,
                "input_cost_per_token": input_rate,
                "output_cost_per_token": output_rate,
                # cache_*_input_token_cost intentionally absent
            },
            "model_info": {
                "id": DEPLOYMENT_UUID,
            },
        }
    ]
)
del router  # the registration side-effects are what we care about

uuid_entry = litellm.model_cost.get(DEPLOYMENT_UUID, {})
print("After Router(model_list=...) registration:")
print(f"  UUID entry exists:                 {DEPLOYMENT_UUID in litellm.model_cost}")
print(
    f"  input_cost_per_token               = {uuid_entry.get('input_cost_per_token')}"
)
print(
    f"  output_cost_per_token              = {uuid_entry.get('output_cost_per_token')}"
)
print(
    f"  cache_read_input_token_cost        = {uuid_entry.get('cache_read_input_token_cost')}"
)
print(
    f"  cache_creation_input_token_cost    = {uuid_entry.get('cache_creation_input_token_cost')}"
)
print()

if uuid_entry.get("cache_read_input_token_cost") is None:
    fail(
        "Router registered a deployment-UUID model_cost entry without "
        "cache_read_input_token_cost. The backfill from canonical static "
        "entry is missing — see router.py _backfill_cost_fields_from_canonical."
    )
if uuid_entry.get("cache_creation_input_token_cost") is None:
    fail(
        "Router registered a deployment-UUID model_cost entry without "
        "cache_creation_input_token_cost. Same fix as above."
    )

# Build the cost-calc request shape — same as the prod logging path.
usage = Usage(
    prompt_tokens=PROMPT_TOKENS,
    completion_tokens=COMPLETION_TOKENS,
    total_tokens=PROMPT_TOKENS + COMPLETION_TOKENS,
    prompt_tokens_details=PromptTokensDetailsWrapper(
        cached_tokens=CACHE_READ,
        cache_creation_tokens=CACHE_CREATE,
    ),
)
usage.cache_read_input_tokens = CACHE_READ
usage.cache_creation_input_tokens = CACHE_CREATE

resp = ModelResponse(
    id="case12-repro",
    object="chat.completion",
    created=0,
    model=MODEL,
    choices=[
        {
            "index": 0,
            "message": {"role": "assistant", "content": "ok"},
            "finish_reason": "stop",
        }
    ],
    usage=usage,
)
resp._hidden_params = {
    "custom_llm_provider": PROVIDER,
    "model_id": DEPLOYMENT_UUID,
}

actual_total = response_cost_calculator(
    response_object=resp,
    model=MODEL,
    custom_llm_provider=PROVIDER,
    call_type="completion",
    optional_params={},
    cache_hit=None,
    base_model=None,
    prompt="",
    custom_pricing=True,
    router_model_id=DEPLOYMENT_UUID,
)

print(f"expected total (correct cache billing)  = ${expected_total:.6f}")
print(f"actual total via UUID path              = ${actual_total!r}")

if actual_total is None:
    fail("response_cost_calculator returned None — separate regression")

EPS = 1e-4
if abs(actual_total - expected_total) > EPS:
    diff_pct = 100 * (actual_total - expected_total) / expected_total
    fail(
        f"cost calc via UUID path disagrees with static-map total by "
        f"${actual_total - expected_total:+.6f} ({diff_pct:+.1f}%). "
        f"This means the Router registered a partial deployment-UUID "
        f"entry and the cost calc fell through to a path that ignored "
        f"cache pricing. Check router.py _backfill_cost_fields_from_canonical "
        f"and confirm it is invoked from both register sites in "
        f"_create_deployment and add_deployment."
    )

print()
print(f"PASS: UUID-path total agrees with static-map total within ${EPS}")
sys.exit(0)
