"""
Tests for the opt-in Anthropic invalid-thinking-signature retry gate.

Default behavior is OFF: an Anthropic 400 'Invalid `signature` in `thinking`
block' must propagate so routing / key-rotation issues stay visible. Callers
opt into stripping thinking blocks + retrying via:
  - litellm.anthropic_strip_thinking_on_signature_error (module flag)
  - litellm_settings.anthropic_strip_thinking_on_signature_error (proxy config,
    applied to the module flag at startup)
  - x-litellm-strip-thinking-on-signature-error request header (per-request
    override, resolved into litellm_params["strip_thinking_on_signature_error"])
"""

import os
import sys

import httpx
import pytest

sys.path.insert(
    0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../.."))
)

import litellm
from litellm.llms.anthropic.experimental_pass_through.messages.transformation import (
    AnthropicMessagesConfig,
)
from litellm.proxy.litellm_pre_call_utils import LiteLLMProxyRequestSetup

SIGNATURE_ERROR_TEXT = "messages.1.content.0: Invalid `signature` in `thinking` block"
UNRELATED_400_TEXT = "messages.0.content: invalid base64 image"


def _make_http_status_error(status_code: int, text: str) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(status_code=status_code, text=text, request=request)
    return httpx.HTTPStatusError(text, request=request, response=response)


@pytest.fixture
def config():
    return AnthropicMessagesConfig()


@pytest.fixture(autouse=True)
def _reset_module_flag():
    original = litellm.anthropic_strip_thinking_on_signature_error
    yield
    litellm.anthropic_strip_thinking_on_signature_error = original


class TestShouldRetryGate:
    def test_default_off_does_not_retry_on_signature_error(self, config):
        """Default (flag off, no override) → 400 propagates, no strip+retry."""
        litellm.anthropic_strip_thinking_on_signature_error = False
        e = _make_http_status_error(400, SIGNATURE_ERROR_TEXT)
        assert (
            config.should_retry_anthropic_messages_on_http_error(e=e, litellm_params={})
            is False
        )

    def test_module_flag_on_retries_on_signature_error(self, config):
        litellm.anthropic_strip_thinking_on_signature_error = True
        e = _make_http_status_error(400, SIGNATURE_ERROR_TEXT)
        assert (
            config.should_retry_anthropic_messages_on_http_error(e=e, litellm_params={})
            is True
        )

    def test_header_override_true_beats_module_off(self, config):
        litellm.anthropic_strip_thinking_on_signature_error = False
        e = _make_http_status_error(400, SIGNATURE_ERROR_TEXT)
        assert (
            config.should_retry_anthropic_messages_on_http_error(
                e=e,
                litellm_params={"strip_thinking_on_signature_error": True},
            )
            is True
        )

    def test_header_override_false_beats_module_on(self, config):
        litellm.anthropic_strip_thinking_on_signature_error = True
        e = _make_http_status_error(400, SIGNATURE_ERROR_TEXT)
        assert (
            config.should_retry_anthropic_messages_on_http_error(
                e=e,
                litellm_params={"strip_thinking_on_signature_error": False},
            )
            is False
        )

    def test_enabled_but_unrelated_400_does_not_retry(self, config):
        """Even opted-in, only the thinking-signature 400 is recoverable."""
        litellm.anthropic_strip_thinking_on_signature_error = True
        e = _make_http_status_error(400, UNRELATED_400_TEXT)
        assert (
            config.should_retry_anthropic_messages_on_http_error(e=e, litellm_params={})
            is False
        )

    def test_enabled_but_non_400_does_not_retry(self, config):
        litellm.anthropic_strip_thinking_on_signature_error = True
        e = _make_http_status_error(500, SIGNATURE_ERROR_TEXT)
        assert (
            config.should_retry_anthropic_messages_on_http_error(e=e, litellm_params={})
            is False
        )


class TestHeaderParsing:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("true", True),
            ("True", True),
            ("1", True),
            ("yes", True),
            ("on", True),
            ("false", False),
            ("0", False),
            ("no", False),
            ("", False),
        ],
    )
    def test_header_values(self, raw, expected):
        result = LiteLLMProxyRequestSetup._get_strip_thinking_on_signature_error_from_request(
            {"x-litellm-strip-thinking-on-signature-error": raw}
        )
        assert result is expected

    def test_header_absent_returns_none(self):
        """Absent header → None so the module/config default applies."""
        result = LiteLLMProxyRequestSetup._get_strip_thinking_on_signature_error_from_request(
            {}
        )
        assert result is None
