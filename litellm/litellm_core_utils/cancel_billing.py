"""
Cost computation for client-cancelled requests ("modified strategy 5'").

Companion to ``cancel_finalize.py``:

* ``cancel_finalize`` catches the cancel signal, tags the Logging
  object, and dispatches the partial response through
  ``async_success_handler``.
* This module computes the dollar cost for the partial work — the
  number that ends up in ``LiteLLM_SpendLogs.spend`` for the
  ``status="success_partial"`` row.

Billing strategy (per 2026-06-08 design decision):

1. **Always bill the prompt** — the upstream provider received the
   request and almost always started processing it. Recovering the
   input cost matches new-api's behavior and aligns with how OpenAI
   / Anthropic themselves charge.

2. **Bill output tokens that we have evidence for.** For streaming
   cancels with chunks accumulated, the upstream usage (or token-
   counter estimate of received text) already lives on the
   reassembled partial response — the normal cost calculator handles
   it correctly.

3. **For zero-evidence cancels** (cancel fired before any chunk
   arrived, or non-stream shield timed out) we still bill the
   prompt-only baseline rather than $0. The upstream charge is real
   even when we don't see the output.

The functions here implement the "zero-evidence prompt-only" case;
the "we have a partial response" case flows through the existing cost
calculator unchanged (which is correct after PR #1's cursor=1 fix).
"""

from __future__ import annotations

from typing import Any, List, Optional

from litellm._logging import verbose_logger


def compute_prompt_only_cost(
    messages: Optional[List] = None,
    model: Optional[str] = None,
    custom_llm_provider: Optional[str] = None,
) -> float:
    """
    Best-effort prompt-only cost for cancellations that produced no
    response (zero-chunk streaming cancel, or non-stream shield
    timeout). Returns 0.0 if the prompt can't be priced (unknown
    model, tokenizer failure, etc.) — never raises.

    This is NOT a substitute for real upstream usage. When the cancel
    path has chunks to work with, the regular cost calculator on the
    reassembled partial response produces a better number.

    Pricing-model rationale: counts prompt_tokens via ``token_counter``
    (the same path used by ``stream_chunk_builder``'s fallback after
    PR #1's cursor=1 fix), then uses ``cost_per_token`` against the
    LiteLLM model cost map. cache_creation / cache_read are zero
    because zero-chunk cancels haven't seen the message_start usage
    block where those fields are populated.
    """
    if not model or not messages:
        return 0.0

    try:
        import litellm

        prompt_tokens = litellm.token_counter(messages=messages, model=model)
        if prompt_tokens <= 0:
            return 0.0

        prompt_cost, _completion_cost = litellm.cost_per_token(
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=0,
            custom_llm_provider=custom_llm_provider,
        )
        return float(prompt_cost) if prompt_cost is not None else 0.0
    except Exception as exc:
        # Pricing the prompt is best-effort — model not in the cost map,
        # bad tokenizer config, unsupported provider all fall through to
        # 0.0. Better than crashing the failure-hook code path.
        verbose_logger.debug(
            "compute_prompt_only_cost: pricing failed for model=%s: %s",
            model,
            exc,
        )
        return 0.0


def enrich_request_metadata_with_cancel_markers(
    request_data: dict,
    logging_obj: Any,
) -> None:
    """
    Copy the cancellation markers from ``logging_obj.model_call_details``
    into ``request_data["litellm_params"]["metadata"]`` so that the
    standard SpendLogs metadata pipeline picks them up and persists
    them into the metadata JSON column.

    Without this bridge, ``cancel_finalize.mark_logging_obj_cancelled``
    writes to the Logging object but the markers never reach SpendLogs
    — the metadata-keyed extractor in ``_get_spend_logs_metadata``
    pulls from ``litellm_params.metadata``, not from
    ``model_call_details``.

    Idempotent. No-op when the Logging object has no markers (i.e.
    this is a normal, non-cancelled request).
    """
    if logging_obj is None:
        return
    details = getattr(logging_obj, "model_call_details", None)
    if not details or details.get("cancellation_indicator") is None:
        return

    # Build the list of target metadata dicts to mutate. We have to
    # touch ALL of:
    #
    #   1. request_data["litellm_params"]["metadata"] — used by the
    #      proxy failure-hook path to build SpendLogs.
    #   2. request_data["litellm_params"]["litellm_metadata"] — used
    #      by newer endpoints (e.g. /v1/messages, anthropic_messages,
    #      generate_content). get_litellm_metadata_from_kwargs prefers
    #      litellm_metadata when both are present, so writing only to
    #      metadata leaves the newer endpoints' markers invisible.
    #   3. logging_obj.litellm_params["metadata"] — used by the litellm
    #      Logging.async_success_handler / cost callback when the
    #      Logging object's litellm_params dict has diverged from
    #      request_data's (some code paths copy at construction time).
    #   4. logging_obj.litellm_params["litellm_metadata"] — same as
    #      above but for the newer-endpoint variant.
    #
    # We write the cancel markers to whichever variants already exist,
    # plus always to "metadata" (which the old endpoints + the failure
    # hook read). Idempotent; safe to call multiple times.
    target_dicts: list = []

    def _ensure_metadata_dicts(parent: dict) -> None:
        if "metadata" not in parent or parent["metadata"] is None:
            parent["metadata"] = {}
        target_dicts.append(parent["metadata"])
        # litellm_metadata is only present on newer endpoints; if it's
        # already there with content, we must also write to it (the
        # extractor prefers it over metadata).
        existing_litellm_metadata = parent.get("litellm_metadata")
        if isinstance(existing_litellm_metadata, dict):
            target_dicts.append(existing_litellm_metadata)

    # 1+2: request_data side.
    if "litellm_params" not in request_data:
        request_data["litellm_params"] = {}
    _ensure_metadata_dicts(request_data["litellm_params"])

    # 3+4: logging_obj side, if it has its own litellm_params dict.
    lp = getattr(logging_obj, "litellm_params", None)
    if isinstance(lp, dict):
        prior_targets = list(target_dicts)
        _ensure_metadata_dicts(lp)
        # De-dup: skip any dict already in our list (when proxy did the
        # usual `logging_obj.litellm_params = request_data["litellm_params"]`
        # assignment, the same dict gets enumerated twice).
        target_dicts = prior_targets + [
            d for d in target_dicts[len(prior_targets) :] if d not in prior_targets
        ]

    # Copy the five cancellation fields into every target.
    for target_metadata in target_dicts:
        for field in (
            "cancellation_indicator",
            "cancel_phase",
            "bytes_delivered_to_client",
            "upstream_completed",
            "usage_source",
        ):
            if field in details:
                target_metadata[field] = details[field]

        # Also override status so the SpendLogs row gets success_partial.
        target_metadata["status"] = "success_partial"
