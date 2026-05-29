import asyncio
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException, Request, status
from prisma import errors as prisma_errors
from prisma.errors import (
    ClientNotConnectedError,
    DataError,
    ForeignKeyViolationError,
    HTTPClientClosedError,
    MissingRequiredValueError,
    PrismaError,
    RawQueryError,
    RecordNotFoundError,
    TableNotFoundError,
    UniqueViolationError,
)

sys.path.insert(
    0, os.path.abspath("../../..")
)  # Adds the parent directory to the system path

from litellm._logging import verbose_proxy_logger
from litellm.proxy._types import ProxyErrorTypes, ProxyException
from litellm.proxy.auth.auth_exception_handler import UserAPIKeyAuthExceptionHandler


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "prisma_error",
    [
        # Specific connectivity subclasses.
        HTTPClientClosedError(),
        ClientNotConnectedError(),
        # Bare / generic PrismaError defaults to connectivity — we can't
        # tell what it is, so err on the safe side for genuine outages.
        PrismaError(),
    ],
)
async def test_handle_authentication_error_db_unavailable_connectivity(prisma_error):
    """Transport-level / connectivity failures (and generic PrismaError)
    trigger the HA fallback."""
    handler = UserAPIKeyAuthExceptionHandler()

    mock_request = MagicMock()
    with patch(
        "litellm.proxy.proxy_server.general_settings",
        {"allow_requests_on_db_unavailable": True},
    ):
        result = await handler._handle_authentication_error(
            prisma_error,
            mock_request,
            {},
            "/test",
            None,
            "test-key",
        )
        assert result.key_name == "failed-to-connect-to-db"
        assert result.token == "failed-to-connect-to-db"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "prisma_error",
    [
        DataError(data={"user_facing_error": {"meta": {"table": "test_table"}}}),
        UniqueViolationError(
            data={"user_facing_error": {"meta": {"table": "test_table"}}}
        ),
        ForeignKeyViolationError(
            data={"user_facing_error": {"meta": {"table": "test_table"}}}
        ),
        MissingRequiredValueError(
            data={"user_facing_error": {"meta": {"table": "test_table"}}}
        ),
        RawQueryError(data={"user_facing_error": {"meta": {"table": "test_table"}}}),
        TableNotFoundError(
            data={"user_facing_error": {"meta": {"table": "test_table"}}}
        ),
        RecordNotFoundError(
            data={"user_facing_error": {"meta": {"table": "test_table"}}}
        ),
    ],
)
async def test_handle_authentication_error_data_layer_errors_do_not_fall_back(
    prisma_error,
):
    """Known data-layer PrismaError subclasses (UniqueViolation,
    RecordNotFound, etc.) mean the DB IS reachable — they must propagate
    instead of triggering the HA fallback, which would grant the
    restricted INTERNAL_USER token to a request that should have
    returned 401."""
    handler = UserAPIKeyAuthExceptionHandler()

    mock_request = MagicMock()
    with patch(
        "litellm.proxy.proxy_server.general_settings",
        {"allow_requests_on_db_unavailable": True},
    ):
        with pytest.raises(ProxyException):
            await handler._handle_authentication_error(
                prisma_error,
                mock_request,
                {},
                "/test",
                None,
                "test-key",
            )


@pytest.mark.asyncio
async def test_handle_authentication_error_budget_exceeded():
    handler = UserAPIKeyAuthExceptionHandler()

    # Mock request and other dependencies
    mock_request = MagicMock()
    mock_request_data = {}
    mock_route = "/test"
    mock_span = None
    mock_api_key = "test-key"

    # Test with budget exceeded error
    with pytest.raises(ProxyException) as exc_info:
        from litellm.exceptions import BudgetExceededError

        budget_error = BudgetExceededError(
            message="Budget exceeded", current_cost=100, max_budget=100
        )
        await handler._handle_authentication_error(
            budget_error,
            mock_request,
            mock_request_data,
            mock_route,
            mock_span,
            mock_api_key,
        )

    assert exc_info.value.type == ProxyErrorTypes.budget_exceeded
    assert int(exc_info.value.code) == status.HTTP_429_TOO_MANY_REQUESTS


