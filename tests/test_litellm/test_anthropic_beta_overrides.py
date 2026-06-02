"""
Unit tests for the per-deployment ``anthropic_beta_overrides`` overlay.

Two surfaces are covered:

1. The provider-aware path (``filter_and_transform_beta_headers``,
   ``update_headers_with_filtered_beta``, ``update_request_with_filtered_beta``)
   used by chat / Bedrock invoke routes.

2. The surgical-only path (``apply_overrides_to_anthropic_beta_header``)
   used by the experimental Anthropic /v1/messages passthrough, which
   must never drop a beta solely because the provider table doesn't list
   it.

The override map shape is ``Dict[str, Optional[str]]``:

- value ``None`` or ``""`` -> suppress that beta
- value non-empty string -> rewrite to that string
- key absent from the map -> existing behavior

When a header IS in the override map, the override wins outright -- the
provider mapping table is NOT consulted as a fallback. This lets a user
force-emit a beta that the provider table marks as null/unsupported.
"""

import pytest

from litellm.anthropic_beta_headers_manager import (
    apply_overrides_to_anthropic_beta_header,
    filter_and_transform_beta_headers,
    update_headers_with_filtered_beta,
    update_request_with_filtered_beta,
)


# ---------------------------------------------------------------------------
# filter_and_transform_beta_headers
# ---------------------------------------------------------------------------


class TestFilterAndTransformWithOverrides:
    """Provider-aware filter, with the ``overrides`` overlay parameter."""

    def test_no_overrides_behaves_as_before(self):
        # baseline: anthropic provider table is identity for this header
        out = filter_and_transform_beta_headers(
            ["advanced-tool-use-2025-11-20"], "anthropic"
        )
        assert out == ["advanced-tool-use-2025-11-20"]

    def test_override_rewrites_for_anthropic_provider(self):
        out = filter_and_transform_beta_headers(
            ["advanced-tool-use-2025-11-20"],
            "anthropic",
            overrides={
                "advanced-tool-use-2025-11-20": "tool-search-tool-2025-10-19",
            },
        )
        assert out == ["tool-search-tool-2025-10-19"]

    def test_override_none_suppresses(self):
        out = filter_and_transform_beta_headers(
            ["advanced-tool-use-2025-11-20", "mcp-client-2025-04-04"],
            "anthropic",
            overrides={"advanced-tool-use-2025-11-20": None},
        )
        # advanced-tool-use suppressed; mcp-client passes through provider table
        assert out == ["mcp-client-2025-04-04"]

    def test_override_empty_string_suppresses(self):
        out = filter_and_transform_beta_headers(
            ["advanced-tool-use-2025-11-20"],
            "anthropic",
            overrides={"advanced-tool-use-2025-11-20": ""},
        )
        assert out == []

    def test_override_only_affects_listed_headers(self):
        out = filter_and_transform_beta_headers(
            ["advanced-tool-use-2025-11-20", "mcp-client-2025-04-04"],
            "anthropic",
            overrides={
                "advanced-tool-use-2025-11-20": "tool-search-tool-2025-10-19",
                # mcp-client-2025-04-04 not in overrides -> provider table
            },
        )
        assert out == sorted(["tool-search-tool-2025-10-19", "mcp-client-2025-04-04"])

    def test_override_wins_when_provider_table_would_drop(self):
        # On the `bedrock_converse` provider, tool-search-tool-2025-10-19 is
        # mapped to null (dropped). An override pointing at the same string
        # should still emit it -- the override sidesteps the provider table.
        out = filter_and_transform_beta_headers(
            ["advanced-tool-use-2025-11-20"],
            "bedrock_converse",
            overrides={
                "advanced-tool-use-2025-11-20": "tool-search-tool-2025-10-19",
            },
        )
        assert out == ["tool-search-tool-2025-10-19"]

    def test_override_wins_when_provider_table_would_rewrite_differently(self):
        # bedrock provider already maps advanced-tool-use -> tool-search-tool.
        # An override to a different string must win.
        out = filter_and_transform_beta_headers(
            ["advanced-tool-use-2025-11-20"],
            "bedrock",
            overrides={"advanced-tool-use-2025-11-20": "made-up-flag-9999-99-99"},
        )
        assert out == ["made-up-flag-9999-99-99"]

    def test_unknown_header_not_in_overrides_still_dropped(self):
        # absent from both overrides and the provider table -> dropped
        out = filter_and_transform_beta_headers(
            ["totally-unknown-xyz-9999-99-99"],
            "anthropic",
            overrides={"advanced-tool-use-2025-11-20": None},
        )
        assert out == []

    def test_empty_overrides_map_is_no_op(self):
        out_no = filter_and_transform_beta_headers(
            ["advanced-tool-use-2025-11-20"], "anthropic"
        )
        out_empty = filter_and_transform_beta_headers(
            ["advanced-tool-use-2025-11-20"], "anthropic", overrides={}
        )
        assert out_no == out_empty

    def test_overrides_none_is_no_op(self):
        out_no = filter_and_transform_beta_headers(
            ["advanced-tool-use-2025-11-20"], "anthropic"
        )
        out_none = filter_and_transform_beta_headers(
            ["advanced-tool-use-2025-11-20"], "anthropic", overrides=None
        )
        assert out_no == out_none

    def test_chain_resolution_is_single_level_only(self):
        # {a: b, b: c} must NOT chain to a -> c. a maps to b verbatim and
        # the lookup stops there (the override-wins rule prevents falling
        # back into the table for b).
        out = filter_and_transform_beta_headers(
            ["advanced-tool-use-2025-11-20"],
            "anthropic",
            overrides={
                "advanced-tool-use-2025-11-20": "mcp-client-2025-04-04",
                "mcp-client-2025-04-04": "something-else",
            },
        )
        assert out == ["mcp-client-2025-04-04"]


