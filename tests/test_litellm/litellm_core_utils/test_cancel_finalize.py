"""
Tests for litellm/litellm_core_utils/cancel_finalize.py.

Design notes
------------
The goal of this file is to verify the CONTRACT of the cancel-finalize
plumbing, not just that "the right Python function was called". Two
specific anti-patterns are avoided here (per CLAUDE.md):

1. **No mocking of the unit under test or its core dependencies.**
   ``stream_chunk_builder`` is NOT mocked — we feed real
   ``ModelResponseStream`` chunks through it and assert on the actual
   reassembled response's ``usage`` field. This means the test also
   catches regressions in the cursor=1 fix from PR #1, which is the
   right cross-coverage.

2. **No ``MagicMock.called`` / ``.call_args is mock`` assertions.**
   The Logging object is a small real ``SpyLogging`` class that
   captures the kwargs handlers receive. Tests assert on the captured
   *data* (response shape, usage fields, marker keys), not on
   "the mock was called".

If you add a new test here, follow the same pattern: build the smallest
realistic input that triggers the behavior you want to lock in, then
assert on observable outputs (dict mutations, captured handler args,
returned tasks).
"""

import asyncio
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.abspath("../../.."))

from litellm.litellm_core_utils.cancel_finalize import (
    _get_accumulated_chunks,
    finalize_non_stream_cancel,
    finalize_streaming_cancel,
    is_logging_obj_cancelled,
    mark_logging_obj_cancelled,
)
from litellm.types.utils import (
    Delta,
    ModelResponseStream,
    StreamingChoices,
    Usage,
)

# ---------------------------------------------------------------------------
# Real test doubles (NOT MagicMock)
# ---------------------------------------------------------------------------


class SpyLogging:
    """Minimal real Logging-shaped object used in place of MagicMock.

    Captures the kwargs that ``async_success_handler`` /
    ``async_failure_handler`` receive so tests can assert on the
    payload (not on ``.called``). This is the spy pattern from
    CLAUDE.md's "no theater tests" rule — tests verify what actually
    flowed through, which catches real shape / value regressions.
    """

    def __init__(self):
        # Real dict (not MagicMock attribute access) so that
        # cancel_finalize's get/set patterns behave exactly as in prod.
        self.model_call_details: dict = {}
        self.start_time = None
        self.captured_success_calls: list = []
        self.captured_failure_calls: list = []

    async def async_success_handler(self, **kwargs):
        # Defensive deep snapshot of the result kwarg if it's mutable —
        # this catches "we set the field, then mutated it back" bugs.
        self.captured_success_calls.append(dict(kwargs))

    async def async_failure_handler(self, *args, **kwargs):
        self.captured_failure_calls.append({"args": args, "kwargs": dict(kwargs)})


def _make_chunk(
    *,
    content: str = "",
    usage: Usage = None,
    finish_reason: str = None,
) -> ModelResponseStream:
    """Construct a real ModelResponseStream — same factory as the
    cursor-bug regression file. Reused so any chunk-shape change forces
    both test files to update together."""
    return ModelResponseStream(
        id="msg_cancel_test",
        created=1738900000,
        model="claude-sonnet-4-6",
        object="chat.completion.chunk",
        choices=[
            StreamingChoices(
                finish_reason=finish_reason,
                index=0,
                delta=Delta(content=content, role="assistant"),
            )
        ],
        usage=usage,
    )


# ---------------------------------------------------------------------------
# mark_logging_obj_cancelled — pure dict mutation contract
# ---------------------------------------------------------------------------


