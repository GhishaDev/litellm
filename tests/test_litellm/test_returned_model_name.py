"""
Tests for the per-deployment `returned_model_name` override.

Configured under `model_list[].litellm_params.returned_model_name`, this
literal string is stamped onto the `model` field of every response — non-
streaming + streaming, OpenAI `/v1/chat/completions` + Anthropic
`/v1/messages` (including the nested `message_start.message.model` that
the existing OpenAI chunk restamper does not touch).

Empty / whitespace `returned_model_name` is treated as unset so a misconfig
like `returned_model_name: ""` falls back to the client-requested name
instead of returning `model=""` (which breaks OpenAI-compatible clients).
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from litellm.proxy.common_request_processing import (  # noqa: E402
    ProxyBaseLLMRequestProcessing,
    _override_openai_response_model,
    _resolve_returned_model_name,
)
from litellm.proxy.proxy_server import (  # noqa: E402
    _get_client_requested_model_for_streaming,
    _restamp_streaming_chunk_model,
)
from litellm.types.router import GenericLiteLLMParams  # noqa: E402


class _FakeDeployment:
    """Stand-in for the Deployment object returned by Router.get_deployment.
    Mirrors both the attribute-style (typical) and dict-style (rare)
    `litellm_params` containers the resolver supports."""

    def __init__(self, litellm_params):
        self.litellm_params = litellm_params


class _FakeRouter:
    """Minimal Router stub: returns a configured deployment for any
    model_id; behavior on lookup failure is controlled via `raise_exc`."""

    def __init__(self, deployment=None, raise_exc: bool = False):
        self._deployment = deployment
        self._raise_exc = raise_exc

    def get_deployment(self, *, model_id):
        if self._raise_exc:
            raise RuntimeError("router lookup blew up")
        return self._deployment


class _FakeLitellmParams:
    """Object-style litellm_params (the common case — Pydantic model)."""

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


class TestResolveReturnedModelName:
    """`_resolve_returned_model_name` extracts the per-deployment override.

    Empty/whitespace must return None so a misconfigured
    `returned_model_name: ""` in YAML falls back to the client-requested
    name (preserves OpenAI-compatible clients that reject `model=""`).
    """

    # ---- happy path ------------------------------------------------------
    def test_object_style_litellm_params(self):
        router = _FakeRouter(
            _FakeDeployment(_FakeLitellmParams(returned_model_name="public-name"))
        )
        assert (
            _resolve_returned_model_name(llm_router=router, model_id="id-1")
            == "public-name"
        )

    def test_dict_style_litellm_params(self):
        router = _FakeRouter(_FakeDeployment({"returned_model_name": "public-name"}))
        assert (
            _resolve_returned_model_name(llm_router=router, model_id="id-1")
            == "public-name"
        )

    def test_dict_style_deployment(self):
        """Some code paths surface deployment as a dict; resolver must
        still find litellm_params inside it."""
        router = _FakeRouter({"litellm_params": {"returned_model_name": "public-name"}})
        assert (
            _resolve_returned_model_name(llm_router=router, model_id="id-1")
            == "public-name"
        )

    def test_strips_surrounding_whitespace(self):
        router = _FakeRouter(
            _FakeDeployment(_FakeLitellmParams(returned_model_name="  public-name  "))
        )
        assert (
            _resolve_returned_model_name(llm_router=router, model_id="id-1")
            == "public-name"
        )

    # ---- empty / whitespace = unset -------------------------------------
    def test_empty_string_returns_none(self):
        """The whole point: YAML `returned_model_name: ""` must NOT cause
        `model=""` on the wire — resolver returns None so caller falls back."""
        router = _FakeRouter(
            _FakeDeployment(_FakeLitellmParams(returned_model_name=""))
        )
        assert _resolve_returned_model_name(llm_router=router, model_id="id-1") is None

    def test_whitespace_only_returns_none(self):
        router = _FakeRouter(
            _FakeDeployment(_FakeLitellmParams(returned_model_name="   \t  "))
        )
        assert _resolve_returned_model_name(llm_router=router, model_id="id-1") is None

    def test_explicit_none_returns_none(self):
        router = _FakeRouter(
            _FakeDeployment(_FakeLitellmParams(returned_model_name=None))
        )
        assert _resolve_returned_model_name(llm_router=router, model_id="id-1") is None

    def test_non_string_returns_none(self):
        """A YAML scalar that parses to int / bool / list shouldn't trip
        the override on — fall back to client-requested name."""
        router = _FakeRouter(
            _FakeDeployment(_FakeLitellmParams(returned_model_name=123))
        )
        assert _resolve_returned_model_name(llm_router=router, model_id="id-1") is None

    # ---- absent field / container ---------------------------------------
    def test_field_absent_returns_none(self):
        router = _FakeRouter(
            _FakeDeployment(_FakeLitellmParams())  # no returned_model_name
        )
        assert _resolve_returned_model_name(llm_router=router, model_id="id-1") is None

    def test_litellm_params_absent_returns_none(self):
        class _DepNoParams:
            pass

        router = _FakeRouter(_DepNoParams())
        assert _resolve_returned_model_name(llm_router=router, model_id="id-1") is None

    def test_deployment_none_returns_none(self):
        router = _FakeRouter(deployment=None)
        assert _resolve_returned_model_name(llm_router=router, model_id="id-1") is None

    # ---- pre-flight guards -----------------------------------------------
    def test_no_router_returns_none(self):
        assert _resolve_returned_model_name(llm_router=None, model_id="id-1") is None

    def test_no_model_id_returns_none(self):
        router = _FakeRouter(
            _FakeDeployment(_FakeLitellmParams(returned_model_name="public-name"))
        )
        assert _resolve_returned_model_name(llm_router=router, model_id="") is None
        assert _resolve_returned_model_name(llm_router=router, model_id=None) is None

    def test_router_lookup_exception_returns_none(self):
        """A buggy / unconfigured router must not raise into the request
        path — resolver swallows and falls back to default behavior."""
        router = _FakeRouter(raise_exc=True)
        assert _resolve_returned_model_name(llm_router=router, model_id="id-1") is None


class TestGenericLiteLLMParamsField:
    def test_field_default_none(self):
        p = GenericLiteLLMParams(api_key="x")
        assert p.returned_model_name is None

    def test_field_accepts_string(self):
        p = GenericLiteLLMParams(api_key="x", returned_model_name="public-name")
        assert p.returned_model_name == "public-name"


class TestNonStreamingOverride:
    """`_override_openai_response_model(override_model_name=...)` should
    unconditionally stamp the literal value and bypass the requested-model
    preserve heuristics (fallback / Azure router / fastest_response)."""

    def test_override_stamps_dict(self):
        resp = {"model": "claude-sonnet-4-6", "id": "msg_1"}
        _override_openai_response_model(
            response_obj=resp,
            requested_model="claude-sonnet-cache",
            log_context="t",
            override_model_name="public-name",
        )
        assert resp["model"] == "public-name"

    def test_override_strips_whitespace(self):
        resp = {"model": "raw"}
        _override_openai_response_model(
            response_obj=resp,
            requested_model="",
            log_context="t",
            override_model_name="  public-name  ",
        )
        assert resp["model"] == "public-name"

    def test_override_empty_falls_back_to_requested(self):
        """Empty string override → treat as unset; the existing requested-model
        path stamps the requested name."""
        resp = {"model": "claude-sonnet-4-6"}
        _override_openai_response_model(
            response_obj=resp,
            requested_model="claude-sonnet-cache",
            log_context="t",
            override_model_name="",
        )
        assert resp["model"] == "claude-sonnet-cache"

    def test_override_whitespace_only_falls_back_to_requested(self):
        resp = {"model": "claude-sonnet-4-6"}
        _override_openai_response_model(
            response_obj=resp,
            requested_model="claude-sonnet-cache",
            log_context="t",
            override_model_name="   ",
        )
        assert resp["model"] == "claude-sonnet-cache"

    def test_no_override_no_requested_is_noop(self):
        resp = {"model": "claude-sonnet-4-6"}
        _override_openai_response_model(
            response_obj=resp,
            requested_model="",
            log_context="t",
            override_model_name=None,
        )
        # Untouched.
        assert resp["model"] == "claude-sonnet-4-6"

    def test_override_stamps_attribute_object(self):
        class _Obj:
            model = "claude-sonnet-4-6"

        obj = _Obj()
        _override_openai_response_model(
            response_obj=obj,
            requested_model="ignored",
            log_context="t",
            override_model_name="public-name",
        )
        assert obj.model == "public-name"


class TestStreamingResolver:
    """`_get_client_requested_model_for_streaming` must prefer
    `_litellm_returned_model_name` over `_litellm_client_requested_model`
    and the raw request `model`."""

    def test_returned_override_wins(self):
        data = {
            "_litellm_returned_model_name": "public-name",
            "_litellm_client_requested_model": "alias",
            "model": "anthropic/claude-sonnet-4-6",
        }
        assert _get_client_requested_model_for_streaming(data) == "public-name"

    def test_returned_override_strips_whitespace(self):
        data = {"_litellm_returned_model_name": "  public-name  "}
        assert _get_client_requested_model_for_streaming(data) == "public-name"

    def test_empty_override_falls_back_to_requested(self):
        data = {
            "_litellm_returned_model_name": "",
            "_litellm_client_requested_model": "alias",
        }
        assert _get_client_requested_model_for_streaming(data) == "alias"

    def test_whitespace_override_falls_back_to_requested(self):
        data = {
            "_litellm_returned_model_name": "   ",
            "_litellm_client_requested_model": "alias",
        }
        assert _get_client_requested_model_for_streaming(data) == "alias"


class TestChatStreamingChunkRestamp:
    """`_restamp_streaming_chunk_model` should:
    - stamp the chunk model under default behavior;
    - bypass Azure-router / fastest_response preserves when
      `_litellm_returned_model_name` is set."""

    def test_dict_chunk_gets_stamped(self):
        chunk = {"model": "claude-sonnet-4-6", "choices": []}
        out, _ = _restamp_streaming_chunk_model(
            chunk=chunk,
            requested_model_from_client="public-name",
            request_data={"_litellm_returned_model_name": "public-name"},
            model_mismatch_logged=False,
        )
        assert out["model"] == "public-name"

    def test_returned_override_bypasses_fastest_response_preserve(self):
        """Without the override, fastest_response=True would skip stamping;
        with the override the operator's intent wins."""
        chunk = {"model": "raw", "choices": []}
        request_data = {
            "fastest_response": True,
            "_litellm_returned_model_name": "public-name",
        }
        out, _ = _restamp_streaming_chunk_model(
            chunk=chunk,
            requested_model_from_client="public-name",
            request_data=request_data,
            model_mismatch_logged=False,
        )
        assert out["model"] == "public-name"

    def test_fastest_response_preserve_still_active_without_override(self):
        chunk = {"model": "actual-winner", "choices": []}
        out, _ = _restamp_streaming_chunk_model(
            chunk=chunk,
            requested_model_from_client="alias",
            request_data={"fastest_response": True},
            model_mismatch_logged=False,
        )
        # Preserved (default behavior).
        assert out["model"] == "actual-winner"