# ---------------------------------------------------------------------------
# update_headers_with_filtered_beta (HTTP header surface)
# ---------------------------------------------------------------------------


class TestUpdateHeadersWithFilteredBetaOverrides:
    def test_rewrite_via_http_header(self):
        headers = {"anthropic-beta": "advanced-tool-use-2025-11-20"}
        out = update_headers_with_filtered_beta(
            headers=headers,
            provider="anthropic",
            overrides={
                "advanced-tool-use-2025-11-20": "tool-search-tool-2025-10-19",
            },
        )
        assert out["anthropic-beta"] == "tool-search-tool-2025-10-19"

    def test_suppress_removes_header_entirely_when_only_value(self):
        headers = {"anthropic-beta": "advanced-tool-use-2025-11-20"}
        out = update_headers_with_filtered_beta(
            headers=headers,
            provider="anthropic",
            overrides={"advanced-tool-use-2025-11-20": None},
        )
        assert "anthropic-beta" not in out

    def test_suppress_one_of_many_keeps_others(self):
        headers = {
            "anthropic-beta": "advanced-tool-use-2025-11-20,mcp-client-2025-04-04"
        }
        out = update_headers_with_filtered_beta(
            headers=headers,
            provider="anthropic",
            overrides={"advanced-tool-use-2025-11-20": None},
        )
        assert out["anthropic-beta"] == "mcp-client-2025-04-04"


# ---------------------------------------------------------------------------
# update_request_with_filtered_beta (body-level anthropic_beta array, Bedrock)
# ---------------------------------------------------------------------------


class TestUpdateRequestWithFilteredBetaOverrides:
    def test_overrides_applied_to_body_array(self):
        headers = {}
        body = {"anthropic_beta": ["advanced-tool-use-2025-11-20"]}
        headers, body = update_request_with_filtered_beta(
            headers=headers,
            request_data=body,
            provider="bedrock",
            overrides={
                "advanced-tool-use-2025-11-20": "tool-search-tool-2025-10-19",
            },
        )
        assert body["anthropic_beta"] == ["tool-search-tool-2025-10-19"]

    def test_overrides_applied_to_both_header_and_body(self):
        headers = {"anthropic-beta": "advanced-tool-use-2025-11-20"}
        body = {"anthropic_beta": ["advanced-tool-use-2025-11-20"]}
        headers, body = update_request_with_filtered_beta(
            headers=headers,
            request_data=body,
            provider="anthropic",
            overrides={"advanced-tool-use-2025-11-20": None},
        )
        assert "anthropic-beta" not in headers
        assert "anthropic_beta" not in body


