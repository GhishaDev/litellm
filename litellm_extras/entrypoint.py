"""Wrapper entrypoint that installs internal middleware before the LiteLLM CLI runs.

Invoke this module in place of the plain ``litellm`` console script::

    python -m litellm_extras.entrypoint --config=/app/config.yaml --port=4000

It works by importing :mod:`litellm.proxy.proxy_server` first (which has the
side effect of constructing the FastAPI ``app`` instance), attaching
:class:`~litellm_extras.public_req_middleware.PublicReqMiddleware`, and only
then delegating to the existing Click-based ``run_server`` CLI. Because
Python caches imported modules, the subsequent ``from .proxy_server import
app`` inside ``run_server`` returns the same ``app`` object with our
middleware already inserted.

``FastAPI.add_middleware`` prepends to ``app.user_middleware``, so calling it
after the proxy's own ``add_middleware`` lines puts ours on the OUTSIDE —
which is what we want so the header strip runs before any LiteLLM
auth/logging layer.
"""

import sys

from litellm.proxy import proxy_server  # noqa: E402  (load order is intentional)
from litellm.proxy.proxy_cli import run_server  # noqa: E402

from litellm_extras.public_req_middleware import PublicReqMiddleware


def install_middleware() -> None:
    """Attach internal middleware to the proxy's FastAPI app.

    Idempotent: if PublicReqMiddleware is already present we skip the
    insert. This makes the entrypoint safe to invoke from tests that may
    have imported ``litellm.proxy.proxy_server`` earlier.
    """
    existing = {
        m.cls.__name__ for m in proxy_server.app.user_middleware if hasattr(m, "cls")
    }
    if PublicReqMiddleware.__name__ in existing:
        return
    proxy_server.app.add_middleware(PublicReqMiddleware)


def main() -> int:
    install_middleware()
    run_server.main(args=sys.argv[1:], standalone_mode=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
