import datetime as real_datetime
import json
import os
import sys

import pytest
from fastapi import HTTPException

from litellm.caching.caching import DualCache
from litellm.proxy._types import ProxyErrorTypes
from litellm.proxy.utils import ProxyLogging

sys.path.insert(
    0, os.path.abspath("../../..")
)  # Adds the parent directory to the system path


from unittest.mock import MagicMock

from litellm.proxy.utils import get_custom_url, join_paths


def test_get_custom_url(monkeypatch):
    monkeypatch.setenv("SERVER_ROOT_PATH", "/litellm")
    custom_url = get_custom_url(request_base_url="http://0.0.0.0:4000", route="ui/")
    assert custom_url == "http://0.0.0.0:4000/litellm/ui/"


def test_proxy_only_error_true_for_llm_route():
    proxy_logging_obj = ProxyLogging(user_api_key_cache=DualCache())
    assert proxy_logging_obj._is_proxy_only_llm_api_error(
        original_exception=Exception(),
        error_type=ProxyErrorTypes.auth_error,
        route="/v1/chat/completions",
    )


def test_proxy_only_error_true_for_info_route():
    proxy_logging_obj = ProxyLogging(user_api_key_cache=DualCache())
    assert (
        proxy_logging_obj._is_proxy_only_llm_api_error(
            original_exception=Exception(),
            error_type=ProxyErrorTypes.auth_error,
            route="/key/info",
        )
        is True
    )


def test_proxy_only_error_false_for_non_llm_non_info_route():
    proxy_logging_obj = ProxyLogging(user_api_key_cache=DualCache())
    assert (
        proxy_logging_obj._is_proxy_only_llm_api_error(
            original_exception=Exception(),
            error_type=ProxyErrorTypes.auth_error,
            route="/key/generate",
        )
        is False
    )


def test_proxy_only_error_false_for_other_error_type():
    proxy_logging_obj = ProxyLogging(user_api_key_cache=DualCache())
    assert (
        proxy_logging_obj._is_proxy_only_llm_api_error(
            original_exception=Exception(),
            error_type=None,
            route="/v1/chat/completions",
        )
        is False
    )


def test_get_model_group_info_order():
    from litellm import Router
    from litellm.proxy.proxy_server import _get_model_group_info

    router = Router(
        model_list=[
            {
                "model_name": "openai/tts-1",
                "litellm_params": {
                    "model": "openai/tts-1",
                    "api_key": "sk-1234",
                },
            },
            {
                "model_name": "openai/gpt-3.5-turbo",
                "litellm_params": {
                    "model": "openai/gpt-3.5-turbo",
                    "api_key": "sk-1234",
                },
            },
        ]
    )
    model_list = _get_model_group_info(
        llm_router=router,
        all_models_str=["openai/tts-1", "openai/gpt-3.5-turbo"],
        model_group=None,
    )

    model_groups = [m.model_group for m in model_list]
    assert model_groups == ["openai/tts-1", "openai/gpt-3.5-turbo"]


def test_join_paths_no_duplication():
    """Test that join_paths doesn't duplicate route when base_path already ends with it"""
    result = join_paths(
        base_path="http://0.0.0.0:4000/my-custom-path/", route="/my-custom-path"
    )
    assert result == "http://0.0.0.0:4000/my-custom-path"


def test_join_paths_normal_join():
    """Test normal path joining"""
    result = join_paths(base_path="http://0.0.0.0:4000", route="/api/v1")
    assert result == "http://0.0.0.0:4000/api/v1"


def test_join_paths_with_trailing_slash():
    """Test path joining with trailing slash on base_path"""
    result = join_paths(base_path="http://0.0.0.0:4000/", route="api/v1")
    assert result == "http://0.0.0.0:4000/api/v1"


def test_join_paths_empty_base():
    """Test path joining with empty base_path"""
    result = join_paths(base_path="", route="api/v1")
    assert result == "/api/v1"


def test_join_paths_empty_route():
    """Test path joining with empty route"""
    result = join_paths(base_path="http://0.0.0.0:4000", route="")
    assert result == "http://0.0.0.0:4000"


def test_join_paths_both_empty():
    """Test path joining with both empty"""
    result = join_paths(base_path="", route="")
    assert result == "/"


def test_join_paths_nested_path():
    """Test path joining with nested paths"""
    result = join_paths(base_path="http://0.0.0.0:4000/v1", route="chat/completions")
    assert result == "http://0.0.0.0:4000/v1/chat/completions"


def _patch_today(monkeypatch, year, month, day):
    class PatchedDate(real_datetime.date):
        @classmethod
        def today(cls):
            return real_datetime.date(year, month, day)

    monkeypatch.setattr("litellm.proxy.utils.date", PatchedDate)


