"""
Behavioral tests for `_get_spend_logs_metadata` propagating the cancel
markers AND materializing the derived ``delivery_status`` /
``billing_status`` taxonomy.

The 5 cancel markers + 2 derived dimensions land in the existing
``metadata`` JSON column on LiteLLM_SpendLogs — no Prisma migration
required — but only if ``_get_spend_logs_metadata`` (the function the
proxy actually calls to shape a SpendLogs payload) preserves them when
filtering input metadata through ``SpendLogsMetadata.__annotations__``
AND invokes ``_derive_delivery_billing_status`` to populate the derived
fields.

This file guards two failure modes:
1. Forgetting to declare a new marker on SpendLogsMetadata silently
   drops it from every SpendLogs row.
2. Forgetting to call (or correctly wire) the derivation helper means
   downstream dashboards filtering on delivery_status / billing_status
   match zero rows.

Each test runs the real function and asserts on the real returned dict.
No Literal/Enum runtime assertions (those are mypy's job).
"""

import os
import sys

sys.path.insert(0, os.path.abspath("../../../.."))

from litellm.proxy.spend_tracking.spend_tracking_utils import (
    _derive_delivery_billing_status,
    _get_spend_logs_metadata,
    _get_status_for_spend_log,
)


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


class TestDeriveDeliveryBillingStatus:
    """Direct unit coverage of ``_derive_delivery_billing_status`` — one
    test per row of the semantic mapping (see docstring inside the
    helper). These are the source-of-truth assertions; the
    ``TestMaterializeDeliveryBillingStatus`` class below verifies they
    actually land in the SpendLogs metadata output dict.
    """

    def test_normal_success_yields_full_full(self):
        assert _derive_delivery_billing_status({}) == ("full", "full")
        assert _derive_delivery_billing_status({"status": "success"}) == (
            "full",
            "full",
        )

    def test_failure_yields_none_none(self):
        assert _derive_delivery_billing_status({"status": "failure"}) == (
            "none",
            "none",
        )

    def test_streaming_partial_with_bytes_yields_partial_partial(self):
        meta = {
            "cancellation_indicator": "client_disconnect",
            "cancel_phase": "streaming_partial",
            "bytes_delivered_to_client": 4096,
            "usage_source": "tokenizer_estimate",
        }
        assert _derive_delivery_billing_status(meta) == ("partial", "partial")

    def test_shield_success_yields_none_full(self):
        meta = {
            "cancellation_indicator": "client_disconnect",
            "cancel_phase": "during_upstream",
            "bytes_delivered_to_client": 0,
            "upstream_completed": True,
            "usage_source": "upstream_completed_after_cancel",
        }
        assert _derive_delivery_billing_status(meta) == ("none", "full")

    def test_shield_timeout_yields_none_partial(self):
        meta = {
            "cancellation_indicator": "client_disconnect",
            "cancel_phase": "during_upstream",
            "upstream_completed": False,
            "usage_source": "shield_timeout",
        }
        assert _derive_delivery_billing_status(meta) == ("none", "partial")

    def test_before_upstream_cancel_yields_none_none(self):
        # Cancel fired BEFORE the proxy dispatched anything to upstream.
        # No upstream charge → spend=0 → billing=none. This is the only
        # legitimate (none, none) cancel case.
        meta = {
            "cancellation_indicator": "client_disconnect",
            "cancel_phase": "before_upstream",
            "usage_source": "no_completion",
        }
        assert _derive_delivery_billing_status(meta) == ("none", "none")

    def test_streaming_cancel_with_zero_bytes_yields_none_partial(self):
        # Cancel fired between "proxy dispatched the request to upstream"
        # and "first chunk reached the client". Upstream got the prompt
        # and started generating, so ``compute_prompt_only_cost`` in
        # ``proxy_track_cost_callback.async_post_call_failure_hook``
        # bills > 0 for known models. billing_status MUST be "partial"
        # to match — labeling this (none, none) would contradict the
        # row's own ``spend`` column.
        meta = {
            "cancellation_indicator": "client_disconnect",
            "cancel_phase": "streaming_partial",
            "bytes_delivered_to_client": 0,
        }
        assert _derive_delivery_billing_status(meta) == ("none", "partial")

    def test_cancel_during_upstream_no_completion_yields_none_partial(self):
        # Non-stream cancel routed through _fallback_to_failure_hook
        # because the shield wait gave up without recovering a usage
        # object (upstream errored or stalled). The prompt was
        # dispatched → prompt-only billing fires → billing=partial.
        # Previously the rule labeled this (none, none) — that
        # contradicted the positive spend on the actual row.
        meta = {
            "cancellation_indicator": "client_disconnect",
            "cancel_phase": "during_upstream",
            "upstream_completed": False,
            "usage_source": "no_completion",
        }
        assert _derive_delivery_billing_status(meta) == ("none", "partial")


class TestMaterializeDeliveryBillingStatus:
    """``_get_spend_logs_metadata`` must call the derivation helper and
    write the result into the returned dict, so SQL dashboards can
    filter on ``metadata::jsonb->>'delivery_status'`` directly."""

    def test_normal_success_materializes_full_full(self):
        out = _get_spend_logs_metadata(metadata={"user_api_key": "sk-test"})
        assert out["delivery_status"] == "full"
        assert out["billing_status"] == "full"

    def test_streaming_cancel_materializes_partial_partial(self):
        out = _get_spend_logs_metadata(
            metadata={
                "cancellation_indicator": "client_disconnect",
                "cancel_phase": "streaming_partial",
                "bytes_delivered_to_client": 4096,
                "usage_source": "tokenizer_estimate",
            }
        )
        assert out["delivery_status"] == "partial"
        assert out["billing_status"] == "partial"

    def test_failure_materializes_none_none(self):
        out = _get_spend_logs_metadata(metadata={"status": "failure"})
        assert out["delivery_status"] == "none"
        assert out["billing_status"] == "none"

    def test_none_metadata_branch_includes_derived_fields(self):
        # The early-failure path that passes metadata=None still must
        # include both derived keys (so JSON serializers don't silently
        # drop them and downstream dashboards see them as NULL rather
        # than missing).
        out = _get_spend_logs_metadata(metadata=None)
        assert "delivery_status" in out
        assert "billing_status" in out


class TestBinaryStatusReader:
    """``_get_status_for_spend_log`` returns only "success" or
    "failure". Cancellation taxonomy lives in metadata markers, not
    this column."""

    def test_success(self):
        assert _get_status_for_spend_log({}) == "success"
        assert _get_status_for_spend_log({"status": "success"}) == "success"

    def test_failure(self):
        assert _get_status_for_spend_log({"status": "failure"}) == "failure"

    def test_cancel_with_markers_stays_success(self):
        # The hot path under the new taxonomy: cancelled row carries
        # status="success" + cancellation_indicator marker. The reader
        # must return "success", letting the marker drive dashboard
        # filtering.
        assert (
            _get_status_for_spend_log(
                {
                    "status": "success",
                    "cancellation_indicator": "client_disconnect",
                }
            )
            == "success"
        )
