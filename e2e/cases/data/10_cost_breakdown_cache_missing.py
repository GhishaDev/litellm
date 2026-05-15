"""
Regression fixture for Case 10 — cost breakdown must include cache fields.

Reproduces the exact Usage shape from the user-reported bug
(claude-haiku-4-5-20251001, cache_read=99774, cache_creation=416) and
asserts that:

  1) litellm.completion_cost() returns the mathematically-correct total
  2) get_model_info() returns non-None cache rate keys
  3) response_cost_calculator() (the proxy path) returns the same total

The bug we are guarding against: when litellm.model_cost[<model>] is
missing cache rate keys at runtime (because the upstream JSON was lagging
at fetch time, or because register_model() overwrote the entry without
cache fields), get_model_info() raises inside the try/except at
litellm/cost_calculator.py:1605-1632 and cache_read_cost /
cache_creation_cost end up as None in the breakdown — silently absorbing
the entire cache portion of the bill.

Run via Case 10 runbook (docker exec); exit non-zero on regression.
"""
import os
import sys
import traceback

os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

import litellm
from litellm import Usage, ModelResponse
from litellm.cost_calculator import response_cost_calculator
from litellm.types.utils import PromptTokensDetailsWrapper

MODEL = "claude-haiku-4-5-20251001"
PROMPT_TOKENS = 100191
COMPLETION_TOKENS = 151
CACHE_READ = 99774
CACHE_CREATE = 416


def fail(msg: str) -> None:
    print(f"FAIL: {msg}")
    sys.exit(1)


# ---- 1. model_cost entry must have cache fields ---------------------
mc = litellm.model_cost.get(MODEL)
if mc is None:
    fail(f"{MODEL} missing from litellm.model_cost "
         f"(total entries: {len(litellm.model_cost)})")

required_fields = [
    "input_cost_per_token",
    "output_cost_per_token",
    "cache_read_input_token_cost",
    "cache_creation_input_token_cost",
]
for k in required_fields:
    if mc.get(k) is None:
        fail(f"model_cost[{MODEL!r}].{k} is None — "
             f"this is the exact bug we are guarding against")

input_rate = mc["input_cost_per_token"]
output_rate = mc["output_cost_per_token"]
cache_read_rate = mc["cache_read_input_token_cost"]
cache_create_rate = mc["cache_creation_input_token_cost"]

# ---- 2. get_model_info() must surface the same rates ----------------
try:
    mi = litellm.get_model_info(model=MODEL, custom_llm_provider="anthropic")
except Exception as e:
    traceback.print_exc()
    fail(f"get_model_info raised {type(e).__name__}: {e}")

for k in ["cache_read_input_token_cost", "cache_creation_input_token_cost"]:
    if mi.get(k) is None:
        fail(f"get_model_info().{k} is None — "
             f"would trigger silent None breakdown")

# ---- 3. completion_cost() math must match manual computation --------
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
    id="case10-repro",
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
    "custom_llm_provider": "anthropic",
    "additional_headers": {},
}

actual_total = litellm.completion_cost(
    completion_response=resp,
    model=MODEL,
    custom_llm_provider="anthropic",
)

non_cache_prompt = PROMPT_TOKENS - CACHE_READ - CACHE_CREATE
expected_input_cost = non_cache_prompt * input_rate
expected_output_cost = COMPLETION_TOKENS * output_rate
expected_cache_read_cost = CACHE_READ * cache_read_rate
expected_cache_create_cost = CACHE_CREATE * cache_create_rate
expected_total = (
    expected_input_cost
    + expected_output_cost
    + expected_cache_read_cost
    + expected_cache_create_cost
)

# ---- 4. proxy cost_calculator path must agree -----------------------
proxy_total = response_cost_calculator(
    response_object=resp,
    model=MODEL,
    custom_llm_provider="anthropic",
    call_type="completion",
    optional_params={},
    cache_hit=None,
    base_model=None,
    prompt="",
)

# ---- 5. Report and assert -------------------------------------------
print(f"model               = {MODEL}")
print(f"input_rate          = {input_rate}")
print(f"output_rate         = {output_rate}")
print(f"cache_read_rate     = {cache_read_rate}")
print(f"cache_create_rate   = {cache_create_rate}")
print()
print(f"expected input     ({non_cache_prompt} * {input_rate})        = {expected_input_cost}")
print(f"expected output    ({COMPLETION_TOKENS} * {output_rate})         = {expected_output_cost}")
print(f"expected cache_read({CACHE_READ} * {cache_read_rate})  = {expected_cache_read_cost}")
print(f"expected cache_crt ({CACHE_CREATE} * {cache_create_rate})        = {expected_cache_create_cost}")
print(f"expected TOTAL                                = {expected_total}")
print()
print(f"litellm.completion_cost()        = {actual_total}")
print(f"response_cost_calculator()       = {proxy_total}")

EPS = 1e-9
if abs(actual_total - expected_total) > EPS:
    fail(f"completion_cost mismatch: got {actual_total}, expected {expected_total}")
if abs(proxy_total - expected_total) > EPS:
    fail(f"response_cost_calculator mismatch: got {proxy_total}, expected {expected_total}")

# Sanity: the cache portion alone must dominate input+output, otherwise
# the test wouldn't catch the bug
cache_portion = expected_cache_read_cost + expected_cache_create_cost
non_cache_portion = expected_input_cost + expected_output_cost
if cache_portion <= non_cache_portion:
    fail(f"test geometry weakened: cache_portion ({cache_portion}) must "
         f"dominate non_cache_portion ({non_cache_portion}) for the "
         f"regression to be detectable")

print()
print(f"PASS: cache portion = {cache_portion:.6f} ({100*cache_portion/expected_total:.1f}% of total)")
print(f"PASS: all paths agree on total = {actual_total:.6f}")