def test_get_projected_spend_over_limit_day_one(monkeypatch):
    from litellm.proxy.utils import _get_projected_spend_over_limit

    _patch_today(monkeypatch, 2026, 1, 1)
    result = _get_projected_spend_over_limit(100.0, 1.0)

    assert result is not None
    projected_spend, projected_exceeded_date = result
    assert projected_spend == 3100.0
    assert projected_exceeded_date == real_datetime.date(2026, 1, 1)


def test_get_projected_spend_over_limit_december(monkeypatch):
    from litellm.proxy.utils import _get_projected_spend_over_limit

    _patch_today(monkeypatch, 2026, 12, 15)
    result = _get_projected_spend_over_limit(100.0, 1.0)

    assert result is not None
    projected_spend, projected_exceeded_date = result
    assert projected_spend == pytest.approx(214.28571428571428)
    assert projected_exceeded_date == real_datetime.date(2026, 12, 15)


def test_get_projected_spend_over_limit_includes_current_spend(monkeypatch):
    from litellm.proxy.utils import _get_projected_spend_over_limit

    _patch_today(monkeypatch, 2026, 4, 11)
    result = _get_projected_spend_over_limit(100.0, 200.0)

    assert result is not None
    projected_spend, projected_exceeded_date = result
    assert projected_spend == 290.0
    assert projected_exceeded_date == real_datetime.date(2026, 4, 21)


# ---------------------------------------------------------------------------
# L2: _enrich_http_exception_with_guardrail_context
# Regression coverage for case 2026-04-10-internal-bedrock-guardrail-streaming-error.
# ---------------------------------------------------------------------------


def test_enrich_http_exception_with_guardrail_context_dict_detail():
    """L2: dict-detail HTTPException is enriched with guardrail_name and mode."""
    from litellm.proxy.utils import _enrich_http_exception_with_guardrail_context

    class StubCallback:
        guardrail_name = "bedrock-pii-guard"
        event_hook = "post_call"

    exc = HTTPException(status_code=400, detail={"error": "Violated guardrail policy"})
    _enrich_http_exception_with_guardrail_context(exc, StubCallback())
    assert exc.detail["guardrail_name"] == "bedrock-pii-guard"
    assert exc.detail["guardrail_mode"] == "post_call"


def test_enrich_http_exception_string_detail_noop():
    """L2: string-detail HTTPException is not mutated (can't add fields to a str)."""
    from litellm.proxy.utils import _enrich_http_exception_with_guardrail_context

    class StubCallback:
        guardrail_name = "x"
        event_hook = "pre_call"

    exc = HTTPException(status_code=400, detail="Content blocked")
    _enrich_http_exception_with_guardrail_context(exc, StubCallback())
    assert exc.detail == "Content blocked"


def test_enrich_http_exception_setdefault_does_not_overwrite():
    """L2: a guardrail that already populates guardrail_name explicitly wins."""
    from litellm.proxy.utils import _enrich_http_exception_with_guardrail_context

    class StubCallback:
        guardrail_name = "inferred-name"
        event_hook = "pre_call"

    exc = HTTPException(
        status_code=400,
        detail={"error": "x", "guardrail_name": "explicit-name"},
    )
    _enrich_http_exception_with_guardrail_context(exc, StubCallback())
    assert exc.detail["guardrail_name"] == "explicit-name"


def test_enrich_http_exception_non_http_exception_noop():
    """L2: non-HTTPException is left alone and the helper does not raise."""
    from litellm.proxy.utils import _enrich_http_exception_with_guardrail_context

    class StubCallback:
        guardrail_name = "x"
        event_hook = "pre_call"

    exc = ValueError("not an HTTPException")
    _enrich_http_exception_with_guardrail_context(exc, StubCallback())
    assert str(exc) == "not an HTTPException"


def test_enrich_http_exception_callback_without_guardrail_name_noop():
    """L2: callback without guardrail_name attribute leaves detail alone."""
    from litellm.proxy.utils import _enrich_http_exception_with_guardrail_context

    class StubCallback:
        pass

    exc = HTTPException(status_code=400, detail={"error": "x"})
    _enrich_http_exception_with_guardrail_context(exc, StubCallback())
    assert exc.detail == {"error": "x"}


# -----------------------------------------------------------------------------
# _apply_user_models_filter — per-user restriction on /v1/models listing.
# Parity with can_user_call_model at inference time (BerriAI/litellm#26420).
# -----------------------------------------------------------------------------


def _make_dict():
    """Stand-in for `UserAPIKeyAuth` — `_apply_user_models_filter` only reads
    `.user_id`, so an attribute-bearing object is enough and avoids pulling
    the full Pydantic model into every test.
    """

    class _UAK:
        def __init__(self, user_id):
            self.user_id = user_id

    return _UAK