class TestAnthropicSSEMessageStartRewrite:
    """The Anthropic SSE generator rewrites the nested `message.model` on
    `message_start` only — other events have no model field and must pass
    through unchanged."""

    def _run(self, chunks, request_data):
        """Drive only the per-chunk rewrite block by simulating what the
        generator does inline before serialize_chunk."""
        rewritten = []
        returned_override = request_data.get("_litellm_returned_model_name")
        for c in chunks:
            if (
                isinstance(returned_override, str)
                and returned_override.strip()
                and isinstance(c, dict)
                and c.get("type") == "message_start"
                and isinstance(c.get("message"), dict)
                and "model" in c["message"]
            ):
                c["message"]["model"] = returned_override.strip()
            rewritten.append(c)
        return rewritten

    def test_message_start_model_rewritten(self):
        out = self._run(
            [
                {
                    "type": "message_start",
                    "message": {
                        "model": "claude-sonnet-4-6",
                        "id": "msg_1",
                    },
                },
            ],
            {"_litellm_returned_model_name": "public-name"},
        )
        assert out[0]["message"]["model"] == "public-name"

    def test_other_event_types_untouched(self):
        events = [
            {"type": "content_block_start", "index": 0},
            {"type": "ping"},
            {"type": "content_block_delta", "delta": {"text": "hi"}},
            {"type": "message_stop"},
        ]
        out = self._run(events, {"_litellm_returned_model_name": "public-name"})
        assert out == events

    def test_no_override_passes_through(self):
        evt = {
            "type": "message_start",
            "message": {"model": "claude-sonnet-4-6"},
        }
        out = self._run([evt], {})
        assert out[0]["message"]["model"] == "claude-sonnet-4-6"

    def test_empty_override_passes_through(self):
        evt = {
            "type": "message_start",
            "message": {"model": "claude-sonnet-4-6"},
        }
        out = self._run([evt], {"_litellm_returned_model_name": "   "})
        assert out[0]["message"]["model"] == "claude-sonnet-4-6"


