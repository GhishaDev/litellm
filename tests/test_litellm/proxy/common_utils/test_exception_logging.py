"""Tests for litellm.proxy.common_utils.exception_logging.

The contract under test: every exception raised in a proxy route
handler gets routed to WARN (no traceback) or ERROR (with traceback)
based on whether it represents a client-induced/business outcome or
an unexpected server fault. These tests pin that policy so future
edits to the classifier can't silently re-introduce the
ERROR+traceback flood that triggered this work.
"""

import logging

import pytest
from fastapi import HTTPException

import litellm
from litellm.proxy._types import ProxyException
from litellm.proxy.common_utils.exception_logging import (
    classify_log_level,
    log_proxy_exception,
)


# ---------------------------------------------------------------------------
# classify_log_level — known business exception types must be WARN
# ---------------------------------------------------------------------------


class TestBusinessExceptionsAreWarnings:
    """The whole point of this module is that these never become
    ERROR-level traceback floods. If any of these regresses, this file
    is the canary."""

    def test_http_exception_409_is_warning(self) -> None:
        e = HTTPException(status_code=409, detail="User already exists")
        assert classify_log_level(e) == logging.WARNING

    def test_http_exception_404_is_warning(self) -> None:
        e = HTTPException(status_code=404, detail="not found")
        assert classify_log_level(e) == logging.WARNING

    def test_proxy_exception_is_warning(self) -> None:
        e = ProxyException(
            message="Invalid token",
            type="auth_invalid_credentials",
            param=None,
            code=401,
        )
        assert classify_log_level(e) == logging.WARNING

    def test_budget_exceeded_is_warning(self) -> None:
        e = litellm.BudgetExceededError(current_cost=10.0, max_budget=5.0)
        assert classify_log_level(e) == logging.WARNING

    def test_unsupported_params_is_warning(self) -> None:
        # The trigger case from the original investigation: zai
        # rejecting context_management should not be an ERROR.
        e = litellm.UnsupportedParamsError(
            status_code=400,
            message="zai does not support parameters: ['context_management']",
        )
        assert classify_log_level(e) == logging.WARNING

    def test_context_window_exceeded_is_warning(self) -> None:
        e = litellm.ContextWindowExceededError(
            message="too long", model="gpt-4", llm_provider="openai"
        )
        assert classify_log_level(e) == logging.WARNING


# ---------------------------------------------------------------------------
# classify_log_level — status-code based fallback
# ---------------------------------------------------------------------------


class TestStatusCodeFallback:
    """Even when the type isn't in our known list, a 4xx status_code
    or an upstream-5xx code is enough to route to WARN. Catches
    third-party subclasses we haven't named explicitly."""

    def test_unknown_exception_with_4xx_status_code_is_warning(self) -> None:
        class CustomClientError(Exception):
            status_code = 422

        assert classify_log_level(CustomClientError()) == logging.WARNING

    def test_unknown_exception_with_503_is_warning(self) -> None:
        class UpstreamUnavailable(Exception):
            status_code = 503

        assert classify_log_level(UpstreamUnavailable()) == logging.WARNING

    def test_unknown_exception_with_502_is_warning(self) -> None:
        class BadGateway(Exception):
            status_code = 502

        assert classify_log_level(BadGateway()) == logging.WARNING

    def test_unknown_exception_with_504_is_warning(self) -> None:
        class GatewayTimeout(Exception):
            status_code = 504

        assert classify_log_level(GatewayTimeout()) == logging.WARNING

    def test_exception_with_code_attr_instead_of_status_code(self) -> None:
        """ProxyException-shaped objects expose ``.code`` not ``.status_code``."""

        class CodeOnly(Exception):
            code = 409

        assert classify_log_level(CodeOnly()) == logging.WARNING


# ---------------------------------------------------------------------------
# classify_log_level — unexpected exceptions must remain ERROR
# ---------------------------------------------------------------------------