@pytest.mark.asyncio
async def test_known_auth_failure_logs_at_warning_without_traceback(caplog):
    """
    Regression for log-level mismatch: routine auth failures (no key,
    expired key, invalid key, role mismatch) used to be logged at ERROR
    with a full traceback via verbose_proxy_logger.exception. That fired
    on every 401 from a probe/scanner and buried real errors. They must
    now log at WARNING without exc_info, while truly unexpected exceptions
    keep ERROR + traceback.
    """
    import logging

    handler = UserAPIKeyAuthExceptionHandler()

    mock_request = MagicMock()
    mock_request.headers = {"x-litellm-call-id": "call-abc-123"}
    mock_request_data: dict = {}
    test_route = "/v1/chat/completions"
    mock_span = None
    mock_api_key = "test-key"

    with (
        patch(
            "litellm.proxy.proxy_server.general_settings",
            {"allow_requests_on_db_unavailable": False},
        ),
        patch(
            "litellm.proxy.proxy_server.proxy_logging_obj.post_call_failure_hook",
            new_callable=AsyncMock,
        ),
    ):
        caplog.set_level(logging.WARNING, logger=verbose_proxy_logger.name)
        # ProxyException is a known auth failure type — should log at WARN.
        proxy_exc = ProxyException(
            message="Authentication Error - Expired Key",
            type=ProxyErrorTypes.auth_error,
            param=None,
            code=401,
        )
        try:
            await handler._handle_authentication_error(
                proxy_exc,
                mock_request,
                mock_request_data,
                test_route,
                mock_span,
                mock_api_key,
            )
        except Exception:
            pass

    auth_records = [
        r for r in caplog.records if "user_api_key_auth failed" in r.getMessage()
    ]
    assert len(auth_records) == 1, "expected exactly one auth-failure log record"
    record = auth_records[0]
    # WARN level for the known auth error class.
    assert record.levelno == logging.WARNING
    # No traceback attached — exc_info=False sets the record attribute to
    # False, not None. Treat both as "no traceback".
    assert not record.exc_info
    # Structured extras carry the useful signal for log search / alerting.
    assert getattr(record, "request_id", None) == "call-abc-123"
    assert getattr(record, "route", None) == test_route
    assert getattr(record, "exception_type", None) == "ProxyException"
    # ProxyException.code is stored as string; whichever attribute the
    # exception exposed, the structured field carries it through. Compare
    # via str() so the test is robust to int/str representation.
    assert str(getattr(record, "http_status", None)) == "401"


@pytest.mark.asyncio
async def test_unknown_exception_logs_at_error_with_traceback(caplog):
    """
    Counterpart to the previous test: unexpected exceptions (not a known
    auth failure type) must still log at ERROR with a full traceback so
    operators see real bugs.
    """
    import logging

    handler = UserAPIKeyAuthExceptionHandler()

    mock_request = MagicMock()
    mock_request.headers = {}
    mock_request_data: dict = {}
    test_route = "/v1/chat/completions"

    with (
        patch(
            "litellm.proxy.proxy_server.general_settings",
            {"allow_requests_on_db_unavailable": False},
        ),
        patch(
            "litellm.proxy.proxy_server.proxy_logging_obj.post_call_failure_hook",
            new_callable=AsyncMock,
        ),
    ):
        caplog.set_level(logging.WARNING, logger=verbose_proxy_logger.name)
        # ValueError is NOT in _KNOWN_AUTH_ERROR_TYPES — must surface as ERROR.
        try:
            await handler._handle_authentication_error(
                ValueError("totally unexpected"),
                mock_request,
                mock_request_data,
                test_route,
                None,
                "test-key",
            )
        except Exception:
            pass

    auth_records = [
        r for r in caplog.records if "user_api_key_auth failed" in r.getMessage()
    ]
    assert len(auth_records) == 1
    record = auth_records[0]
    assert record.levelno == logging.ERROR
    # Traceback attached — exc_info=True populates this tuple at log time.
    assert record.exc_info is not None
    assert getattr(record, "exception_type", None) == "ValueError"


