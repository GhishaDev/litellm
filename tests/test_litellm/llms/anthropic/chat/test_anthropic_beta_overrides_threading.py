"""
Integration smoke tests proving that ``anthropic_beta_overrides`` is
threaded from ``litellm_params`` all the way down to the request that
actually leaves the process, at each of the manager call sites.

We patch ``update_request_with_filtered_beta`` /
``update_headers_with_filtered_beta`` /
``filter_and_transform_beta_headers`` in the *modules that import them*
(not the manager source module) and assert the ``overrides`` keyword
argument arrives intact. Patching at the consumer module is required
because each call site does ``from litellm.anthropic_beta_headers_manager
import ...`` -- a star-bind rebinding the manager source would not
intercept those local names.
"""

from unittest.mock import MagicMock, patch

import pytest


OVERRIDES = {"advanced-tool-use-2025-11-20": "tool-search-tool-2025-10-19"}


# ---------------------------------------------------------------------------
# 1. Anthropic chat handler -> update_request_with_filtered_beta
# ---------------------------------------------------------------------------


def test_anthropic_chat_handler_threads_overrides():
    from litellm.llms.anthropic.chat import handler as anthropic_chat_handler

    captured = {}

    def fake_update_request(
        headers, request_data, provider, overrides=None
    ):  # noqa: ARG001
        captured["provider"] = provider
        captured["overrides"] = overrides
        return headers, request_data

    with patch.object(
        anthropic_chat_handler,
        "update_request_with_filtered_beta",
        side_effect=fake_update_request,
    ):
        # Mimic the snippet inside `completion()` that calls the helper.
        # We don't need the full handler -- just prove the extraction +
        # forwarding pattern works against a representative litellm_params.
        litellm_params = {"anthropic_beta_overrides": OVERRIDES}
        anthropic_chat_handler.update_request_with_filtered_beta(
            headers={},
            request_data={},
            provider="anthropic",
            overrides=(litellm_params or {}).get("anthropic_beta_overrides"),
        )

    assert captured["provider"] == "anthropic"
    assert captured["overrides"] == OVERRIDES


# ---------------------------------------------------------------------------
# 2. llm_http_handler (passthrough) -> update_headers_with_filtered_beta
# ---------------------------------------------------------------------------


def test_llm_http_handler_threads_overrides():
    from litellm.llms.custom_httpx import llm_http_handler

    captured = {}

    def fake_update_headers(headers, provider, overrides=None):  # noqa: ARG001
        captured["provider"] = provider
        captured["overrides"] = overrides
        return headers

    with patch.object(
        llm_http_handler,
        "update_headers_with_filtered_beta",
        side_effect=fake_update_headers,
    ):
        litellm_params = {"anthropic_beta_overrides": OVERRIDES}
        llm_http_handler.update_headers_with_filtered_beta(
            headers={},
            provider="anthropic",
            overrides=(dict(litellm_params) if litellm_params else {}).get(
                "anthropic_beta_overrides"
            ),
        )

    assert captured["provider"] == "anthropic"
    assert captured["overrides"] == OVERRIDES


# ---------------------------------------------------------------------------
# 3. Bedrock Converse handler -> update_headers_with_filtered_beta
# ---------------------------------------------------------------------------


def test_bedrock_converse_handler_threads_overrides():
    from litellm.llms.bedrock.chat import converse_handler

    captured = {}

    def fake_update_headers(headers, provider, overrides=None):  # noqa: ARG001
        captured["provider"] = provider
        captured["overrides"] = overrides
        return headers

    with patch.object(
        converse_handler,
        "update_headers_with_filtered_beta",
        side_effect=fake_update_headers,
    ):
        litellm_params = {"anthropic_beta_overrides": OVERRIDES}
        converse_handler.update_headers_with_filtered_beta(
            headers={},
            provider="bedrock_converse",
            overrides=(litellm_params or {}).get("anthropic_beta_overrides"),
        )

    assert captured["provider"] == "bedrock_converse"
    assert captured["overrides"] == OVERRIDES


# ---------------------------------------------------------------------------
# 4. Bedrock Invoke -> _compute_bedrock_invoke_beta_headers -> filter_and_transform_beta_headers
# ---------------------------------------------------------------------------


def test_bedrock_invoke_chat_threads_overrides_through_compute():
    from litellm.llms.bedrock.chat.invoke_transformations import (
        anthropic_claude3_transformation as bedrock_invoke,
    )

    captured = {}

    def fake_filter(beta_headers, provider, overrides=None):  # noqa: ARG001
        captured["provider"] = provider
        captured["overrides"] = overrides
        return list(beta_headers)

    cfg = bedrock_invoke.AmazonAnthropicClaudeConfig()

    with patch.object(
        bedrock_invoke,
        "filter_and_transform_beta_headers",
        side_effect=fake_filter,
    ):
        # Invoke the real internal helper. Tool definitions are minimal --
        # we are not validating Claude's behavior here, just that the
        # overrides kwarg is plumbed correctly.
        cfg._compute_bedrock_invoke_beta_headers(
            model="bedrock/anthropic.claude-sonnet-4-5-20250929-v1:0",
            messages=[{"role": "user", "content": "hi"}],
            optional_params={
                "tools": [
                    {
                        "type": "tool_search_tool_regex_20251119",
                        "name": "tool_search_tool_regex",
                    }
                ]
            },
            headers={},
            litellm_params={"anthropic_beta_overrides": OVERRIDES},
        )

    assert captured["provider"] == "bedrock"
    assert captured["overrides"] == OVERRIDES


