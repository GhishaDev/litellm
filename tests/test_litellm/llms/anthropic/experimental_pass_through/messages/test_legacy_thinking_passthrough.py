"""Tests for the per-deployment ``disable_adaptive_thinking_rewrite`` flag.

The /v1/messages passthrough rewrites legacy
``thinking={"type":"enabled","budget_tokens":N}`` into the adaptive form
for Claude Sonnet/Opus 4.6 by default. This flag opts a deployment out of
that rewrite so the legacy shape is passed to upstream verbatim. See
``litellm/llms/anthropic/experimental_pass_through/messages/transformation.py``.
"""

from litellm.llms.anthropic.experimental_pass_through.messages.transformation import (
    AnthropicMessagesConfig,
)
from litellm.types.router import GenericLiteLLMParams


def _legacy_thinking():
    return {"type": "enabled", "budget_tokens": 2048}


def test_rewrite_default_behavior_unchanged_for_sonnet_4_6():
    optional_params = {"thinking": _legacy_thinking()}

    AnthropicMessagesConfig._translate_legacy_thinking_for_adaptive_model(
        model="claude-sonnet-4-6",
        optional_params=optional_params,
        litellm_params=None,
    )

    assert optional_params["thinking"] == {"type": "adaptive"}
    assert optional_params["output_config"] == {"effort": "low"}


def test_rewrite_disabled_when_litellm_params_flag_set_via_dict():
    optional_params = {"thinking": _legacy_thinking()}

    AnthropicMessagesConfig._translate_legacy_thinking_for_adaptive_model(
        model="claude-sonnet-4-6",
        optional_params=optional_params,
        litellm_params={"disable_adaptive_thinking_rewrite": True},
    )

    assert optional_params == {"thinking": _legacy_thinking()}
    assert "output_config" not in optional_params


def test_flag_no_op_on_non_adaptive_model():
    optional_params = {"thinking": _legacy_thinking()}

    AnthropicMessagesConfig._translate_legacy_thinking_for_adaptive_model(
        model="claude-sonnet-3-7",
        optional_params=optional_params,
        litellm_params={"disable_adaptive_thinking_rewrite": True},
    )

    assert optional_params == {"thinking": _legacy_thinking()}


def test_flag_reads_from_generic_litellm_params_model():
    optional_params = {"thinking": _legacy_thinking()}
    params = GenericLiteLLMParams(disable_adaptive_thinking_rewrite=True)

    AnthropicMessagesConfig._translate_legacy_thinking_for_adaptive_model(
        model="claude-opus-4-6",
        optional_params=optional_params,
        litellm_params=params,
    )

    assert optional_params == {"thinking": _legacy_thinking()}


def test_flag_default_false_does_not_skip_rewrite():
    optional_params = {"thinking": _legacy_thinking()}
    params = GenericLiteLLMParams()

    AnthropicMessagesConfig._translate_legacy_thinking_for_adaptive_model(
        model="claude-sonnet-4-6",
        optional_params=optional_params,
        litellm_params=params,
    )

    assert optional_params["thinking"] == {"type": "adaptive"}
    assert optional_params["output_config"] == {"effort": "low"}