@pytest.mark.asyncio
async def test_empty_exception_message_falls_back_to_type_name(caplog):
    """
    ProxyException sometimes carries an empty `message` field; without the
    fallback the log line collapses to just the type name with no signal.
    Verify the format string substitutes the type name when str(e) is empty.
    """
    import logging

    handler = UserAPIKeyAuthExceptionHandler()

    mock_request = MagicMock()
    mock_request.headers = {}
    mock_request_data: dict = {}

    with (
        patch(
            "litellm.proxy.proxy_server.general_settings",
            {"allow_requests_on_db_unavailable": False},
        ),
        patch(
            "litellm.proxy.proxy_server.proxy_logging_obj.post_call_failure_hook",
            new_callable=AsyncMock,
        ),
    ):
        caplog.set_level(logging.WARNING, logger=verbose_proxy_logger.name)
        empty_exc = ProxyException(
            message="",
            type=ProxyErrorTypes.auth_error,
            param=None,
            code=401,
        )
        try:
            await handler._handle_authentication_error(
                empty_exc,
                mock_request,
                mock_request_data,
                "/v1/chat/completions",
                None,
                "test-key",
            )
        except Exception:
            pass

    auth_records = [
        r for r in caplog.records if "user_api_key_auth failed" in r.getMessage()
    ]
    assert len(auth_records) == 1
    # Message body should contain the exception type name (the fallback),
    # not be empty after the "user_api_key_auth failed:" prefix.
    assert "ProxyException" in auth_records[0].getMessage()


class TestClassifyAuthFailure:
    """Pure unit tests for `_classify_auth_failure` — guards the
    contract that wrapped HTTPExceptions surface as a specific auth_*
    type, so UI clients can route on `error.type` instead of regex-
    matching free-text messages."""

    def test_403_maps_to_permission_denied_regardless_of_detail(self):
        from litellm.proxy.auth.auth_exception_handler import _classify_auth_failure

        e = HTTPException(status_code=403, detail="anything at all")
        assert _classify_auth_failure(e) == ProxyErrorTypes.auth_permission_denied

    @pytest.mark.parametrize(
        "detail",
        [
            "Key has expired",
            "Authentication Error - Expired Key",
            "Your API key has been revoked",
            "Key has been deleted",
        ],
    )
    def test_401_with_expired_marker_maps_to_session_expired(self, detail):
        from litellm.proxy.auth.auth_exception_handler import _classify_auth_failure

        e = HTTPException(status_code=401, detail=detail)
        assert _classify_auth_failure(e) == ProxyErrorTypes.auth_session_expired

    @pytest.mark.parametrize(
        "detail",
        [
            "No auth header passed in",
            "No authentication credentials supplied",
            "Invalid API Key",
            "Invalid token format",
            "Invalid bearer credentials",
            "Token not found in database",
            "Key not found in database",
            "Malformed authorization header",
        ],
    )
    def test_401_with_invalid_credential_marker_maps_to_invalid_credentials(
        self, detail
    ):
        from litellm.proxy.auth.auth_exception_handler import _classify_auth_failure

        e = HTTPException(status_code=401, detail=detail)
        assert _classify_auth_failure(e) == ProxyErrorTypes.auth_invalid_credentials

    @pytest.mark.parametrize(
        "detail",
        [
            "Not allowed to access this endpoint",
            "Not authorized for this resource",
            "Admin only endpoint",
            "Admin-only operation",
            "Master Key required",
            "Requires admin role to access",
            "Insufficient permission",
            "Forbidden",
            "Access denied",
        ],
    )
    def test_401_with_permission_marker_maps_to_permission_denied(self, detail):
        from litellm.proxy.auth.auth_exception_handler import _classify_auth_failure

        e = HTTPException(status_code=401, detail=detail)
        assert _classify_auth_failure(e) == ProxyErrorTypes.auth_permission_denied

    def test_401_with_unknown_detail_falls_back_to_auth_error(self):
        # Critical fallback: ambiguous messages must NOT silently map to
        # a wrong specific type. The UI's heuristic handles auth_error.
        from litellm.proxy.auth.auth_exception_handler import _classify_auth_failure

        e = HTTPException(status_code=401, detail="something we have never seen")
        assert _classify_auth_failure(e) == ProxyErrorTypes.auth_error

    def test_401_with_empty_detail_falls_back_to_auth_error(self):
        from litellm.proxy.auth.auth_exception_handler import _classify_auth_failure

        e = HTTPException(status_code=401, detail="")
        assert _classify_auth_failure(e) == ProxyErrorTypes.auth_error

    def test_expired_markers_take_priority_over_invalid_markers(self):
        # A revoked key can surface as both "invalid" and "revoked" — we
        # prefer session_expired (the recovery flow is the same and the
        # label is semantically more accurate).
        from litellm.proxy.auth.auth_exception_handler import _classify_auth_failure

        e = HTTPException(status_code=401, detail="Invalid API Key - has been revoked")
        assert _classify_auth_failure(e) == ProxyErrorTypes.auth_session_expired


