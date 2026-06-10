"""Internal LiteLLM extensions kept out of the upstream tree.

This package hosts deployment-specific middleware and entrypoints layered on
top of the vendored LiteLLM source. Modules here MUST NOT be imported by any
file under ``litellm/`` itself — that would couple upstream code to internal
add-ons and complicate future rebases.

Public symbols re-exported here are wiring points consumed by
``litellm/proxy/proxy_server.py`` startup; importing the names below is the
only sanctioned coupling between core and extras and it happens lazily so
the core package stays usable without these modules on PYTHONPATH.
"""

from litellm_extras.time_weighted_router import TimeWeightedRouter, install

__all__ = ["TimeWeightedRouter", "install"]
