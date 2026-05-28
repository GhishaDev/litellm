"""
Handles Authentication Errors
"""

import logging
from typing import TYPE_CHECKING, Any, Optional, Union

from fastapi import HTTPException, Request, status

import litellm
from litellm._logging import verbose_proxy_logger
from litellm.proxy._types import (
    LitellmUserRoles,
    ProxyErrorTypes,
    ProxyException,
    UserAPIKeyAuth,
)
from litellm.proxy.auth.auth_utils import _get_request_ip_address
from litellm.proxy.db.exception_handler import PrismaDBExceptionHandler
from litellm.types.services import ServiceTypes

# Sentinel user_id for the synthetic UserAPIKeyAuth issued during a DB
# outage when allow_requests_on_db_unavailable is True. Downstream
# enforcement can key off this value; it must never collide with a real
# user_id.
DB_UNAVAILABLE_FALLBACK_USER_ID = "__db_unavailable_fallback__"

# Known auth-failure exception types. These reflect a normal 401/403 outcome
# (no key, expired key, invalid key, route not allowed, budget exceeded) and
# should NOT be logged at ERROR with a full traceback — they happen routinely
# from probes, expired sessions, and fat-fingered keys, and flooding the
# error monitoring stream with them buries genuine issues.
_KNOWN_AUTH_ERROR_TYPES = (ProxyException, HTTPException)

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

            # Non-admin restricted token so a DB outage cannot escalate
            # an anonymous caller to proxy-admin privileges.
            verbose_proxy_logger.warning(
                "Auth: DB unavailable — issuing restricted INTERNAL_USER "
                "fallback token (allow_requests_on_db_unavailable=True)"
            )
            return UserAPIKeyAuth(
                key_name="failed-to-connect-to-db",
                token="failed-to-connect-to-db",
                user_id=DB_UNAVAILABLE_FALLBACK_USER_ID,
                user_role=LitellmUserRoles.INTERNAL_USER,
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
            _is_known = isinstance(e, _KNOWN_AUTH_ERROR_TYPES)
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
                    code=getattr(e, "status_code", status.HTTP_429_TOO_MANY_REQUESTS),
                )
            if isinstance(e, HTTPException):
                raise ProxyException(
                    message=getattr(e, "detail", f"Authentication Error({str(e)})"),
                    type=ProxyErrorTypes.auth_error,
                    param=getattr(e, "param", "None"),
                    code=getattr(e, "status_code", status.HTTP_401_UNAUTHORIZED),
                )
            elif isinstance(e, ProxyException):
                raise e
            raise ProxyException(
                message="Authentication Error, " + str(e),
                type=ProxyErrorTypes.auth_error,
                param=getattr(e, "param", "None"),
                code=status.HTTP_401_UNAUTHORIZED,
            )
