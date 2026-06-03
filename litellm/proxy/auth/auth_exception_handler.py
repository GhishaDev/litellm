"""
Handles Authentication Errors
"""

import logging
from typing import TYPE_CHECKING, Any, Optional, Union

from fastapi import HTTPException, Request, status

import litellm
from litellm._logging import verbose_proxy_logger
from litellm.proxy._types import ProxyErrorTypes, ProxyException, UserAPIKeyAuth
from litellm.proxy.auth.auth_utils import _get_request_ip_address
from litellm.proxy.db.exception_handler import PrismaDBExceptionHandler
from litellm.types.services import ServiceTypes

# Known auth-failure exception types. These reflect a normal 401/403 outcome
# (no key, expired key, invalid key, route not allowed, budget exceeded) and
# should NOT be logged at ERROR with a full traceback — they happen routinely
# from probes, expired sessions, and fat-fingered keys, and flooding the
# error monitoring stream with them buries genuine issues.
_KNOWN_AUTH_ERROR_TYPES = (ProxyException, HTTPException)

# Substrings (lowercase) in an upstream HTTPException's `detail` that
# indicate the caller's session/token was once valid but is no longer.
# Used by `_classify_auth_failure` to pick the right ProxyErrorTypes value
# when wrapping HTTPException — so the UI can route on a structured type
# instead of regex-matching free-text again.
_SESSION_EXPIRED_DETAIL_MARKERS = (
    "expired",
    "expir",  # covers expired / expiration
    "revoked",
    "key has been deleted",
    "key has expired",
)

# Substrings indicating the supplied credential was never valid (bad
# format, missing entirely, not in DB).
_INVALID_CREDENTIALS_DETAIL_MARKERS = (
    "no auth header",
    "no authentication",
    "no api key passed",  # bare `Exception("No api key passed in.")` from user_api_key_auth.py
    "no api key",  # broader form
    "invalid api key",
    "invalid token",
    "invalid bearer",
    "token not found",
    "key not found",
    "malformed",
    "malformed api key",  # bare `Exception("Malformed API Key passed in. ...")`
    "virtual key expected",  # bare `Exception("LiteLLM Virtual Key expected. ...")`
    "expected to start with 'sk-'",  # tail of the same exception
)

# Substrings indicating the caller IS authenticated but lacks the
# privilege for this specific operation. The role/scope/admin language
# is the giveaway. Distinct from "expired" or "invalid" — the session is
# fine, just this endpoint isn't allowed.
_PERMISSION_DENIED_DETAIL_MARKERS = (
    "not allowed",
    "not authorized",
    "admin only",
    "admin-only",
    "proxy admin",  # "Only proxy admin can be used to generate ..."
    "your role",  # "Your role=unknown" / "Your role is not allowed ..."
    "master key",
    "requires",  # "requires admin role", "requires master key", etc.
    "permission",
    "forbidden",
    "access denied",
)


def _classify_auth_failure(e: Exception) -> "ProxyErrorTypes":
    """Pick a specific ProxyErrorTypes for an auth-pipeline exception
    based on its status code (if any) and message text.

    Rationale: the wrapper used to collapse every wrapped auth failure
    into the generic `auth_error` type, leaving UI clients no way to
    tell "your session is gone, redirect to login" apart from "you're
    logged in but not authorized for THIS endpoint" — both arrived as
    401 with `type=auth_error`. This function makes that decision once,
    centrally, so the wire-format `type` field carries the action.

    Works for BOTH:
    - HTTPException — uses status_code + detail text
    - bare Exception — uses str(e). The auth pipeline raises plenty of
      these as final messages, e.g.
        Exception("No api key passed in.")
        Exception("LiteLLM Virtual Key expected. Received=... start with 'sk-'")
        Exception("Malformed API Key passed in. Ensure Key has `Bearer` prefix.")
      so the classifier MUST inspect them or the bare-Exception
      catch-all in the wrapper keeps emitting plain `auth_error` and
      defeats the whole point of D1.

    Returns the most specific type we can confidently determine. Falls
    back to `auth_error` only when truly ambiguous.

    Logic (in priority order):
    - HTTP 403 -> `auth_permission_denied` (semantics of 403)
    - text contains an expired/revoked marker -> `auth_session_expired`
    - text contains an invalid/missing-credential marker -> `auth_invalid_credentials`
    - text contains a permission/role marker -> `auth_permission_denied`
      (LiteLLM uses 401 for role mismatch too)
    - Otherwise -> `auth_error` (UI falls back to its heuristic)
    """
    status_code = getattr(e, "status_code", None)
    # Prefer HTTPException.detail; fall back to str(e) so the same
    # function classifies bare Exceptions from the auth pipeline.
    detail_attr = str(getattr(e, "detail", "") or "")
    text = (detail_attr or str(e)).lower()

    if status_code == 403:
        return ProxyErrorTypes.auth_permission_denied

    # Order matters: check expired first because revoked keys often
    # surface as "invalid" too, and we want to label them as
    # session-expired (the recovery action — re-login — is the same and
    # more accurate semantically).
    if any(marker in text for marker in _SESSION_EXPIRED_DETAIL_MARKERS):
        return ProxyErrorTypes.auth_session_expired
    if any(marker in text for marker in _INVALID_CREDENTIALS_DETAIL_MARKERS):
        return ProxyErrorTypes.auth_invalid_credentials
    if any(marker in text for marker in _PERMISSION_DENIED_DETAIL_MARKERS):
        return ProxyErrorTypes.auth_permission_denied

    return ProxyErrorTypes.auth_error


if TYPE_CHECKING:
    from opentelemetry.trace import Span as _Span

    Span = Union[_Span, Any]
