"""Internal LiteLLM extensions kept out of the upstream tree.

This package hosts deployment-specific middleware and entrypoints layered on
top of the vendored LiteLLM source. Modules here MUST NOT be imported by any
file under ``litellm/`` itself — that would couple upstream code to internal
add-ons and complicate future rebases.
"""
