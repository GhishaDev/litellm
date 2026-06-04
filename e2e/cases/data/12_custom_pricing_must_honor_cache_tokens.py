"""
Regression fixture for Case 12 — `router.py:register_model` must not
write a deployment-UUID entry that strips cache rates.

REAL PROD PATH (verified against a running e2e proxy):

  1. Proxy startup / DB sync: `router.py:7230-7237` registers each
     deployment into `litellm.model_cost` under its UUID. The dict it
     writes is `deployment.model_info.model_dump(exclude_none=True)`
     plus any `CustomPricingLiteLLMParams` keys from
     `deployment.litellm_params`. When the dashboard `/model/new` form
     was used to add the model, `litellm_params` carries only
     `input_cost_per_token` and `output_cost_per_token` — and if
     `deployment.model_info` lacks the static-map cache rates at
     register time, the UUID entry written into `litellm.model_cost`
     is permanently missing cache fields.

  2. Cost calc time: `cost_calculator._select_model_name_for_cost_calc`
     (cost_calculator.py:661-672) sees `custom_pricing=True` and
     prefers the UUID entry over the bare model name. It returns the
     UUID, and `cost_per_token` then computes against the partial
     entry — cache tokens go unbilled.

  3. Clicking "Reload Price Data" replaces `litellm.model_cost`
     wholesale (proxy_server.py:13319), which incidentally evicts the
     UUID entry. Next call resolves the bare model name and gets the
     full static-map row, so it bills correctly — until the DB-sync
     task re-registers the deployment a few minutes later.

This case asserts: a deployment registered with partial pricing must
NOT under-bill cache tokens. The fix is in `router.py:_create_deployment`
(see suggested patch in the case markdown).

Run via Case 12 runbook (docker exec). Exit non-zero when the cost
calc under-bills relative to the correct static-map total.
"""
import os
import sys

os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

import litellm
from litellm import ModelResponse
from litellm.cost_calculator import response_cost_calculator
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


# --- correct baseline: pure model_cost lookup (no UUID interference) ---
static_entry = litellm.model_cost.get(MODEL, {})
input_rate = static_entry.get("input_cost_per_token")
output_rate = static_entry.get("output_cost_per_token")
cache_read_rate = static_entry.get("cache_read_input_token_cost")
cache_create_rate = static_entry.get("cache_creation_input_token_cost")
if None in (input_rate, output_rate, cache_read_rate, cache_create_rate):
    fail(
        f"static {MODEL} entry incomplete — this case relies on the bundled "
        "JSON having full pricing for the baseline model. See case 10."
    )

non_cache_prompt = PROMPT_TOKENS - CACHE_READ - CACHE_CREATE
expected_total = (
    non_cache_prompt * input_rate
    + COMPLETION_TOKENS * output_rate
    + CACHE_READ * cache_read_rate
    + CACHE_CREATE * cache_create_rate
)

# --- simulate the broken state router.py:7237 produces -----------------
litellm.register_model({
    DEPLOYMENT_UUID: {
        "input_cost_per_token": input_rate,
        "output_cost_per_token": output_rate,
        "litellm_provider": PROVIDER,
        "mode": "chat",
        # cache_*_input_token_cost intentionally absent — exactly what the
        # dashboard /model/new form produces, and what gets register_model'd
        # if deployment.model_info doesn't have the static-map cache fields
        # merged in by the time _create_deployment runs.
    }
})

if DEPLOYMENT_UUID not in litellm.model_cost:
    fail("register_model didn't write the UUID entry — broken assumption")
if litellm.model_cost[DEPLOYMENT_UUID].get("cache_read_input_token_cost") is not None:
    fail(
        "UUID entry has cache rates already — something is auto-merging that "
        "this test was meant to detect; revisit the case design"
    )

# --- build the request shape the proxy passes to cost calc -------------
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
    choices=[{
        "index": 0,
        "message": {"role": "assistant", "content": "ok"},
        "finish_reason": "stop",
    }],
    usage=usage,
)
resp._hidden_params = {
    "custom_llm_provider": PROVIDER,
    "model_id": DEPLOYMENT_UUID,
}

# --- the actual prod-shaped call ---------------------------------------
actual_total = response_cost_calculator(
    response_object=resp,
    model=MODEL,
    custom_llm_provider=PROVIDER,
    call_type="completion",
    optional_params={},
    cache_hit=None,
    base_model=None,
    prompt="",
    custom_pricing=True,         # litellm_params has input_cost_per_token set
    router_model_id=DEPLOYMENT_UUID,
)

print(f"deployment UUID         = {DEPLOYMENT_UUID}")
print(f"static {MODEL} entry has cache rates: yes")
print(f"UUID entry has cache rates:           no  (router writes partial dict)")
print()
print(f"expected total (correct cache billing)  = ${expected_total:.6f}")
print(f"actual total via UUID path              = ${actual_total!r}")

if actual_total is None:
    fail("response_cost_calculator returned None — separate regression")

EPS = 1e-4
if abs(actual_total - expected_total) > EPS:
    print()
    print(f"FAIL: cost calc via UUID path disagrees with static-map total by "
          f"${actual_total - expected_total:+.6f} "
          f"({100*(actual_total - expected_total)/expected_total:+.1f}%)")
    print()
    print("Cause: router.py:7237 registers the deployment under its UUID "
          "with partial pricing. cost_calculator._select_model_name_for_cost_calc "
          "prefers the UUID over the bare model name, and cost_per_token then "
          "reads the partial entry — cache_*_input_token_cost are None, so "
          "the cache portion is dropped.")
    print()
    print("Fix: in router._create_deployment, when writing the UUID entry "
          "into litellm.model_cost, merge the static map's cache rate fields "
          "for the bare model name when not provided in litellm_params. See "
          "the case markdown 'Suggested fix' section.")
    sys.exit(1)

print()
print(f"PASS: UUID-path total agrees with static-map total within ${EPS}")
sys.exit(0)