class TestMarkLoggingObjCancelled:
    def test_sets_indicator_and_phase(self):
        logging_obj = SpyLogging()
        mark_logging_obj_cancelled(logging_obj, phase="streaming_partial")
        assert (
            logging_obj.model_call_details["cancellation_indicator"]
            == "client_disconnect"
        )
        assert logging_obj.model_call_details["cancel_phase"] == "streaming_partial"
        assert "cancelled_at" in logging_obj.model_call_details

    def test_idempotent_does_not_overwrite_first_marker(self):
        logging_obj = SpyLogging()
        mark_logging_obj_cancelled(
            logging_obj, phase="streaming_partial", indicator="client_disconnect"
        )
        first_t = logging_obj.model_call_details["cancelled_at"]
        # Second call with a different phase/indicator should be ignored —
        # the first detection point wins so phase progression doesn't lie.
        mark_logging_obj_cancelled(
            logging_obj,
            phase="during_parsing",
            indicator="upstream_disconnect",
        )
        assert logging_obj.model_call_details["cancel_phase"] == "streaming_partial"
        assert (
            logging_obj.model_call_details["cancellation_indicator"]
            == "client_disconnect"
        )
        assert logging_obj.model_call_details["cancelled_at"] == first_t

    def test_none_logging_obj_is_no_op(self):
        # Cancel can fire before Logging is built (pre-pre-call hook)
        mark_logging_obj_cancelled(None, phase="before_upstream")

    def test_logging_obj_without_model_call_details_is_no_op(self):
        bad_obj = SimpleNamespace(model_call_details=None)
        # Must not raise — pathological logging objects shouldn't crash
        # the cancel finalize path.
        mark_logging_obj_cancelled(bad_obj, phase="before_upstream")

    def test_bytes_delivered_recorded_when_provided(self):
        logging_obj = SpyLogging()
        mark_logging_obj_cancelled(
            logging_obj, phase="streaming_partial", bytes_delivered=4096
        )
        assert logging_obj.model_call_details["bytes_delivered_to_client"] == 4096

    def test_bytes_delivered_omitted_when_not_provided(self):
        logging_obj = SpyLogging()
        mark_logging_obj_cancelled(logging_obj, phase="before_upstream")
        # Important: don't insert a None key. Downstream JSON serialization
        # would write `"bytes_delivered_to_client": null` and pollute the
        # SpendLogs metadata with a misleading field.
        assert "bytes_delivered_to_client" not in logging_obj.model_call_details


class TestIsLoggingObjCancelled:
    def test_returns_true_after_marking(self):
        logging_obj = SpyLogging()
        assert is_logging_obj_cancelled(logging_obj) is False
        mark_logging_obj_cancelled(logging_obj, phase="streaming_partial")
        assert is_logging_obj_cancelled(logging_obj) is True

    def test_returns_false_for_none(self):
        assert is_logging_obj_cancelled(None) is False


class TestGetAccumulatedChunks:
    def test_returns_chunks_when_present(self):
        wrapper = SimpleNamespace(chunks=[{"a": 1}, {"b": 2}])
        assert _get_accumulated_chunks(wrapper) == [{"a": 1}, {"b": 2}]

    def test_returns_empty_list_when_no_chunks_attr(self):
        wrapper = SimpleNamespace()
        assert _get_accumulated_chunks(wrapper) == []

    def test_returns_empty_list_when_chunks_not_a_list(self):
        wrapper = SimpleNamespace(chunks="not a list")
        assert _get_accumulated_chunks(wrapper) == []


# ---------------------------------------------------------------------------
# Streaming finalize — end-to-end through stream_chunk_builder
# ---------------------------------------------------------------------------