class TestAnthropicSSEBytesRewrite:
    """In practice anthropic /v1/messages streams pass through as raw SSE
    bytes (PassThroughStreamingHandler uses response.aiter_bytes()), so the
    bytes-aware rewrite is the load-bearing path. Single-chunk multi-event
    frames are the common case for Anthropic."""

    def _rewrite(self, raw: bytes, new_model: str) -> bytes:
        return ProxyBaseLLMRequestProcessing._rewrite_message_start_model_in_sse_bytes(
            raw, new_model
        )

    def test_rewrites_nested_message_model(self):
        raw = (
            b"event: message_start\n"
            b'data: {"type":"message_start","message":{"model":"claude-sonnet-4-6","id":"msg_1"}}\n\n'
        )
        out = self._rewrite(raw, "public-name")
        assert b'"model": "public-name"' in out or b'"model":"public-name"' in out
        assert b"claude-sonnet-4-6" not in out

    def test_passes_through_chunks_without_message_start(self):
        raw = (
            b"event: content_block_delta\n"
            b'data: {"type":"content_block_delta","delta":{"text":"hi"}}\n\n'
        )
        assert self._rewrite(raw, "public-name") == raw

    def test_multi_event_chunk_only_rewrites_message_start(self):
        raw = (
            b"event: message_start\n"
            b'data: {"type":"message_start","message":{"model":"upstream","id":"x"}}\n\n'
            b"event: content_block_start\n"
            b'data: {"type":"content_block_start","index":0}\n\n'
        )
        out = self._rewrite(raw, "public-name")
        assert b"upstream" not in out
        # Other event passes through verbatim.
        assert (
            b'"type":"content_block_start"' in out
            or b'"type": "content_block_start"' in out
        )

    def test_non_utf8_bytes_pass_through_unchanged(self):
        raw = b"\xff\xfe not utf-8 \xff"
        assert self._rewrite(raw, "public-name") == raw

    def test_malformed_json_in_data_line_left_alone(self):
        raw = b"event: message_start\n" b"data: {not-json,,,\n\n"
        # No rewrite; original bytes returned untouched.
        assert self._rewrite(raw, "public-name") == raw