@pytest.mark.asyncio
async def test_apply_user_models_filter_no_user_id_skips_filter(monkeypatch):
    """Master key / service account → user_id is None → no filter."""
    from litellm.proxy import utils as proxy_utils

    UAK = _make_dict()

    async def _should_not_be_called(*args, **kwargs):
        raise AssertionError("get_user_object must not be called when user_id is None")

    monkeypatch.setattr(
        "litellm.proxy.auth.auth_checks.get_user_object",
        _should_not_be_called,
    )

    result = await proxy_utils._apply_user_models_filter(
        all_models=["m1", "m2"],
        user_api_key_dict=UAK(user_id=None),
        proxy_model_list=["m1", "m2"],
        model_access_groups={},
        prisma_client=MagicMock(),
        proxy_logging_obj=None,
        user_api_key_cache=DualCache(),
    )
    assert result == ["m1", "m2"]


@pytest.mark.asyncio
async def test_apply_user_models_filter_no_prisma_skips_filter(monkeypatch):
    """No DB connection → return list unchanged."""
    from litellm.proxy import utils as proxy_utils

    UAK = _make_dict()
    result = await proxy_utils._apply_user_models_filter(
        all_models=["m1", "m2"],
        user_api_key_dict=UAK(user_id="u1"),
        proxy_model_list=["m1", "m2"],
        model_access_groups={},
        prisma_client=None,
        proxy_logging_obj=None,
        user_api_key_cache=DualCache(),
    )
    assert result == ["m1", "m2"]


@pytest.mark.asyncio
async def test_apply_user_models_filter_user_obj_none_skips_filter(monkeypatch):
    """user_id present but DB returns no row → no filter."""
    from litellm.proxy import utils as proxy_utils

    UAK = _make_dict()

    async def _none_user(*args, **kwargs):
        return None

    monkeypatch.setattr(
        "litellm.proxy.auth.auth_checks.get_user_object",
        _none_user,
    )

    result = await proxy_utils._apply_user_models_filter(
        all_models=["m1", "m2"],
        user_api_key_dict=UAK(user_id="u-missing"),
        proxy_model_list=["m1", "m2"],
        model_access_groups={},
        prisma_client=MagicMock(),
        proxy_logging_obj=None,
        user_api_key_cache=DualCache(),
    )
    assert result == ["m1", "m2"]


@pytest.mark.asyncio
async def test_apply_user_models_filter_empty_user_models_skips_filter(monkeypatch):
    """user.models == [] → unrestricted → no filter."""
    from litellm.proxy import utils as proxy_utils
    from litellm.proxy._types import LiteLLM_UserTable

    UAK = _make_dict()

    async def _user(*args, **kwargs):
        return LiteLLM_UserTable(
            user_id="u1", max_budget=None, user_email=None, models=[]
        )

    monkeypatch.setattr(
        "litellm.proxy.auth.auth_checks.get_user_object",
        _user,
    )
    result = await proxy_utils._apply_user_models_filter(
        all_models=["m1", "m2"],
        user_api_key_dict=UAK(user_id="u1"),
        proxy_model_list=["m1", "m2"],
        model_access_groups={},
        prisma_client=MagicMock(),
        proxy_logging_obj=None,
        user_api_key_cache=DualCache(),
    )
    assert result == ["m1", "m2"]


@pytest.mark.asyncio
async def test_apply_user_models_filter_no_default_models_returns_empty(monkeypatch):
    """`no-default-models` sentinel → /v1/models returns [] (matches 401 inference)."""
    from litellm.proxy import utils as proxy_utils
    from litellm.proxy._types import LiteLLM_UserTable

    UAK = _make_dict()

    async def _user(*args, **kwargs):
        return LiteLLM_UserTable(
            user_id="u1",
            max_budget=None,
            user_email=None,
            models=["no-default-models"],
        )

    monkeypatch.setattr(
        "litellm.proxy.auth.auth_checks.get_user_object",
        _user,
    )
    result = await proxy_utils._apply_user_models_filter(
        all_models=["m1", "m2"],
        user_api_key_dict=UAK(user_id="u1"),
        proxy_model_list=["m1", "m2"],
        model_access_groups={},
        prisma_client=MagicMock(),
        proxy_logging_obj=None,
        user_api_key_cache=DualCache(),
    )
    assert result == []


@pytest.mark.asyncio
async def test_apply_user_models_filter_all_proxy_models_no_filter(monkeypatch):
    """`all-proxy-models` sentinel → no filter."""
    from litellm.proxy import utils as proxy_utils
    from litellm.proxy._types import LiteLLM_UserTable

    UAK = _make_dict()

    async def _user(*args, **kwargs):
        return LiteLLM_UserTable(
            user_id="u1",
            max_budget=None,
            user_email=None,
            models=["all-proxy-models"],
        )

    monkeypatch.setattr(
        "litellm.proxy.auth.auth_checks.get_user_object",
        _user,
    )
    result = await proxy_utils._apply_user_models_filter(
        all_models=["m1", "m2", "m3"],
        user_api_key_dict=UAK(user_id="u1"),
        proxy_model_list=["m1", "m2", "m3"],
        model_access_groups={},
        prisma_client=MagicMock(),
        proxy_logging_obj=None,
        user_api_key_cache=DualCache(),
    )
    assert result == ["m1", "m2", "m3"]


