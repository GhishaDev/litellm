"""
Tests for litellm/litellm_core_utils/cancel_billing.py — the cost
computation half of the "modified strategy 5'" cancellation handling.

Discipline (per CLAUDE.md):
* No mocking of ``cost_per_token`` or ``token_counter`` — we exercise
  the real LiteLLM pricing pipeline so a model-cost-map regression
  here is caught by these tests.
* The metadata-bridging helper is tested by inspecting the dict it
  actually mutates, not by mocking and asserting ``.called``.
"""

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath("../../.."))

from litellm.litellm_core_utils.cancel_billing import (
    compute_prompt_only_cost,
    enrich_request_metadata_with_cancel_markers,
)

# ---------------------------------------------------------------------------
# compute_prompt_only_cost — real model cost map, real tokenizer
# ---------------------------------------------------------------------------


class TestComputePromptOnlyCost:
    def test_real_anthropic_pricing_for_known_messages(self):
        """
        Real test: feed a known message into the real pricing path. The
        cost MUST be > 0 (would be 0.0 only if the model cost map was
        missing OR our function returned the zero-fallback). This
        catches "cancel_billing.py returns 0.0 for everything" bugs.
        """
        messages = [{"role": "user", "content": "Hello, how are you today?"}]
        cost = compute_prompt_only_cost(
            messages=messages, model="anthropic/claude-sonnet-4-5"
        )
        # Real Anthropic Sonnet pricing × ~10-20 prompt tokens. Won't
        # be huge ($0.001 ballpark) but MUST exceed zero.
        assert cost > 0.0, (
            f"compute_prompt_only_cost returned {cost} for a normal Anthropic "
            f"prompt — likely the cost map lookup failed silently"
        )
        # Sanity upper bound — Anthropic Sonnet is ~$3/1M input tokens,
        # 20 tokens × $3/1M = $0.00006. A cost > $0.01 here would
        # mean we accidentally multiplied or used the wrong rate.
        assert cost < 0.01, (
            f"compute_prompt_only_cost = {cost} for ~10-token prompt is "
            f"absurdly large; pricing logic likely wrong"
        )

    def test_real_openai_pricing_path(self):
        """Exercise the OpenAI cost lookup path too — different code path
        in cost_per_token, would mask provider-specific regressions."""
        messages = [{"role": "user", "content": "What is 2+2?"}]
        cost = compute_prompt_only_cost(messages=messages, model="openai/gpt-4o-mini")
        assert cost > 0.0
        assert cost < 0.01

    def test_unknown_model_returns_zero_not_raise(self):
        """Pricing failure must degrade gracefully — failure hook can't
        afford an exception."""
        messages = [{"role": "user", "content": "Hello"}]
        cost = compute_prompt_only_cost(
            messages=messages, model="nonexistent-provider/nonexistent-model-xyz"
        )
        assert cost == 0.0

    def test_no_messages_returns_zero(self):
        assert (
            compute_prompt_only_cost(messages=None, model="anthropic/claude-sonnet-4-5")
            == 0.0
        )
        assert (
            compute_prompt_only_cost(messages=[], model="anthropic/claude-sonnet-4-5")
            == 0.0
        )

    def test_no_model_returns_zero(self):
        messages = [{"role": "user", "content": "Hello"}]
        assert compute_prompt_only_cost(messages=messages, model=None) == 0.0
        assert compute_prompt_only_cost(messages=messages, model="") == 0.0

    def test_long_prompt_scales_with_token_count(self):
        """Cost should scale with prompt length — sanity check that
        we're actually using the token count, not a constant."""
        short = [{"role": "user", "content": "Hi"}]
        long = [
            {
                "role": "user",
                "content": "Hello " * 500,  # ~500 tokens
            }
        ]
        cost_short = compute_prompt_only_cost(
            messages=short, model="anthropic/claude-sonnet-4-5"
        )
        cost_long = compute_prompt_only_cost(
            messages=long, model="anthropic/claude-sonnet-4-5"
        )
        # Long prompt should cost notably more (≥ 10x for 500 vs 1 token)
        assert cost_long > cost_short * 5, (
            f"Long-prompt cost ({cost_long}) not meaningfully larger than "
            f"short-prompt cost ({cost_short}) — token count probably ignored"
        )


# ---------------------------------------------------------------------------
# enrich_request_metadata_with_cancel_markers — real dict mutation
# ---------------------------------------------------------------------------