else:
    Span = Any


class UserAPIKeyAuthExceptionHandler:
    @staticmethod
    async def _handle_authentication_error(
        e: Exception,
        request: Request,
        request_data: dict,
        route: str,
        parent_otel_span: Optional[Span],
        api_key: str,
    ) -> UserAPIKeyAuth:
        """
        Handles Connection Errors when reading a Virtual Key from LiteLLM DB
        Use this if you don't want failed DB queries to block LLM API reqiests

        Reliability scenarios this covers:
        - DB is down and having an outage
        - Unable to read / recover a key from the DB

        Returns:
            - UserAPIKeyAuth: If general_settings.allow_requests_on_db_unavailable is True

        Raises:
            - Original Exception in all other cases
        """
        from litellm.proxy.proxy_server import (
            general_settings,
            litellm_proxy_admin_name,
            proxy_logging_obj,
        )

        if (
            PrismaDBExceptionHandler.should_allow_request_on_db_unavailable()
            and PrismaDBExceptionHandler.is_database_connection_error(e)
        ):
            # log this as a DB failure on prometheus
            proxy_logging_obj.service_logging_obj.service_failure_hook(
                service=ServiceTypes.DB,
                call_type="get_key_object",
                error=e,
                duration=0.0,
            )

            return UserAPIKeyAuth(
                key_name="failed-to-connect-to-db",
                token="failed-to-connect-to-db",
                user_id=litellm_proxy_admin_name,
                request_route=route,
            )
        else:
            # raise the exception to the caller
            requester_ip = _get_request_ip_address(
                request=request,
                use_x_forwarded_for=general_settings.get("use_x_forwarded_for", False),
            )

            # Known auth failures (no/expired/invalid key, role mismatch,
            # budget exceeded) are normal 401/403 outcomes — log them at
            # WARN without a traceback so the error stream stays signal.
            # Truly unexpected exceptions still go to ERROR with a stack.
            #
            # Two-tier check: the wire-format types (ProxyException /
            # HTTPException) are always known. Bare ``Exception`` is the
            # tricky case — the auth pipeline raises a lot of these
            # ("No api key passed in.", "Malformed API Key passed in. ...",
            # "LiteLLM Virtual Key expected. ..."). Run the same
            # text classifier we use for the wire `type` field; if it
            # confidently routes to a specific auth_* type, treat the
            # exception as known and drop the traceback. Only the catch-all
            # `auth_error` outcome (genuinely ambiguous) keeps ERROR+stack.
            _is_known = isinstance(e, _KNOWN_AUTH_ERROR_TYPES) or (
                _classify_auth_failure(e) != ProxyErrorTypes.auth_error
            )
            _log_level = logging.WARNING if _is_known else logging.ERROR

            # `str(e)` is sometimes empty for ProxyException, which historically
            # left the log line as just the exception type name with no signal
            # for why the request was rejected. Fall back to the type name so
            # there is always SOMETHING to grep on.
            _message_part = str(e) or type(e).__name__

            verbose_proxy_logger.log(
                _log_level,
                "user_api_key_auth failed: %s",
                _message_part,
                extra={
                    "requester_ip": requester_ip,
                    "request_id": request.headers.get("x-litellm-call-id"),
                    "route": route,
                    "exception_type": type(e).__name__,
                    "http_status": (
                        getattr(e, "code", None) or getattr(e, "status_code", None)
                    ),
                },
                # Only attach a traceback for unexpected exceptions. Known
                # auth errors are self-explanatory from the type + message.
                exc_info=not _is_known,
            )

            # Log this exception to OTEL, Datadog etc
            user_api_key_dict = UserAPIKeyAuth(
                parent_otel_span=parent_otel_span,
                api_key=api_key,
                request_route=route,
            )
            # Allow callbacks to transform the error response
            transformed_exception = await proxy_logging_obj.post_call_failure_hook(
                request_data=request_data,
                original_exception=e,
                user_api_key_dict=user_api_key_dict,
                error_type=ProxyErrorTypes.auth_error,
                route=route,
            )
            # Use transformed exception if callback returned one, otherwise use original
            if transformed_exception is not None:
                e = transformed_exception

            if isinstance(e, litellm.BudgetExceededError):
                raise ProxyException(
                    message=e.message,
                    type=ProxyErrorTypes.budget_exceeded,
                    param=None,
                    code=400,
                )
            if isinstance(e, HTTPException):
                # Classify into the specific auth_* type so UI clients can
                # route on `type` instead of regex-matching the free-text
                # message. Centralized here rather than at each raise site
                # because most HTTPExceptions in the auth pipeline come
                # from FastAPI / upstream code we don't own.
                raise ProxyException(
                    message=getattr(e, "detail", f"Authentication Error({str(e)})"),
                    type=_classify_auth_failure(e),
                    param=getattr(e, "param", "None"),
                    code=getattr(e, "status_code", status.HTTP_401_UNAUTHORIZED),
                )
            elif isinstance(e, ProxyException):
                # Inner exception already carries a specific type
                # (e.g. token_not_found_in_db, expired_key, *_access_denied);
                # passing it through preserves that signal end-to-end.
                raise e
            # Catch-all for bare Exception. Classify by message content
            # so the wire-format type still carries an action signal —
            # the auth pipeline raises plenty of these (e.g. missing/
            # malformed/wrong-prefix key checks). _classify_auth_failure
            # transparently inspects str(e) when there's no `detail`.
            raise ProxyException(
                message="Authentication Error, " + str(e),
                type=_classify_auth_failure(e),
                param=getattr(e, "param", "None"),
                code=status.HTTP_401_UNAUTHORIZED,
            )
