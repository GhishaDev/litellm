"""
Behavioral tests for `_get_spend_logs_metadata` propagating the new
cancellation-tracking fields (status="success_partial" path).

These fields land inside the existing `metadata` JSON column on
LiteLLM_SpendLogs — no Prisma migration required — but only if
``_get_spend_logs_metadata`` (the function the proxy actually calls to
shape a SpendLogs payload) preserves them when filtering input metadata
through ``SpendLogsMetadata.__annotations__``. That filter is the failure
mode this file guards against: forgetting to declare a new field on
SpendLogsMetadata silently drops it from every SpendLogs row.

Each test runs the real function and asserts on the real returned dict.
No Literal/Enum runtime assertions (those are mypy's job).
"""

import os
import sys

sys.path.insert(0, os.path.abspath("../../../.."))

from litellm.proxy.spend_tracking.spend_tracking_utils import _get_spend_logs_metadata


class TestCancellationFieldsInitializedFromNone:
    """When the proxy hasn't built a metadata dict yet (early failure path),
    `_get_spend_logs_metadata(None)` must still produce a SpendLogsMetadata
    with the new cancellation fields explicitly initialized to None.

    Without explicit initialization the keys would be missing from the
    TypedDict, and downstream JSON dumpers would silently skip them
    when persisting to the metadata column — breaking dashboard queries
    that filter on `metadata->>'cancel_phase'`.
    """

    def test_all_five_cancellation_fields_present_with_none(self):
        meta = _get_spend_logs_metadata(metadata=None)
        # These keys MUST exist (even as None) so JSON serializers don't
        # silently drop them. If a key is missing, json.dumps emits the
        # dict without it, and reconciliation queries return NULL —
        # indistinguishable from "field was never populated by the
        # cancel-billing path" vs "schema doesn't know about it".
        for key in (
            "cancellation_indicator",
            "cancel_phase",
            "bytes_delivered_to_client",
            "upstream_completed",
            "usage_source",
        ):
            assert key in meta, (
                f"_get_spend_logs_metadata(None) lost the '{key}' key. "
                f"This typically means the field was added to "
                f"SpendLogsMetadata.__annotations__ but the None-branch "
                f"of _get_spend_logs_metadata wasn't updated. Both must "
                f"agree or new fields go silent in SpendLogs."
            )
            assert meta[key] is None


class TestCancellationFieldsPropagateFromInputMetadata:
    """
    The hot path: proxy builds a metadata dict containing the cancellation
    fields (set by litellm_core_utils/cancel_finalize.py), then calls
    `_get_spend_logs_metadata(metadata)`. The fields must flow through
    the SpendLogsMetadata.__annotations__-keyed filter into the output
    dict that becomes the SpendLogs row's metadata column.

    Regression risk: someone adds a field to the docstring / Literal but
    forgets to declare it on SpendLogsMetadata. The filter would silently
    drop it — these tests catch that by asserting on the output.
    """

    def test_streaming_partial_cancel_fields_propagate(self):
        input_meta = {
            "cancellation_indicator": "client_disconnect",
            "cancel_phase": "streaming_partial",
            "bytes_delivered_to_client": 4096,
            "upstream_completed": False,
            "usage_source": "tokenizer_estimate",
            # Mix in an unrelated existing field to verify we didn't
            # accidentally clobber other propagation
            "requester_ip_address": "10.0.0.1",
        }
        out = _get_spend_logs_metadata(metadata=input_meta)

        assert out["cancellation_indicator"] == "client_disconnect"
        assert out["cancel_phase"] == "streaming_partial"
        assert out["bytes_delivered_to_client"] == 4096
        assert out["upstream_completed"] is False
        assert out["usage_source"] == "tokenizer_estimate"
        # Sanity: pre-existing fields still flow
        assert out["requester_ip_address"] == "10.0.0.1"

    def test_non_stream_shield_success_fields_propagate(self):
        """Different value combination — the non-stream cancel path
        records upstream_completed=True and a different usage_source."""
        input_meta = {
            "cancellation_indicator": "client_disconnect",
            "cancel_phase": "during_upstream",
            "bytes_delivered_to_client": 0,
            "upstream_completed": True,
            "usage_source": "upstream_completed_after_cancel",
        }
        out = _get_spend_logs_metadata(metadata=input_meta)

        assert out["upstream_completed"] is True
        assert out["usage_source"] == "upstream_completed_after_cancel"
        assert out["bytes_delivered_to_client"] == 0
        assert out["cancel_phase"] == "during_upstream"

    def test_partial_input_only_some_fields_set(self):
        """Realistic scenario: cancel fires before LiteLLM has finished
        gathering all the metadata. Set fields propagate, unset stay None."""
        input_meta = {
            "cancellation_indicator": "client_disconnect",
            "cancel_phase": "before_upstream",
            # No bytes_delivered_to_client, no upstream_completed,
            # no usage_source — cancel was too early to know any of these.
        }
        out = _get_spend_logs_metadata(metadata=input_meta)

        assert out["cancellation_indicator"] == "client_disconnect"
        assert out["cancel_phase"] == "before_upstream"
        # The filter returns metadata.get(key) for declared keys, so
        # missing input keys come out as None. This is the contract that
        # downstream dashboards rely on.
        assert out["bytes_delivered_to_client"] is None
        assert out["upstream_completed"] is None
        assert out["usage_source"] is None


class TestNormalSuccessUnaffected:
    """A successful (non-cancel) request must not gain spurious
    cancellation fields with non-None values. The new fields should be
    absent / None for normal requests so downstream filters like
    `WHERE metadata->>'cancel_phase' IS NULL` correctly identify
    non-cancel traffic."""

    def test_normal_request_metadata_has_no_cancel_markers(self):
        input_meta = {
            "requester_ip_address": "10.0.0.1",
            "user_api_key": "sk-test",
            # No cancellation fields — normal happy path
        }
        out = _get_spend_logs_metadata(metadata=input_meta)
        assert out["cancellation_indicator"] is None
        assert out["cancel_phase"] is None
        assert out["bytes_delivered_to_client"] is None
        assert out["upstream_completed"] is None
        assert out["usage_source"] is None