@pytest.mark.asyncio
async def test_apply_user_models_filter_restrictive_intersect(monkeypatch):
    """The bug from #26420: user.models is a strict subset → filter narrows."""
    from litellm.proxy import utils as proxy_utils
    from litellm.proxy._types import LiteLLM_UserTable

    UAK = _make_dict()

    async def _user(*args, **kwargs):
        return LiteLLM_UserTable(
            user_id="u1",
            max_budget=None,
            user_email=None,
            models=["claude-3-opus"],
        )

    monkeypatch.setattr(
        "litellm.proxy.auth.auth_checks.get_user_object",
        _user,
    )
    result = await proxy_utils._apply_user_models_filter(
        all_models=["gpt-4", "claude-3-opus", "claude-3-haiku"],
        user_api_key_dict=UAK(user_id="u1"),
        proxy_model_list=["gpt-4", "claude-3-opus", "claude-3-haiku"],
        model_access_groups={},
        prisma_client=MagicMock(),
        proxy_logging_obj=None,
        user_api_key_cache=DualCache(),
    )
    assert result == ["claude-3-opus"]


@pytest.mark.asyncio
async def test_apply_user_models_filter_access_group_expansion(monkeypatch):
    """user.models lists a group → expanded then intersected."""
    from litellm.proxy import utils as proxy_utils
    from litellm.proxy._types import LiteLLM_UserTable

    UAK = _make_dict()

    async def _user(*args, **kwargs):
        return LiteLLM_UserTable(
            user_id="u1",
            max_budget=None,
            user_email=None,
            models=["common-models"],
        )

    monkeypatch.setattr(
        "litellm.proxy.auth.auth_checks.get_user_object",
        _user,
    )
    result = await proxy_utils._apply_user_models_filter(
        all_models=["gpt-4", "claude-3-opus", "claude-3-haiku"],
        user_api_key_dict=UAK(user_id="u1"),
        proxy_model_list=["gpt-4", "claude-3-opus", "claude-3-haiku"],
        model_access_groups={
            "common-models": ["gpt-4", "claude-3-haiku"],
        },
        prisma_client=MagicMock(),
        proxy_logging_obj=None,
        user_api_key_cache=DualCache(),
    )
    assert result == ["gpt-4", "claude-3-haiku"]


@pytest.mark.asyncio
async def test_apply_user_models_filter_wildcard(monkeypatch):
    """`anthropic/*` in user.models → keep all anthropic/* in the list."""
    from litellm.proxy import utils as proxy_utils
    from litellm.proxy._types import LiteLLM_UserTable

    UAK = _make_dict()

    async def _user(*args, **kwargs):
        return LiteLLM_UserTable(
            user_id="u1",
            max_budget=None,
            user_email=None,
            models=["anthropic/*"],
        )

    monkeypatch.setattr(
        "litellm.proxy.auth.auth_checks.get_user_object",
        _user,
    )
    result = await proxy_utils._apply_user_models_filter(
        all_models=[
            "anthropic/claude-3-opus",
            "anthropic/claude-3-haiku",
            "openai/gpt-4",
        ],
        user_api_key_dict=UAK(user_id="u1"),
        proxy_model_list=[
            "anthropic/claude-3-opus",
            "anthropic/claude-3-haiku",
            "openai/gpt-4",
        ],
        model_access_groups={},
        prisma_client=MagicMock(),
        proxy_logging_obj=None,
        user_api_key_cache=DualCache(),
    )
    assert result == ["anthropic/claude-3-opus", "anthropic/claude-3-haiku"]


@pytest.mark.asyncio
async def test_apply_user_models_filter_get_user_object_raises_skips_filter(
    monkeypatch,
):
    """If user lookup blips, we must not break /v1/models — just skip the filter."""
    from litellm.proxy import utils as proxy_utils

    UAK = _make_dict()

    async def _raise(*args, **kwargs):
        raise RuntimeError("db blip")

    monkeypatch.setattr(
        "litellm.proxy.auth.auth_checks.get_user_object",
        _raise,
    )
    result = await proxy_utils._apply_user_models_filter(
        all_models=["m1", "m2"],
        user_api_key_dict=UAK(user_id="u1"),
        proxy_model_list=["m1", "m2"],
        model_access_groups={},
        prisma_client=MagicMock(),
        proxy_logging_obj=None,
        user_api_key_cache=DualCache(),
    )
    assert result == ["m1", "m2"]