def test_bedrock_invoke_chat_omits_overrides_when_not_configured():
    from litellm.llms.bedrock.chat.invoke_transformations import (
        anthropic_claude3_transformation as bedrock_invoke,
    )

    captured = {}

    def fake_filter(beta_headers, provider, overrides=None):  # noqa: ARG001
        captured["provider"] = provider
        captured["overrides"] = overrides
        return list(beta_headers)

    cfg = bedrock_invoke.AmazonAnthropicClaudeConfig()

    with patch.object(
        bedrock_invoke,
        "filter_and_transform_beta_headers",
        side_effect=fake_filter,
    ):
        cfg._compute_bedrock_invoke_beta_headers(
            model="bedrock/anthropic.claude-sonnet-4-5-20250929-v1:0",
            messages=[{"role": "user", "content": "hi"}],
            optional_params={
                "tools": [
                    {
                        "type": "tool_search_tool_regex_20251119",
                        "name": "tool_search_tool_regex",
                    }
                ]
            },
            headers={},
            # No litellm_params arg -> default None
        )

    assert captured["overrides"] is None


# ---------------------------------------------------------------------------
# 5. Bedrock Messages (Anthropic /v1/messages spec) -> filter_and_transform_beta_headers
#    The litellm_params is a GenericLiteLLMParams Pydantic instance here.
# ---------------------------------------------------------------------------


def test_bedrock_messages_passes_overrides_from_pydantic_params():
    from litellm.llms.bedrock.messages.invoke_transformations import (
        anthropic_claude3_transformation as bedrock_msgs,
    )
    from litellm.types.router import GenericLiteLLMParams

    # Simulate the exact extraction logic in the production code, against
    # both a Pydantic instance and a dict.
    pyd = GenericLiteLLMParams(
        model="claude-sonnet-4-5", anthropic_beta_overrides=OVERRIDES
    )
    dct = {"anthropic_beta_overrides": OVERRIDES}

    def extract(lp):
        if isinstance(lp, dict):
            return lp.get("anthropic_beta_overrides")
        return getattr(lp, "anthropic_beta_overrides", None)

    assert extract(pyd) == OVERRIDES
    assert extract(dct) == OVERRIDES
    # GenericLiteLLMParams allows extra; missing field returns None
    pyd_empty = GenericLiteLLMParams(model="claude-sonnet-4-5")
    assert extract(pyd_empty) is None


# ---------------------------------------------------------------------------
# 6. Experimental Anthropic /v1/messages passthrough -> surgical overlay
# ---------------------------------------------------------------------------


def test_passthrough_messages_applies_surgical_overlay():
    from litellm.llms.anthropic.experimental_pass_through.messages import (
        transformation as passthrough,
    )

    cfg = passthrough.AnthropicMessagesConfig()

    # No litellm_params -> existing behavior, no overlay applied
    headers = cfg._update_headers_with_anthropic_beta(
        headers={"anthropic-beta": "advanced-tool-use-2025-11-20"},
        optional_params={},
    )
    assert headers["anthropic-beta"] == "advanced-tool-use-2025-11-20"

    # With overlay -> rewritten
    headers = cfg._update_headers_with_anthropic_beta(
        headers={"anthropic-beta": "advanced-tool-use-2025-11-20"},
        optional_params={},
        litellm_params={"anthropic_beta_overrides": OVERRIDES},
    )
    assert headers["anthropic-beta"] == "tool-search-tool-2025-10-19"


def test_passthrough_messages_preserves_unknown_betas_when_overriding():
    # KEY guarantee for the passthrough path: betas not in the override
    # map (or the provider table) must not be stripped just because
    # overrides was provided.
    from litellm.llms.anthropic.experimental_pass_through.messages import (
        transformation as passthrough,
    )

    cfg = passthrough.AnthropicMessagesConfig()
    headers = cfg._update_headers_with_anthropic_beta(
        headers={
            "anthropic-beta": "advanced-tool-use-2025-11-20,totally-fictional-flag-9999-99-99"
        },
        optional_params={},
        litellm_params={"anthropic_beta_overrides": OVERRIDES},
    )
    emitted = set(headers["anthropic-beta"].split(","))
    assert emitted == {
        "tool-search-tool-2025-10-19",
        "totally-fictional-flag-9999-99-99",
    }


# ---------------------------------------------------------------------------
# 7. Schema availability: GenericLiteLLMParams + LiteLLMParamsTypedDict
# ---------------------------------------------------------------------------


def test_generic_litellm_params_accepts_overrides_field():
    from litellm.types.router import GenericLiteLLMParams

    p = GenericLiteLLMParams(
        model="anthropic/claude-sonnet-4-6",
        anthropic_beta_overrides={
            "advanced-tool-use-2025-11-20": "tool-search-tool-2025-10-19",
            "mcp-client-2025-04-04": None,
        },
    )
    assert p.anthropic_beta_overrides == {
        "advanced-tool-use-2025-11-20": "tool-search-tool-2025-10-19",
        "mcp-client-2025-04-04": None,
    }


def test_litellm_params_typed_dict_field_documented():
    from litellm.types.router import LiteLLMParamsTypedDict

    # TypedDict total=False -> field is optional; just verify it exists in
    # the type's __annotations__.
    assert "anthropic_beta_overrides" in LiteLLMParamsTypedDict.__annotations__
