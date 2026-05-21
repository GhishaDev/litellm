import os

import importlib_metadata

try:
    _pkg_version = importlib_metadata.version("litellm")
except Exception:
    _pkg_version = "unknown"

# Build-time overrides injected by the Docker build (see Dockerfile).
# LITELLM_BUILD_TAG e.g. "v1.83.10-internal.5" — the immutable release tag.
# LITELLM_BUILD_SHA e.g. "7fd6dcb"               — short git sha of the build.
# These let an internal fork surface its own version in the UI / logs / metrics
# without mutating pyproject.toml (which would conflict on every upstream sync).
_build_tag = os.getenv("LITELLM_BUILD_TAG", "").strip()
_build_sha = os.getenv("LITELLM_BUILD_SHA", "").strip()

# Primary version string used everywhere (User-Agent headers, /health/readiness,
# UI navbar). Prefers the build tag when set, falls back to package metadata.
version: str = _build_tag or _pkg_version

# Upstream package version (always from importlib.metadata). Use this for any
# code that needs the canonical "what upstream litellm version are we based on"
# — feature-flag checks, compatibility gates, etc.
build_base_version: str = _pkg_version

# Short git sha of the build, empty string if not injected.
build_sha: str = _build_sha
