"""Unit tests for litellm_extras.public_req_middleware.PublicReqMiddleware."""

from typing import Any, Dict, List, Optional, Tuple

import pytest

from litellm_extras.public_req_middleware import PublicReqMiddleware


def _build_scope(
    method: str = "GET",
    path: str = "/v1/chat/completions",
    query_string: bytes = b"",
    headers: Optional[List[Tuple[bytes, bytes]]] = None,
) -> Dict[str, Any]:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "query_string": query_string,
        "headers": list(headers or []),
        "client": ("127.0.0.1", 0),
        "server": ("testserver", 80),
        "scheme": "http",
    }


class _RecordingApp:
    """ASGI app that records what scope it received and yields predetermined chunks."""

    def __init__(
        self,
        response_headers: Optional[List[Tuple[bytes, bytes]]] = None,
        body_chunks: Optional[List[bytes]] = None,
        status: int = 200,
    ) -> None:
        self.response_headers = response_headers or []
        self.body_chunks = body_chunks or [b'{"ok":true}']
        self.status = status
        self.received_scope: Optional[Dict[str, Any]] = None

    async def __call__(self, scope: Dict[str, Any], receive: Any, send: Any) -> None:
        self.received_scope = scope
        await send(
            {
                "type": "http.response.start",
                "status": self.status,
                "headers": list(self.response_headers),
            }
        )
        for i, chunk in enumerate(self.body_chunks):
            await send(
                {
                    "type": "http.response.body",
                    "body": chunk,
                    "more_body": i < len(self.body_chunks) - 1,
                }
            )


async def _drive(
    middleware: PublicReqMiddleware,
    scope: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], _RecordingApp]:
    """Run the middleware against an empty-receive client and capture sends."""
    sent: List[Dict[str, Any]] = []

    async def receive() -> Dict[str, Any]:
        return {"type": "http.disconnect"}

    async def send(message: Dict[str, Any]) -> None:
        sent.append(message)

    await middleware(scope, receive, send)
    return sent, middleware.app  # type: ignore[return-value]


def _start_msg(sent: List[Dict[str, Any]]) -> Dict[str, Any]:
    starts = [m for m in sent if m["type"] == "http.response.start"]
    assert starts, f"no response.start in {sent}"
    return starts[0]


