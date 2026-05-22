"""ASGI middleware that gates public-vs-internal request behavior.

The public Nginx ingress injects ``X-Public-Req: 1`` on every request it
forwards. Internal callers reach the proxy directly (e.g. via the in-cluster
Service ``litellm-internal``) and never carry that header. This middleware
keys off the header to apply two safeguards for public requests only:

1.  Strip every ``x-litellm-*`` request header — these headers (notably
    ``x-litellm-api-key``, ``x-litellm-mock-response``,
    ``x-litellm-num-retries``) let callers override internal proxy behavior
    and must never be honored from untrusted sources.

2.  Silently strip sensitive query parameters from ``/v1/models`` and
    ``/models`` requests (``include_metadata``, ``fallback_type``,
    ``include_model_access_groups``, ``only_model_access_groups``). These
    parameters expose router fallback chains and access-group naming,
    which are deployment-internal details. The request still reaches the
    inner app and returns a normal 200 with the redacted view, so naive
    public clients that always set these params do not break.

For public responses the middleware additionally strips every
``x-litellm-*`` response header (model deployment IDs, cache-hit flags,
cost/budget annotations) before they leave the proxy.

Internal requests (no header / header != ``"1"``) are passed through
untouched so internal services can keep using the override headers and
observability fields.

The middleware is pure-ASGI (not Starlette ``BaseHTTPMiddleware``). It does
not buffer response bodies, so streaming endpoints (chat completions with
``stream=true``, ``/v1/messages``, ``/v1/realtime``) keep their original
time-to-first-byte profile.
"""

from typing import Iterable, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode

from starlette.types import ASGIApp, Message, Receive, Scope, Send

LITELLM_HEADER_PREFIX = b"x-litellm-"
PUBLIC_REQ_HEADER = b"x-public-req"
PUBLIC_REQ_VALUE = b"1"

MODELS_PATHS = frozenset({"/v1/models", "/models"})
MODELS_FORBIDDEN_QUERY_KEYS = frozenset(
    {
        "include_metadata",
        "fallback_type",
        "include_model_access_groups",
        "only_model_access_groups",
    }
)


class PublicReqMiddleware:
    """Apply public-request safeguards keyed off ``X-Public-Req``.

    Install order matters: this middleware should be the OUTERMOST layer so
    its header strip runs before any LiteLLM auth/logging middleware reads
    the request. ``FastAPI.add_middleware`` inserts at index 0, so calling
    it after the proxy's own ``add_middleware`` lines puts this on the
    outside automatically.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers: List[Tuple[bytes, bytes]] = list(scope.get("headers") or [])
        if not self._is_public(headers):
            await self.app(scope, receive, send)
            return

        scope = dict(scope)

        path = scope.get("path", "")
        if path in MODELS_PATHS:
            sanitized = self._strip_forbidden_query(scope.get("query_string", b""))
            if sanitized is not None:
                scope["query_string"] = sanitized

        scope["headers"] = [
            (name, value)
            for name, value in headers
            if not name.lower().startswith(LITELLM_HEADER_PREFIX)
            and name.lower() != PUBLIC_REQ_HEADER
        ]

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                message = {
                    **message,
                    "headers": [
                        (name, value)
                        for name, value in message.get("headers", [])
                        if not name.lower().startswith(LITELLM_HEADER_PREFIX)
                    ],
                }
            await send(message)

        await self.app(scope, receive, send_wrapper)

    @staticmethod
    def _is_public(headers: Iterable[Tuple[bytes, bytes]]) -> bool:
        for name, value in headers:
            if name.lower() == PUBLIC_REQ_HEADER:
                return value.strip() == PUBLIC_REQ_VALUE
        return False

    @staticmethod
    def _strip_forbidden_query(query_string: bytes) -> Optional[bytes]:
        """Remove forbidden keys from a percent-encoded query string.

        Returns the rewritten query string (possibly empty) when at least
        one forbidden key was present, or ``None`` if the query string was
        already safe. Returning ``None`` lets callers skip the
        ``scope["query_string"]`` write and keep the original bytes
        untouched — preserving exact ordering and any odd encoding the
        client may have sent.
        """
        if not query_string:
            return None
        pairs = parse_qsl(
            query_string.decode("latin-1"),
            keep_blank_values=True,
        )
        kept = [(k, v) for k, v in pairs if k not in MODELS_FORBIDDEN_QUERY_KEYS]
        if len(kept) == len(pairs):
            return None
        return urlencode(kept, doseq=True).encode("latin-1")