# ---------------------------------------------------------------------------
# apply_overrides_to_anthropic_beta_header (surgical-only)
# ---------------------------------------------------------------------------


class TestApplyOverridesSurgical:
    """The passthrough helper -- never consults the provider mapping."""

    def test_no_op_when_overrides_empty(self):
        headers = {"anthropic-beta": "advanced-tool-use-2025-11-20"}
        out = apply_overrides_to_anthropic_beta_header(headers, {})
        assert out["anthropic-beta"] == "advanced-tool-use-2025-11-20"

    def test_no_op_when_overrides_none(self):
        headers = {"anthropic-beta": "advanced-tool-use-2025-11-20"}
        out = apply_overrides_to_anthropic_beta_header(headers, None)
        assert out["anthropic-beta"] == "advanced-tool-use-2025-11-20"

    def test_no_op_when_no_anthropic_beta_header(self):
        headers = {"content-type": "application/json"}
        out = apply_overrides_to_anthropic_beta_header(
            headers, {"advanced-tool-use-2025-11-20": "x"}
        )
        assert "anthropic-beta" not in out

    def test_does_not_drop_betas_absent_from_provider_table(self):
        # KEY DIFFERENTIATOR vs update_headers_with_filtered_beta. The
        # surgical helper must preserve unknown-to-provider betas as long
        # as the override map doesn't list them.
        headers = {
            "anthropic-beta": "totally-fictional-flag-9999-99-99,advanced-tool-use-2025-11-20"
        }
        out = apply_overrides_to_anthropic_beta_header(
            headers,
            {"advanced-tool-use-2025-11-20": "tool-search-tool-2025-10-19"},
        )
        emitted = set(out["anthropic-beta"].split(","))
        assert emitted == {
            "totally-fictional-flag-9999-99-99",
            "tool-search-tool-2025-10-19",
        }

    def test_suppress_removes_only_matching_entry(self):
        headers = {
            "anthropic-beta": "advanced-tool-use-2025-11-20,mcp-client-2025-04-04"
        }
        out = apply_overrides_to_anthropic_beta_header(
            headers, {"advanced-tool-use-2025-11-20": None}
        )
        assert out["anthropic-beta"] == "mcp-client-2025-04-04"

    def test_suppress_all_removes_header(self):
        headers = {"anthropic-beta": "advanced-tool-use-2025-11-20"}
        out = apply_overrides_to_anthropic_beta_header(
            headers, {"advanced-tool-use-2025-11-20": ""}
        )
        assert "anthropic-beta" not in out

    def test_rewrite_dedups_when_target_already_present(self):
        # Rewriting A -> B when B is also already in the header must not
        # produce a duplicate.
        headers = {
            "anthropic-beta": "advanced-tool-use-2025-11-20,tool-search-tool-2025-10-19"
        }
        out = apply_overrides_to_anthropic_beta_header(
            headers,
            {"advanced-tool-use-2025-11-20": "tool-search-tool-2025-10-19"},
        )
        emitted = out["anthropic-beta"].split(",")
        assert emitted == ["tool-search-tool-2025-10-19"]

    def test_handles_whitespace_in_header(self):
        headers = {
            "anthropic-beta": " advanced-tool-use-2025-11-20 , mcp-client-2025-04-04 "
        }
        out = apply_overrides_to_anthropic_beta_header(
            headers,
            {"advanced-tool-use-2025-11-20": "tool-search-tool-2025-10-19"},
        )
        emitted = set(out["anthropic-beta"].split(","))
        assert emitted == {
            "tool-search-tool-2025-10-19",
            "mcp-client-2025-04-04",
        }