def _body_msgs(sent: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [m for m in sent if m["type"] == "http.response.body"]


@pytest.mark.asyncio
async def test_internal_request_passes_through_unchanged() -> None:
    """No X-Public-Req → request scope untouched, response headers preserved."""
    app = _RecordingApp(
        response_headers=[
            (b"content-type", b"application/json"),
            (b"x-litellm-model-id", b"gpt-4o-mini-internal"),
            (b"x-litellm-cache-hit", b"true"),
        ]
    )
    mw = PublicReqMiddleware(app)
    scope = _build_scope(
        headers=[
            (b"authorization", b"Bearer sk-internal"),
            (b"x-litellm-mock-response", b"trace:xyz"),
            (b"x-litellm-tags", b"team:alpha"),
        ]
    )

    sent, _ = await _drive(mw, scope)

    received_hdrs = dict(app.received_scope["headers"])
    assert received_hdrs.get(b"x-litellm-mock-response") == b"trace:xyz"
    assert received_hdrs.get(b"x-litellm-tags") == b"team:alpha"

    resp_hdrs = dict(_start_msg(sent)["headers"])
    assert resp_hdrs.get(b"x-litellm-model-id") == b"gpt-4o-mini-internal"
    assert resp_hdrs.get(b"x-litellm-cache-hit") == b"true"


@pytest.mark.asyncio
async def test_public_request_strips_inbound_litellm_headers() -> None:
    app = _RecordingApp()
    mw = PublicReqMiddleware(app)
    scope = _build_scope(
        headers=[
            (b"authorization", b"Bearer sk-user"),
            (b"x-public-req", b"1"),
            (b"x-litellm-mock-response", b"hack"),
            (b"x-litellm-num-retries", b"50"),
            (b"x-litellm-tags", b"team:victim"),
            (b"content-type", b"application/json"),
        ]
    )

    await _drive(mw, scope)

    received = dict(app.received_scope["headers"])
    assert b"x-litellm-mock-response" not in received
    assert b"x-litellm-num-retries" not in received
    assert b"x-litellm-tags" not in received
    # X-Public-Req itself is stripped too (don't surface the marker to LiteLLM).
    assert b"x-public-req" not in received
    # Non-LiteLLM headers must survive.
    assert received[b"authorization"] == b"Bearer sk-user"
    assert received[b"content-type"] == b"application/json"


@pytest.mark.asyncio
async def test_public_request_strips_outbound_litellm_headers() -> None:
    app = _RecordingApp(
        response_headers=[
            (b"content-type", b"application/json"),
            (b"x-litellm-model-id", b"gpt-4o-mini-leak"),
            (b"x-litellm-cache-hit", b"true"),
            (b"x-litellm-response-cost", b"0.0001"),
            (b"x-other", b"keep"),
        ]
    )
    mw = PublicReqMiddleware(app)
    scope = _build_scope(headers=[(b"x-public-req", b"1")])

    sent, _ = await _drive(mw, scope)

    resp = dict(_start_msg(sent)["headers"])
    assert b"x-litellm-model-id" not in resp
    assert b"x-litellm-cache-hit" not in resp
    assert b"x-litellm-response-cost" not in resp
    assert resp[b"x-other"] == b"keep"


@pytest.mark.asyncio
async def test_public_request_streaming_body_chunks_pass_through_unbuffered() -> None:
    """Body chunks must reach send() one-by-one without coalescing.

    Regression guard for the BaseHTTPMiddleware trap: a misimplemented
    middleware would buffer the whole body before forwarding. We assert that
    each chunk produced by the inner app surfaces as a separate
    http.response.body message in original order.
    """
    chunks = [
        b'data: {"a":1}\n\n',
        b'data: {"b":2}\n\n',
        b'data: {"c":3}\n\n',
        b"data: [DONE]\n\n",
    ]
    app = _RecordingApp(
        response_headers=[(b"content-type", b"text/event-stream")],
        body_chunks=chunks,
    )
    mw = PublicReqMiddleware(app)
    scope = _build_scope(headers=[(b"x-public-req", b"1")])

    sent, _ = await _drive(mw, scope)

    body_messages = _body_msgs(sent)
    assert len(body_messages) == len(chunks)
    assert [m["body"] for m in body_messages] == chunks
    # All but the last chunk must signal more_body=True.
    assert [m.get("more_body", False) for m in body_messages] == [
        True,
        True,
        True,
        False,
    ]


@pytest.mark.asyncio
async def test_public_request_strips_forbidden_models_query() -> None:
    """Public mode silently drops forbidden keys and proxies the rest."""
    app = _RecordingApp()
    mw = PublicReqMiddleware(app)
    scope = _build_scope(
        method="GET",
        path="/v1/models",
        query_string=b"include_metadata=true&fallback_type=general&team_id=keep",
        headers=[(b"x-public-req", b"1")],
    )

    sent, _ = await _drive(mw, scope)

    assert _start_msg(sent)["status"] == 200, "request must reach inner app"
    assert app.received_scope is not None
    forwarded_qs = app.received_scope["query_string"]
    assert b"include_metadata" not in forwarded_qs
    assert b"fallback_type" not in forwarded_qs
    assert b"team_id=keep" in forwarded_qs


@pytest.mark.asyncio
async def test_public_request_strip_leaves_query_when_only_forbidden_keys() -> None:
    """If every query key was forbidden, the forwarded query is empty."""
    app = _RecordingApp()
    mw = PublicReqMiddleware(app)
    scope = _build_scope(
        method="GET",
        path="/v1/models",
        query_string=b"include_metadata=true&only_model_access_groups=1",
        headers=[(b"x-public-req", b"1")],
    )

    sent, _ = await _drive(mw, scope)

    assert _start_msg(sent)["status"] == 200
    assert app.received_scope is not None
    assert app.received_scope["query_string"] == b""


@pytest.mark.asyncio
async def test_public_request_models_clean_query_unchanged() -> None:
    """Safe query strings must be passed through byte-for-byte."""
    app = _RecordingApp()
    mw = PublicReqMiddleware(app)
    scope = _build_scope(
        method="GET",
        path="/v1/models",
        query_string=b"team_id=alpha&scope=expand",
        headers=[(b"x-public-req", b"1")],
    )

    sent, _ = await _drive(mw, scope)

    assert _start_msg(sent)["status"] == 200
    assert app.received_scope["query_string"] == b"team_id=alpha&scope=expand"


@pytest.mark.asyncio
async def test_public_request_allows_models_without_forbidden_query() -> None:
    app = _RecordingApp()
    mw = PublicReqMiddleware(app)
    scope = _build_scope(
        method="GET",
        path="/v1/models",
        query_string=b"",
        headers=[(b"x-public-req", b"1")],
    )

    sent, _ = await _drive(mw, scope)

    assert _start_msg(sent)["status"] == 200
    assert app.received_scope is not None


@pytest.mark.asyncio
async def test_internal_request_keeps_forbidden_query_on_models() -> None:
    app = _RecordingApp()
    mw = PublicReqMiddleware(app)
    scope = _build_scope(
        method="GET",
        path="/v1/models",
        query_string=b"include_metadata=true",
        headers=[],
    )

    sent, _ = await _drive(mw, scope)

    assert _start_msg(sent)["status"] == 200


@pytest.mark.asyncio
async def test_marker_value_other_than_1_treated_as_internal() -> None:
    """Defensive: only the literal "1" enables public mode."""
    app = _RecordingApp()
    mw = PublicReqMiddleware(app)
    scope = _build_scope(
        headers=[
            (b"x-public-req", b"true"),
            (b"x-litellm-tags", b"team:keep"),
        ]
    )

    await _drive(mw, scope)

    received = dict(app.received_scope["headers"])
    assert received.get(b"x-litellm-tags") == b"team:keep"


@pytest.mark.asyncio
async def test_marker_is_case_insensitive_for_header_name() -> None:
    """ASGI lowercases header names by spec, but defensive parsing matters."""
    app = _RecordingApp()
    mw = PublicReqMiddleware(app)
    # ASGI gives us lowercase, but we double-check the comparison anyway.
    scope = _build_scope(
        headers=[
            (b"X-Public-Req", b"1"),  # mixed case
            (b"X-Litellm-Tags", b"team:victim"),
        ]
    )

    await _drive(mw, scope)

    received = dict(app.received_scope["headers"])
    # Mixed-case x-litellm-tags should still be stripped.
    assert b"X-Litellm-Tags" not in received
    assert b"x-litellm-tags" not in received


@pytest.mark.asyncio
async def test_non_http_scope_is_passed_through() -> None:
    """Websocket and lifespan must reach the inner app untouched."""
    app = _RecordingApp()
    mw = PublicReqMiddleware(app)
    scope = {"type": "websocket", "path": "/v1/realtime", "headers": []}

    async def receive() -> Dict[str, Any]:
        return {"type": "websocket.connect"}

    async def send(message: Dict[str, Any]) -> None:
        return None

    await mw(scope, receive, send)

    assert app.received_scope is scope


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
