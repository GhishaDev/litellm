"""
Classify and log exceptions raised inside proxy route handlers.

Why this exists:

Most management-endpoint route handlers in this codebase end with a
catch-all ``except Exception as e:`` block that calls
``verbose_proxy_logger.exception(...)`` before re-raising. ``.exception()``
always emits at ERROR with a full traceback — which is the right thing
when the proxy itself crashes (Pydantic blew up, the DB went away, an
attribute is missing), but the wrong thing for the vast majority of
exceptions that actually flow through these routes: ``HTTPException(409)``
"already exists", ``ProxyException(token_not_found_in_db)``,
``BudgetExceededError``, ``UnsupportedParamsError`` — the status code
and message ARE the response; the traceback adds no signal and just
buries genuine ERROR events under client-induced noise.

The fix is to centralize the "is this exception interesting?" decision
in one place. Business / client-induced errors (4xx with a stable type)
go to WARN as a single line carrying status_code + route +
exception_type + message. Truly unexpected exceptions still hit ERROR
with a traceback. Upstream provider 5xx (502/503/504) is treated as
"not our bug" — WARN, optionally tracked by Prometheus.

See ``classify_log_level`` for the exact rules.
"""

import logging
from typing import Optional

from fastapi import HTTPException

import litellm
from litellm.proxy._types import ProxyException

# Exceptions whose status_code + message fully describe what happened.
# Adding a traceback would only obscure the WARN line; we never want
# these to count as ERROR events. Order is taxonomic, not by frequency.
_KNOWN_BUSINESS_EXCEPTIONS: tuple = (
    HTTPException,
    ProxyException,
    litellm.BudgetExceededError,
    litellm.RateLimitError,
    litellm.AuthenticationError,
    litellm.PermissionDeniedError,
    litellm.NotFoundError,
    litellm.BadRequestError,
    litellm.UnsupportedParamsError,
    litellm.ContextWindowExceededError,
    litellm.ContentPolicyViolationError,
    litellm.UnprocessableEntityError,
    litellm.Timeout,
)

# Upstream-provider failure status codes. These represent the
# *upstream* service (OpenAI/Anthropic/etc.) being unhealthy, not our
# proxy. A traceback through our request path does not help diagnose
# why OpenAI returned 503, so log at WARN and let Prometheus counters
# drive aggregation/alerting on these.
_UPSTREAM_5XX = frozenset({502, 503, 504})


def _extract_status_code(e: Exception) -> Optional[int]:
    """Pull a numeric HTTP status off an exception, regardless of which
    library raised it. LiteLLM/OpenAI errors expose ``.status_code``;
    ProxyException exposes ``.code``; FastAPI's HTTPException uses
    ``.status_code``. Anything else returns None.
    """
    for attr in ("status_code", "code"):
        value = getattr(e, attr, None)
        if isinstance(value, int):
            return value
    return None


def classify_log_level(e: Exception) -> int:
    """Return the logging level a proxy route handler should use when
    logging ``e``.

    Decision order:

    1. If ``e`` is one of the known business-exception types, return
       ``WARNING``. These are deliberate ``raise``s with a status code
       and a stable wire-format type; the user did something the API
       rejects, and a traceback adds nothing.
    2. If ``e`` carries a 4xx HTTP status code, return ``WARNING``.
       Client error — even if the exception type is unfamiliar (some
       library may subclass without inheriting from our known list),
       the status code itself is the load-bearing signal.
    3. If ``e`` carries an upstream-5xx code (502/503/504), return
       ``WARNING``. Provider problem, not ours.
    4. Otherwise — including bare ``Exception``, ``RuntimeError``,
       ``KeyError``, ``AttributeError``, real 500s, and anything with
       no status_code at all — return ``ERROR``. The traceback IS the
       reason to log.
    """
    if isinstance(e, _KNOWN_BUSINESS_EXCEPTIONS):
        return logging.WARNING

    status_code = _extract_status_code(e)
    if status_code is not None:
        if 400 <= status_code < 500:
            return logging.WARNING
        if status_code in _UPSTREAM_5XX:
            return logging.WARNING

    return logging.ERROR


def log_proxy_exception(
    logger: logging.Logger,
    route: str,
    e: Exception,
    *,
    extra: Optional[dict] = None,
) -> None:
    """Log ``e`` at the level determined by :func:`classify_log_level`.

    For WARN-level entries we emit a single line with structured
    ``extra`` fields and no traceback. For ERROR-level entries we
    attach ``exc_info`` so the traceback is preserved exactly as
    ``logger.exception(...)`` would have done.

    ``route`` should be a stable identifier ("/user/new", "/team/update",
    etc.) so log consumers can group by endpoint without parsing the
    free-form message.
    """
    level = classify_log_level(e)
    status_code = _extract_status_code(e)

    # ``.detail`` is FastAPI/HTTPException; ``str(e)`` covers everything
    # else. Fall back to the type name so the log line is never empty
    # (some ProxyException instances stringify to ""), preserving the
    # grep-ability guarantee from the earlier auth_exception_handler
    # work.
    detail = getattr(e, "detail", None)
    message = str(detail) if detail else (str(e) or type(e).__name__)

    log_extra: dict = {
        "route": route,
        "exception_type": type(e).__name__,
        "http_status": status_code,
    }
    if extra:
        log_extra.update(extra)

    logger.log(
        level,
        "%s failed: %s",
        route,
        message,
        extra=log_extra,
        exc_info=(level >= logging.ERROR),
    )