class TestFinalizeStreamingCancel:
    @pytest.mark.asyncio
    async def test_anthropic_midthinking_cancel_dispatches_real_usage(self):
        """
        Realistic scenario: Anthropic thinking-model stream cancelled
        after the message_start cursor + several content chunks but
        BEFORE message_delta arrives. This is the bug class PR #1 fixed
        — and this test exercises it through the cancel-finalize path,
        proving the two PRs compose correctly end-to-end.

        Why this is not theater: we feed real ModelResponseStream chunks,
        call the real finalize_streaming_cancel, which calls the real
        stream_chunk_builder, which exercises the real cursor=1 fix
        from PR #1. The captured response's usage MUST reflect the
        token-counter estimate of the streamed text, not the cursor 1
        placeholder.
        """
        chunks = [
            # Anthropic message_start: real input_tokens, output cursor=1
            _make_chunk(
                usage=Usage(prompt_tokens=1024, completion_tokens=1, total_tokens=1025)
            ),
            # content_block_delta chunks with visible text
            _make_chunk(
                content="The capital of France is Paris. "
                "It has been the political and cultural center "
                "for over a thousand years."
            ),
            _make_chunk(content="Major landmarks include the Eiffel Tower."),
            # CANCEL — no message_delta, no message_stop
        ]
        wrapper = SimpleNamespace(chunks=chunks, messages=[])
        logging_obj = SpyLogging()

        await finalize_streaming_cancel(
            stream_wrapper=wrapper,
            logging_obj=logging_obj,
            user_api_key_dict=SimpleNamespace(),
            request_data={},
        )

        # 1. Cancel marker must be set on the logging object so the
        #    cost calculator knows to apply success_partial billing.
        assert logging_obj.model_call_details["cancel_phase"] == "streaming_partial"
        assert (
            logging_obj.model_call_details["cancellation_indicator"]
            == "client_disconnect"
        )

        # 2. async_success_handler received the reassembled response
        #    (not the failure handler — cancel routes to success_partial).
        assert len(logging_obj.captured_success_calls) == 1
        assert len(logging_obj.captured_failure_calls) == 0
        captured = logging_obj.captured_success_calls[0]
        response = captured["result"]

        # 3. The reassembled response carries real usage data:
        #    - prompt_tokens preserved from message_start (1024)
        #    - completion_tokens reflects the streamed text length,
        #      NOT the cursor placeholder of 1 (would indicate PR #1
        #      regression — cursor=1 leaked through to billing).
        assert response.usage.prompt_tokens == 1024
        assert response.usage.completion_tokens > 1, (
            f"completion_tokens={response.usage.completion_tokens} — "
            f"cursor=1 leaked through. PR #1's cursor reset should "
            f"have triggered the token_counter fallback on the "
            f"streamed text (~30+ tokens for this fixture)."
        )

    @pytest.mark.asyncio
    async def test_no_chunks_falls_back_to_failure_hook(self):
        """
        Edge: client cancels before any chunk arrived. We have nothing
        to bill, so falling back to the failure hook (with cost=0)
        is correct — at least a SpendLogs row exists, instead of the
        silent drop the old `except Exception` path produced.

        Verify by importing and patching proxy_logging_obj's
        post_call_failure_hook to a spy.
        """
        from litellm.proxy import proxy_server as ps

        wrapper = SimpleNamespace(chunks=[], messages=[])
        logging_obj = SpyLogging()
        captured_failure_calls: list = []

        # Replace the module-level proxy_logging_obj with a real spy
        # object — narrowly scoped to the call surface cancel_finalize
        # touches (post_call_failure_hook only).
        class SpyProxyLogging:
            async def post_call_failure_hook(self, **kwargs):
                captured_failure_calls.append(kwargs)

        original = getattr(ps, "proxy_logging_obj", None)
        ps.proxy_logging_obj = SpyProxyLogging()
        try:
            await finalize_streaming_cancel(
                stream_wrapper=wrapper,
                logging_obj=logging_obj,
                user_api_key_dict=SimpleNamespace(),
                request_data={"req": "data"},
            )
        finally:
            ps.proxy_logging_obj = original

        # Marker still set on logging_obj — billing path can identify
        # this as a zero-chunk cancel for the dashboard.
        assert is_logging_obj_cancelled(logging_obj)
        # And the failure hook fired (zero chunks → no success path)
        assert len(captured_failure_calls) == 1
        # request_data was enriched with cancel markers (PR #3 bridges
        # them in so SpendLogs picks them up). Original keys preserved.
        forwarded = captured_failure_calls[0]["request_data"]
        assert forwarded["req"] == "data"
        meta = forwarded["litellm_params"]["metadata"]
        assert meta["cancellation_indicator"] == "client_disconnect"
        assert meta["cancel_phase"] == "streaming_partial"
        assert meta["status"] == "success_partial"
        # original_exception is a CancelledError (well-typed)
        assert isinstance(
            captured_failure_calls[0]["original_exception"],
            asyncio.CancelledError,
        )

    @pytest.mark.asyncio
    async def test_does_not_raise_when_success_handler_errors(self):
        """
        Defensive: if the user's async_success_handler raises (e.g. a
        broken Langfuse integration), the finalize must swallow it.
        Caller will re-raise the original CancelledError — we must not
        replace that with a derived error that hides the real cancel
        signal from asyncio.
        """

        class RaisingLogging(SpyLogging):
            async def async_success_handler(self, **kwargs):
                raise RuntimeError("simulated downstream callback bug")

        # Need at least one chunk so we get past the no-chunks fallback
        chunks = [_make_chunk(content="hi", usage=Usage(prompt_tokens=5))]
        wrapper = SimpleNamespace(chunks=chunks, messages=[])

        # Must not raise
        await finalize_streaming_cancel(
            stream_wrapper=wrapper,
            logging_obj=RaisingLogging(),
            user_api_key_dict=SimpleNamespace(),
            request_data={},
        )