def _make_logging_obj_with_cancel_markers(**markers):
    """Build a real Logging-shaped object whose model_call_details
    contains the cancel markers we want to bridge."""
    obj = SimpleNamespace()
    obj.model_call_details = dict(markers)
    return obj


class TestEnrichRequestMetadataWithCancelMarkers:
    def test_no_cancel_marker_no_op(self):
        """Normal (non-cancelled) request: helper must not touch
        request_data. The cancel-specific fields would otherwise pollute
        every SpendLogs row."""
        request_data = {"litellm_params": {"metadata": {"existing": "value"}}}
        logging_obj = _make_logging_obj_with_cancel_markers()  # no markers
        enrich_request_metadata_with_cancel_markers(request_data, logging_obj)
        # Unchanged
        assert request_data["litellm_params"]["metadata"] == {"existing": "value"}

    def test_streaming_partial_markers_copied_into_metadata(self):
        request_data = {"litellm_params": {"metadata": {"existing": "value"}}}
        logging_obj = _make_logging_obj_with_cancel_markers(
            cancellation_indicator="client_disconnect",
            cancel_phase="streaming_partial",
            bytes_delivered_to_client=4096,
            upstream_completed=False,
            usage_source="tokenizer_estimate",
        )
        enrich_request_metadata_with_cancel_markers(request_data, logging_obj)

        meta = request_data["litellm_params"]["metadata"]
        assert meta["cancellation_indicator"] == "client_disconnect"
        assert meta["cancel_phase"] == "streaming_partial"
        assert meta["bytes_delivered_to_client"] == 4096
        assert meta["upstream_completed"] is False
        assert meta["usage_source"] == "tokenizer_estimate"
        # status overridden to success_partial
        assert meta["status"] == "success_partial"
        # Pre-existing fields preserved
        assert meta["existing"] == "value"

    def test_creates_metadata_dict_when_missing(self):
        """Should handle request_data that doesn't have litellm_params
        or metadata yet (e.g. cancel fired before pre_call ran)."""
        request_data = {}  # empty
        logging_obj = _make_logging_obj_with_cancel_markers(
            cancellation_indicator="client_disconnect",
            cancel_phase="before_upstream",
        )
        enrich_request_metadata_with_cancel_markers(request_data, logging_obj)

        meta = request_data["litellm_params"]["metadata"]
        assert meta["cancellation_indicator"] == "client_disconnect"
        assert meta["cancel_phase"] == "before_upstream"
        assert meta["status"] == "success_partial"

    def test_none_logging_obj_no_op(self):
        request_data = {"litellm_params": {"metadata": {}}}
        # Must not raise
        enrich_request_metadata_with_cancel_markers(request_data, None)
        assert request_data == {"litellm_params": {"metadata": {}}}

    def test_partial_markers_only_those_present_propagate(self):
        """Realistic: only some markers set (e.g. cancel fired before
        we knew bytes_delivered_to_client). Don't insert keys for
        markers that weren't on the logging object."""
        request_data = {"litellm_params": {"metadata": {}}}
        logging_obj = _make_logging_obj_with_cancel_markers(
            cancellation_indicator="client_disconnect",
            cancel_phase="streaming_partial",
            # bytes_delivered_to_client, upstream_completed, usage_source NOT set
        )
        enrich_request_metadata_with_cancel_markers(request_data, logging_obj)

        meta = request_data["litellm_params"]["metadata"]
        assert "cancellation_indicator" in meta
        assert "cancel_phase" in meta
        # These should NOT be present (not on source)
        assert "bytes_delivered_to_client" not in meta
        assert "upstream_completed" not in meta
        assert "usage_source" not in meta

    def test_overwrites_stale_status(self):
        """If something else previously set status='success' or 'failure',
        the cancel path must overwrite to 'success_partial'."""
        for prior_status in ("success", "failure", None):
            request_data = {
                "litellm_params": {
                    "metadata": ({"status": prior_status} if prior_status else {})
                }
            }
            logging_obj = _make_logging_obj_with_cancel_markers(
                cancellation_indicator="client_disconnect",
                cancel_phase="streaming_partial",
            )
            enrich_request_metadata_with_cancel_markers(request_data, logging_obj)
            assert (
                request_data["litellm_params"]["metadata"]["status"]
                == "success_partial"
            )