class TestUnexpectedExceptionsAreErrors:
    """The other side of the contract: real bugs must keep their
    traceback. If WARN spreads to these, we lose ERROR signal entirely.
    """

    def test_runtime_error_is_error(self) -> None:
        assert classify_log_level(RuntimeError("boom")) == logging.ERROR

    def test_key_error_is_error(self) -> None:
        assert classify_log_level(KeyError("missing")) == logging.ERROR

    def test_attribute_error_is_error(self) -> None:
        assert classify_log_level(AttributeError("no attr")) == logging.ERROR

    def test_bare_exception_is_error(self) -> None:
        assert classify_log_level(Exception("???")) == logging.ERROR

    def test_unknown_exception_with_500_is_error(self) -> None:
        """A plain 500 (not 502/503/504) is "we crashed". Keep the
        traceback."""

        class WeCrashed(Exception):
            status_code = 500

        assert classify_log_level(WeCrashed()) == logging.ERROR

    def test_unknown_exception_with_no_status_code_is_error(self) -> None:
        class Mystery(Exception):
            pass

        assert classify_log_level(Mystery()) == logging.ERROR


# ---------------------------------------------------------------------------
# log_proxy_exception — emission shape
# ---------------------------------------------------------------------------


class TestLogProxyExceptionEmission:
    """Verify the emitted LogRecord shape: level, exc_info, extra
    fields. Downstream log consumers grep on these, so this is part of
    the public contract."""

    def _capture(self, logger_name: str, level: int = logging.DEBUG):
        logger = logging.getLogger(logger_name)
        logger.setLevel(level)
        records: list[logging.LogRecord] = []

        class _H(logging.Handler):
            def emit(self, record):  # noqa: D401
                records.append(record)

        handler = _H()
        logger.addHandler(handler)
        return logger, records, handler

    def test_business_exception_emits_warning_without_traceback(self) -> None:
        logger, records, handler = self._capture("test.exc.warn")
        try:
            log_proxy_exception(
                logger,
                "/user/new",
                HTTPException(status_code=409, detail="User already exists"),
            )
        finally:
            logger.removeHandler(handler)

        assert len(records) == 1
        rec = records[0]
        assert rec.levelno == logging.WARNING
        # exc_info must be falsy — that's how we suppress the traceback
        assert not rec.exc_info
        assert rec.route == "/user/new"
        assert rec.http_status == 409
        assert rec.exception_type == "HTTPException"
        # The detail (not the type name) must appear in the formatted message
        assert "User already exists" in rec.getMessage()

    def test_unexpected_exception_emits_error_with_traceback(self) -> None:
        logger, records, handler = self._capture("test.exc.err")
        try:
            try:
                raise RuntimeError("kaboom")
            except RuntimeError as e:
                log_proxy_exception(logger, "/user/new", e)
        finally:
            logger.removeHandler(handler)

        assert len(records) == 1
        rec = records[0]
        assert rec.levelno == logging.ERROR
        # exc_info must be present (a tuple) so the traceback formatter
        # has something to work with
        assert rec.exc_info is not None
        assert rec.exception_type == "RuntimeError"
        assert rec.http_status is None

    def test_empty_str_exception_falls_back_to_type_name(self) -> None:
        """Some ProxyException instances stringify to ''. The log line
        must never be empty — fall back to the class name so logs are
        always grep-able by exception_type or message."""
        logger, records, handler = self._capture("test.exc.empty")

        class Silent(Exception):
            def __str__(self):  # noqa: D401
                return ""

        try:
            log_proxy_exception(logger, "/user/new", Silent())
        finally:
            logger.removeHandler(handler)

        assert len(records) == 1
        assert "Silent" in records[0].getMessage()

    def test_extra_kwargs_are_merged(self) -> None:
        logger, records, handler = self._capture("test.exc.extra")
        try:
            log_proxy_exception(
                logger,
                "/team/update",
                HTTPException(status_code=403, detail="forbidden"),
                extra={"team_id": "abc123"},
            )
        finally:
            logger.removeHandler(handler)

        assert len(records) == 1
        rec = records[0]
        assert rec.team_id == "abc123"
        assert rec.route == "/team/update"
        assert rec.http_status == 403


# ---------------------------------------------------------------------------
# Parametric coverage for the wider 4xx range, since the body of
# classify_log_level uses a range check.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("code", [400, 401, 402, 403, 404, 409, 422, 429, 499])
def test_all_4xx_codes_route_to_warning(code: int) -> None:
    class _E(Exception):
        status_code = code

    assert classify_log_level(_E()) == logging.WARNING


@pytest.mark.parametrize("code", [500, 501, 505, 599])
def test_non_upstream_5xx_codes_route_to_error(code: int) -> None:
    """502/503/504 are upstream, everything else in 5xx is ours."""

    class _E(Exception):
        status_code = code

    assert classify_log_level(_E()) == logging.ERROR