@pytest.mark.asyncio
async def test_wrapped_httpexception_carries_classified_type():
    """Wire-format contract: a wrapped HTTPException emerges as a
    ProxyException with the classified specific type, not the generic
    `auth_error`. UI's PR D2 handleErrorResponse keys off this."""
    handler = UserAPIKeyAuthExceptionHandler()
    mock_request = MagicMock()
    mock_request.headers = {}

    with (
        patch(
            "litellm.proxy.proxy_server.general_settings",
            {"allow_requests_on_db_unavailable": False},
        ),
        patch(
            "litellm.proxy.proxy_server.proxy_logging_obj.post_call_failure_hook",
            # AsyncMock's default return is a MagicMock — the wrapper at
            # auth_exception_handler.py treats any truthy return as
            # `transformed_exception` and replaces the original `e`,
            # which destroys the type we want to assert. Pin
            # return_value=None so the HTTPException flows through.
            new=AsyncMock(return_value=None),
        ),
    ):
        expired_http_exc = HTTPException(
            status_code=401, detail="Authentication Error - Expired Key"
        )
        with pytest.raises(ProxyException) as exc_info:
            await handler._handle_authentication_error(
                expired_http_exc,
                mock_request,
                {},
                "/v1/chat/completions",
                None,
                "test-key",
            )

    assert exc_info.value.type == ProxyErrorTypes.auth_session_expired


@pytest.mark.asyncio
async def test_wrapped_httpexception_permission_denied_carries_specific_type():
    handler = UserAPIKeyAuthExceptionHandler()
    mock_request = MagicMock()
    mock_request.headers = {}

    with (
        patch(
            "litellm.proxy.proxy_server.general_settings",
            {"allow_requests_on_db_unavailable": False},
        ),
        patch(
            "litellm.proxy.proxy_server.proxy_logging_obj.post_call_failure_hook",
            # Pin return None — see comment in
            # test_wrapped_httpexception_carries_classified_type.
            new=AsyncMock(return_value=None),
        ),
    ):
        perm_http_exc = HTTPException(
            status_code=401, detail="Master Key required to access this endpoint"
        )
        with pytest.raises(ProxyException) as exc_info:
            await handler._handle_authentication_error(
                perm_http_exc,
                mock_request,
                {},
                "/key/new",
                None,
                "test-key",
            )

    assert exc_info.value.type == ProxyErrorTypes.auth_permission_denied


@pytest.mark.asyncio
async def test_route_passed_to_post_call_failure_hook():
    """
    This route is used by proxy track_cost_callback's async_post_call_failure_hook to check if the route is an LLM route
    """
    handler = UserAPIKeyAuthExceptionHandler()

    # Mock request and other dependencies
    mock_request = MagicMock()
    mock_request_data = {}
    test_route = "/custom/route"
    mock_span = None
    mock_api_key = "test-key"

    # Mock proxy_logging_obj.post_call_failure_hook
    with patch(
        "litellm.proxy.proxy_server.proxy_logging_obj.post_call_failure_hook",
        new_callable=AsyncMock,
    ) as mock_post_call_failure_hook:
        # Test with DB connection error
        with patch(
            "litellm.proxy.proxy_server.general_settings",
            {"allow_requests_on_db_unavailable": False},
        ):
            try:
                await handler._handle_authentication_error(
                    PrismaError(),
                    mock_request,
                    mock_request_data,
                    test_route,
                    mock_span,
                    mock_api_key,
                )
            except Exception as e:
                pass
            asyncio.sleep(1)
            # Verify post_call_failure_hook was called with the correct route
            mock_post_call_failure_hook.assert_called_once()
            call_args = mock_post_call_failure_hook.call_args[1]
            assert call_args["user_api_key_dict"].request_route == test_route
