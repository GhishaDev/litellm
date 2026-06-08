"""
Client-cancellation finalization for LiteLLM streaming + non-streaming paths.

When a client disconnects mid-flight, asyncio injects ``CancelledError``
into the in-flight request task. Historically every ``except Exception``
in LiteLLM let this BaseException-subclass slip through, so:

* No SpendLogs record was written for the cancelled request.
* No failure or success callback fired (Langfuse trace ended in a
  permanent "Running" state, Prometheus failed_requests counter not
  incremented).
* The upstream provider (which often continues generating after a
  client TCP close) silently charged for compute that never reached
  the LiteLLM ledger.

This module re-routes cancellation through the proxy's existing
``async_success_handler`` path (with a new ``status="success_partial"``
marker) so that:

* SpendLogs always records a row, even on cancellation.
* Whatever was streamed (or, for non-stream, would have been streamed
  given the shield logic in cancel_billing.py) gets billed.
* Failure-rate metrics are not polluted by client-initiated cancels.
* Langfuse traces close cleanly with success-partial status.

This file is the catch-and-route plumbing. The cost computation for
the partial response lives in cancel_billing.py (added separately —
see PR #3 / decision D9 in the billing-accuracy plan).

Public API
----------
``mark_logging_obj_cancelled(logging_obj, phase, indicator)``
    Idempotently tags the LiteLLM Logging object with cancellation
    metadata. The downstream cost calculator (PR #3) reads these markers
    to drive the success_partial billing path.

``finalize_streaming_cancel(...)``
    Called from the proxy's streaming generator catch block. Builds a
    partial response from whatever chunks the stream wrapper has
    accumulated, then dispatches through the normal success callback
    chain so all the usual observability hooks fire.

Important: callers MUST re-raise the original ``CancelledError`` after
calling these helpers — swallowing the cancel signal would leak tasks
and confuse asyncio's cancellation propagation.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, List, Optional

import anyio

from litellm._logging import verbose_logger
from litellm.types.utils import CancelPhase

# Default timeout for the non-stream shield wait. 60 s is long enough to
# cover Anthropic thinking-model long-tails (p99 ~120 s on observed
# traffic — see e2e/tools/glm_repro findings) without holding the worker
# indefinitely. Tunable via env if a deployment finds it too generous.
DEFAULT_CANCEL_SHIELD_TIMEOUT_S: float = 60.0


def mark_logging_obj_cancelled(
    logging_obj: Any,
    *,
    phase: CancelPhase,
    indicator: str = "client_disconnect",
    bytes_delivered: Optional[int] = None,
) -> None:
    """
    Tag a Logging object with cancellation metadata so the downstream
    cost calculator and the SpendLogs row populator know to apply the
    success_partial billing path.

    Idempotent — calling twice on the same object is safe (a defensive
    re-call in nested catch blocks will not corrupt the first marker).

    Parameters
    ----------
    logging_obj : litellm.Logging
        The per-request Logging instance. May be ``None`` (e.g. cancel
        fired before pre_call ran), in which case this is a no-op.
    phase : CancelPhase
        Where in the request lifecycle the cancel was detected. Drives
        the billing strategy in cancel_billing.py.
    indicator : str
        Source of the cancel signal. Almost always ``"client_disconnect"``;
        reserved value ``"upstream_disconnect"`` is for the rare case
        where the upstream provider RST's mid-stream.
    bytes_delivered : Optional[int]
        Total bytes flushed to the client socket before the disconnect,
        if known. Helpful for client-vs-upstream gap analysis (zero
        bytes delivered + non-zero upstream usage = client paid for
        compute it never received).
    """
    if logging_obj is None:
        return

    # Logging object exposes a mutable dict for per-request scratch space.
    details = getattr(logging_obj, "model_call_details", None)
    if details is None:
        # Pathological case — the Logging instance has no scratch dict
        # (would mean function_setup never ran). Nothing safe to do here.
        verbose_logger.debug(
            "mark_logging_obj_cancelled: logging_obj.model_call_details is None, skipping"
        )
        return

    # Don't overwrite a prior, earlier marker. The phase progression is
    # before_upstream → during_upstream → streaming_partial → during_parsing,
    # and only the FIRST detection point is meaningful.
    if details.get("cancellation_indicator") is not None:
        return

    details["cancellation_indicator"] = indicator
    details["cancel_phase"] = phase
    details["cancelled_at"] = time.time()
    if bytes_delivered is not None:
        details["bytes_delivered_to_client"] = bytes_delivered


def is_logging_obj_cancelled(logging_obj: Any) -> bool:
    """Whether the cancel marker has been set on this Logging object."""
    if logging_obj is None:
        return False
    details = getattr(logging_obj, "model_call_details", None)
    if details is None:
        return False
    return details.get("cancellation_indicator") is not None


def _get_accumulated_chunks(stream_wrapper: Any) -> List:
    """
    Extract the accumulated chunks list from a streaming response wrapper.

    LiteLLM's CustomStreamWrapper buffers every yielded chunk on
    ``self.chunks`` (used by stream_chunk_builder at end-of-stream).
    For non-CustomStreamWrapper iterables (test fakes, custom user
    wrappers) we degrade gracefully to an empty list — downstream
    billing will fall back to the prompt-only path.
    """
    chunks = getattr(stream_wrapper, "chunks", None)
    if isinstance(chunks, list):
        return chunks
    return []


async def finalize_streaming_cancel(
    *,
    stream_wrapper: Any,
    logging_obj: Any,
    user_api_key_dict: Any,
    request_data: dict,
    bytes_delivered: Optional[int] = None,
) -> None:
    """
    Dispatch a streaming-cancel through the normal success callback chain
    with status="success_partial".

    Called from the catch block of the proxy's streaming generator (and
    defensively from CustomStreamWrapper.__anext__). The reassembled
    partial response is built via the same stream_chunk_builder path used
    by end-of-stream success, so all the cost-calculation / Langfuse /
    Prometheus hooks see a normal-looking response object — they just
    see the ``cancellation_indicator`` marker on the Logging instance
    and apply the partial-billing logic.

    Hardening notes:

    * The whole finalize is wrapped in ``anyio.CancelScope(shield=True)``.
      Without this, a follow-on cancel signal (the asyncio runtime can
      inject CancelledError multiple times into a task being torn down)
      would interrupt the SpendLogs write and leave us back at square
      one with no record.

    * Inner ``except Exception`` (NOT BaseException) catches *finalize-path
      bugs* without masking the cancel signal. Anything blowing up here
      gets logged at ERROR but does not propagate, because the caller
      will re-raise the original CancelledError after we return.

    * The caller is responsible for re-raising the CancelledError after
      this returns. Do NOT raise from here — would break asyncio's
      cancel-propagation contract.
    """
    import litellm

    with anyio.CancelScope(shield=True):
        try:
            mark_logging_obj_cancelled(
                logging_obj,
                phase="streaming_partial",
                indicator="client_disconnect",
                bytes_delivered=bytes_delivered,
            )

            chunks = _get_accumulated_chunks(stream_wrapper)
            if not chunks:
                # Nothing to bill — no chunks made it before cancel.
                # Fall back to the failure path so SpendLogs still gets
                # a row (with prompt cost only, via existing failure
                # hook logic).
                await _fallback_to_failure_hook(
                    user_api_key_dict=user_api_key_dict,
                    request_data=request_data,
                )
                return

            # Reassemble whatever we received. Stream_chunk_builder is
            # the same path the normal end-of-stream success handler
            # uses, so the cost calculator gets a familiar shape.
            partial_response = None
            try:
                partial_response = litellm.stream_chunk_builder(
                    chunks=chunks,
                    messages=getattr(stream_wrapper, "messages", None),
                    logging_obj=logging_obj,
                )
            except Exception as build_exc:
                verbose_logger.error(
                    "finalize_streaming_cancel: stream_chunk_builder failed; "
                    "falling back to failure hook: %s",
                    build_exc,
                    exc_info=True,
                )

            if partial_response is None:
                await _fallback_to_failure_hook(
                    user_api_key_dict=user_api_key_dict,
                    request_data=request_data,
                )
                return

            # Dispatch through normal success path. The cost calculator
            # picks up the cancellation_indicator marker (set above) and
            # applies the partial-billing logic from cancel_billing.py
            # (PR #3). Until that lands, the normal cost calc runs on
            # the partial response — which is already an improvement
            # over the prior "drop everything" behavior because
            # PR #1's cursor=1 fix makes the partial usage estimate
            # reasonable for Anthropic.
            if logging_obj is not None:
                try:
                    await logging_obj.async_success_handler(
                        result=partial_response,
                        start_time=getattr(logging_obj, "start_time", None),
                        end_time=time.time(),
                        cache_hit=False,
                    )
                except Exception as success_exc:
                    verbose_logger.error(
                        "finalize_streaming_cancel: async_success_handler "
                        "raised: %s",
                        success_exc,
                        exc_info=True,
                    )
        except Exception as outer_exc:
            # Last-resort: anything we didn't anticipate. Log but do NOT
            # let it propagate — the caller needs to re-raise the
            # original CancelledError.
            verbose_logger.error(
                "finalize_streaming_cancel: unexpected error: %s",
                outer_exc,
                exc_info=True,
            )


async def _fallback_to_failure_hook(
    *,
    user_api_key_dict: Any,
    request_data: dict,
) -> None:
    """
    Call the existing post_call_failure_hook for cancellation cases where
    we have no chunks to reassemble.

    This still produces a SpendLogs row (with cost=0 under the current
    failure-path logic, which PR #3 will improve to charge for the
    prompt-only baseline). Better than silent drop.
    """
    try:
        from litellm.proxy.proxy_server import proxy_logging_obj

        await proxy_logging_obj.post_call_failure_hook(
            user_api_key_dict=user_api_key_dict,
            original_exception=asyncio.CancelledError(
                "Client disconnected before any chunks were received"
            ),
            request_data=request_data,
        )
    except Exception as fb_exc:
        verbose_logger.error(
            "_fallback_to_failure_hook failed: %s", fb_exc, exc_info=True
        )


async def finalize_non_stream_cancel(
    *,
    upstream_task: Optional[asyncio.Task],
    logging_obj: Any,
    user_api_key_dict: Any,
    request_data: dict,
    shield_timeout_s: float = DEFAULT_CANCEL_SHIELD_TIMEOUT_S,
) -> None:
    """
    Non-stream cancellation finalizer with shield-and-wait semantics.

    For non-stream requests the upstream provider typically does NOT
    detect or honor client cancellation — once they've received our
    request body they generate to completion and bill us. To keep our
    SpendLogs honest we must shield the upstream call past the cancel,
    wait for the real usage to come back, and record it.

    Strategy
    --------
    1. Tag logging_obj with phase=during_upstream.
    2. Shield the upstream_task from the cancel signal.
    3. ``asyncio.wait_for`` it with ``shield_timeout_s`` — if upstream
       returns in time we get real usage; otherwise we fall back to
       prompt-only billing and tag ``usage_source=shield_timeout``.
    4. Always dispatch through async_success_handler so failure-rate
       metrics stay clean.

    Caller must still re-raise CancelledError after this returns. PR #3
    wires this into the non-stream code path; included here as part of
    PR #2 so the API surface is settled.
    """
    with anyio.CancelScope(shield=True):
        try:
            mark_logging_obj_cancelled(
                logging_obj,
                phase="during_upstream",
                indicator="client_disconnect",
                bytes_delivered=0,  # non-stream → nothing flushed yet
            )

            if upstream_task is None:
                # Cancel fired before we even kicked off the upstream
                # call — phase should really be before_upstream. Caller
                # should pre-set that via mark_logging_obj_cancelled.
                await _fallback_to_failure_hook(
                    user_api_key_dict=user_api_key_dict,
                    request_data=request_data,
                )
                return

            details = getattr(logging_obj, "model_call_details", {}) or {}
            try:
                response = await asyncio.wait_for(
                    upstream_task, timeout=shield_timeout_s
                )
                details["upstream_completed"] = True
                details["usage_source"] = "upstream_completed_after_cancel"
                if logging_obj is not None:
                    try:
                        await logging_obj.async_success_handler(
                            result=response,
                            start_time=getattr(logging_obj, "start_time", None),
                            end_time=time.time(),
                            cache_hit=False,
                        )
                    except Exception as success_exc:
                        verbose_logger.error(
                            "finalize_non_stream_cancel: async_success_handler "
                            "raised: %s",
                            success_exc,
                            exc_info=True,
                        )
            except asyncio.TimeoutError:
                # Upstream took longer than the shield budget. Cancel it
                # for real this time so we don't leak the task, and
                # fall back to prompt-only billing.
                details["upstream_completed"] = False
                details["usage_source"] = "shield_timeout"
                upstream_task.cancel()
                await _fallback_to_failure_hook(
                    user_api_key_dict=user_api_key_dict,
                    request_data=request_data,
                )
            except Exception as upstream_exc:
                # Upstream errored out during the shield window. Record
                # whatever phase info we have and bail to failure path.
                details["upstream_completed"] = False
                details["usage_source"] = "no_completion"
                verbose_logger.debug(
                    "finalize_non_stream_cancel: upstream raised during " "shield: %s",
                    upstream_exc,
                )
                await _fallback_to_failure_hook(
                    user_api_key_dict=user_api_key_dict,
                    request_data=request_data,
                )
        except Exception as outer_exc:
            verbose_logger.error(
                "finalize_non_stream_cancel: unexpected error: %s",
                outer_exc,
                exc_info=True,
            )