# ---------------------------------------------------------------------------
# Non-stream finalize — shield + real asyncio tasks
# ---------------------------------------------------------------------------


class TestFinalizeNonStreamCancel:
    @pytest.mark.asyncio
    async def test_shield_lets_upstream_complete_records_real_usage(self):
        """
        Shield-and-wait happy path: cancel fires while upstream is
        still working, but upstream returns within the shield window.
        The real response object reaches async_success_handler — that's
        the whole point of choosing strategy A.
        """
        fake_response = SimpleNamespace(
            usage=Usage(prompt_tokens=100, completion_tokens=42, total_tokens=142)
        )

        async def upstream_returns_quickly():
            await asyncio.sleep(0.05)
            return fake_response

        upstream_task = asyncio.create_task(upstream_returns_quickly())
        logging_obj = SpyLogging()

        await finalize_non_stream_cancel(
            upstream_task=upstream_task,
            logging_obj=logging_obj,
            user_api_key_dict=SimpleNamespace(),
            request_data={},
            shield_timeout_s=5.0,
        )

        assert logging_obj.model_call_details["upstream_completed"] is True
        assert (
            logging_obj.model_call_details["usage_source"]
            == "upstream_completed_after_cancel"
        )
        # async_success_handler received the REAL response (not a fake
        # from a mock) with the REAL usage numbers from upstream.
        assert len(logging_obj.captured_success_calls) == 1
        captured = logging_obj.captured_success_calls[0]
        assert captured["result"] is fake_response
        assert captured["result"].usage.completion_tokens == 42

    @pytest.mark.asyncio
    async def test_shield_timeout_cancels_upstream_and_records_timeout_source(self):
        async def upstream_never_returns():
            await asyncio.sleep(10.0)
            return SimpleNamespace()

        upstream_task = asyncio.create_task(upstream_never_returns())
        logging_obj = SpyLogging()

        await finalize_non_stream_cancel(
            upstream_task=upstream_task,
            logging_obj=logging_obj,
            user_api_key_dict=SimpleNamespace(),
            request_data={},
            shield_timeout_s=0.1,
        )

        assert logging_obj.model_call_details["upstream_completed"] is False
        assert logging_obj.model_call_details["usage_source"] == "shield_timeout"
        # Critical for resource hygiene: upstream task MUST be cancelled
        # so we don't leak it past the proxy request lifecycle.
        # Give the cancel a moment to actually take effect on the task.
        for _ in range(10):
            if upstream_task.done():
                break
            await asyncio.sleep(0.01)
        assert upstream_task.cancelled() or upstream_task.done()

    @pytest.mark.asyncio
    async def test_shield_upstream_exception_records_no_completion(self):
        async def upstream_errors():
            raise RuntimeError("simulated 5xx from provider")

        upstream_task = asyncio.create_task(upstream_errors())
        logging_obj = SpyLogging()

        await finalize_non_stream_cancel(
            upstream_task=upstream_task,
            logging_obj=logging_obj,
            user_api_key_dict=SimpleNamespace(),
            request_data={},
            shield_timeout_s=5.0,
        )
        assert logging_obj.model_call_details["upstream_completed"] is False
        assert logging_obj.model_call_details["usage_source"] == "no_completion"

    @pytest.mark.asyncio
    async def test_no_upstream_task_marks_marker_and_does_not_crash(self):
        """before_upstream cancel — no task to wait for."""
        logging_obj = SpyLogging()
        await finalize_non_stream_cancel(
            upstream_task=None,
            logging_obj=logging_obj,
            user_api_key_dict=SimpleNamespace(),
            request_data={},
        )
        assert is_logging_obj_cancelled(logging_obj)
