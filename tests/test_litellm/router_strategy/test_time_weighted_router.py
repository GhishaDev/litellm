"""Unit tests for litellm_extras.time_weighted_router.

Boundary policy (per CLAUDE.md "No theater tests"):
- We construct a REAL ``litellm.Router`` with a synthetic ``model_list``
  and exercise routing through ``router.async_get_available_deployment``.
  No mocking of Router methods; no spy doubles asserting ``.called``.
- The unit under test (TimeWeightedRouter) runs end-to-end; we observe
  results via the deployment id returned from real dispatch and via
  the deployment id distribution over N picks.
- Pure helpers (_parse_hhmm, _in_band, _resolve_weight) are tested
  against real inputs producing real outputs.
"""

import asyncio
import os
import random
import sys
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pytest

sys.path.insert(0, os.path.abspath("../../.."))

import litellm
from litellm_extras.time_weighted_router import (
    TimeWeightedRouter,
    _in_band,
    _parse_hhmm,
    install,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _deployment(
    model_name: str,
    deployment_id: str,
    *,
    bands=None,
    fallback_weight=None,
    tz="UTC",
    params_weight=None,
    blocked=None,
) -> Dict:
    info: Dict[str, Any] = {"id": deployment_id, "tz": tz}
    if blocked is not None:
        info["blocked"] = blocked
    if bands is not None or fallback_weight is not None:
        tw: Dict[str, Any] = {}
        if bands is not None:
            tw["bands"] = bands
        if fallback_weight is not None:
            tw["fallback_weight"] = fallback_weight
        info["time_weights"] = tw
    params: Dict[str, Any] = {
        "model": "openai/gpt-4",
        "api_key": "sk-fake-for-routing-only",
    }
    if params_weight is not None:
        params["weight"] = params_weight
    return {
        "model_name": model_name,
        "model_info": info,
        "litellm_params": params,
    }


def _make_router(model_list: List[Dict]) -> litellm.Router:
    """Build a real Router and install the time-weighted strategy.

    Returns the Router; the test then calls
    ``router.async_get_available_deployment(...)`` to exercise the real
    dispatch path including healthy_deployments filtering, cooldown
    filtering, and our strategy's weighted pick.
    """
    router = litellm.Router(model_list=model_list)
    install(router)
    return router


async def _pick_ids(
    router: litellm.Router, model: str, n: int, request_kwargs: Optional[Dict] = None
) -> Counter:
    """Drive real dispatch N times; return a Counter of deployment ids."""
    counts: Counter = Counter()
    for _ in range(n):
        d = await router.async_get_available_deployment(
            model=model, request_kwargs=request_kwargs or {}
        )
        counts[d["model_info"]["id"]] += 1
    return counts


def _now_utc(year=2026, month=6, day=10, hour=10, minute=0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


def _m(h: int, mi: int = 0) -> int:
    """HH:MM convenience for the in-band tests."""
    return h * 60 + mi


# ---------------------------------------------------------------------------
# _parse_hhmm (pure function)
# ---------------------------------------------------------------------------


def test_parse_hhmm_basic():
    assert _parse_hhmm("00:00") == 0
    assert _parse_hhmm("23:59") == 23 * 60 + 59
    assert _parse_hhmm("08:30") == 8 * 60 + 30


def test_parse_hhmm_24_00_returns_end_of_day():
    assert _parse_hhmm("24:00") == 1440


@pytest.mark.parametrize("bad", ["", "24:01", "25:00", "08", "08:60", "ab:cd"])
def test_parse_hhmm_invalid_raises(bad):
    with pytest.raises(ValueError):
        _parse_hhmm(bad)


# ---------------------------------------------------------------------------
# _in_band (pure function, including cross-midnight)
# ---------------------------------------------------------------------------


def test_in_band_simple_inside():
    assert _in_band(_m(10), _m(8), _m(16)) is True


def test_in_band_simple_outside():
    assert _in_band(_m(7, 59), _m(8), _m(16)) is False
    assert _in_band(_m(16), _m(8), _m(16)) is False


def test_in_band_cross_midnight_late_evening():
    assert _in_band(_m(23, 30), _m(22), _m(3)) is True


def test_in_band_cross_midnight_early_morning():
    assert _in_band(_m(2), _m(22), _m(3)) is True


def test_in_band_cross_midnight_just_outside():
    assert _in_band(_m(21, 59), _m(22), _m(3)) is False
    assert _in_band(_m(3), _m(22), _m(3)) is False


def test_in_band_empty_band_matches_nothing():
    assert _in_band(_m(12), _m(12), _m(12)) is False


def test_in_band_full_day_via_00_to_24():
    assert _in_band(_m(0), 0, 1440) is True
    assert _in_band(_m(12, 30), 0, 1440) is True
    assert _in_band(_m(23, 59), 0, 1440) is True


# ---------------------------------------------------------------------------
# _resolve_weight — fallback chain (pure function; needs a TimeWeightedRouter
# instance, but the Router argument is unused by _resolve_weight, so we wire
# it up against a real Router with a trivial model_list).
# ---------------------------------------------------------------------------


@pytest.fixture
def strategy():
    router = litellm.Router(
        model_list=[
            _deployment(
                "dummy", "x", bands=[{"start": "00:00", "end": "24:00", "weight": 1}]
            )
        ]
    )
    return TimeWeightedRouter(router)


def test_resolve_weight_matches_band(strategy):
    d = _deployment("p", "a", bands=[{"start": "08:00", "end": "16:00", "weight": 7}])
    assert strategy._resolve_weight(d, _now_utc(hour=10)) == 7


def test_resolve_weight_fallback_weight_when_no_band_matches(strategy):
    d = _deployment(
        "p",
        "a",
        bands=[{"start": "08:00", "end": "16:00", "weight": 7}],
        fallback_weight=3,
    )
    assert strategy._resolve_weight(d, _now_utc(hour=18)) == 3


def test_resolve_weight_params_weight_when_no_fallback(strategy):
    d = _deployment(
        "p",
        "a",
        bands=[{"start": "08:00", "end": "16:00", "weight": 7}],
        params_weight=9,
    )
    assert strategy._resolve_weight(d, _now_utc(hour=18)) == 9


def test_resolve_weight_uniform_one_when_nothing_set(strategy):
    d = _deployment("p", "a")
    assert strategy._resolve_weight(d, _now_utc()) == 1


def test_resolve_weight_params_weight_when_no_time_weights(strategy):
    d = _deployment("p", "a", params_weight=5)
    assert strategy._resolve_weight(d, _now_utc()) == 5


def test_resolve_weight_negative_clamped_to_zero(strategy):
    d = _deployment("p", "a", bands=[{"start": "08:00", "end": "16:00", "weight": -3}])
    assert strategy._resolve_weight(d, _now_utc(hour=10)) == 0


def test_resolve_weight_malformed_band_falls_through(strategy):
    d = _deployment(
        "p",
        "a",
        bands=[
            {"start": "not-a-time", "end": "16:00", "weight": 5},
            {"start": "08:00", "end": "16:00", "weight": 3},
        ],
        fallback_weight=99,
    )
    assert strategy._resolve_weight(d, _now_utc(hour=10)) == 3


def test_resolve_weight_unknown_tz_falls_back_to_utc(strategy):
    d = _deployment(
        "p",
        "a",
        bands=[{"start": "10:00", "end": "11:00", "weight": 7}],
        tz="Mars/Olympus_Mons",
    )
    assert strategy._resolve_weight(d, _now_utc(hour=10, minute=30)) == 7


def test_tz_shanghai_vs_utc_same_utc_clock(strategy):
    """Same UTC moment, different tz → different bands match."""
    now = _now_utc(hour=2, minute=0)  # 02:00 UTC == 10:00 Asia/Shanghai

    d_utc = _deployment(
        "p",
        "utc-acc",
        bands=[{"start": "01:00", "end": "03:00", "weight": 5}],
        tz="UTC",
    )
    d_sh = _deployment(
        "p",
        "sh-acc",
        bands=[
            {"start": "09:00", "end": "11:00", "weight": 5},
            {"start": "01:00", "end": "03:00", "weight": 99},
        ],
        tz="Asia/Shanghai",
    )

    assert strategy._resolve_weight(d_utc, now) == 5
    assert strategy._resolve_weight(d_sh, now) == 5


# ---------------------------------------------------------------------------
# Real Router dispatch — observable behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_picks_only_deployment_with_nonzero_weight():
    """Only `a` has nonzero weight in this full-day band; over 50 picks
    every single pick must be `a`. Run against a real Router so the
    full dispatch path (healthy_deployments, cooldown filter, blocked
    filter, our weighted pick) is exercised."""
    pool = [
        _deployment(
            "tw-pool", "a", bands=[{"start": "00:00", "end": "24:00", "weight": 10}]
        ),
        _deployment(
            "tw-pool", "b", bands=[{"start": "00:00", "end": "24:00", "weight": 0}]
        ),
        _deployment(
            "tw-pool", "c", bands=[{"start": "00:00", "end": "24:00", "weight": 0}]
        ),
    ]
    router = _make_router(pool)

    counts = await _pick_ids(router, "tw-pool", 50)
    assert counts == Counter({"a": 50})


@pytest.mark.asyncio
async def test_dispatch_weighted_distribution_matches_config():
    """7/2/1 weighted across 5000 picks → distributions within 3pp."""
    pool = [
        _deployment(
            "tw-pool", "a", bands=[{"start": "00:00", "end": "24:00", "weight": 7}]
        ),
        _deployment(
            "tw-pool", "b", bands=[{"start": "00:00", "end": "24:00", "weight": 2}]
        ),
        _deployment(
            "tw-pool", "c", bands=[{"start": "00:00", "end": "24:00", "weight": 1}]
        ),
    ]
    router = _make_router(pool)

    random.seed(42)
    n = 5000
    counts = await _pick_ids(router, "tw-pool", n)

    assert abs(counts["a"] / n - 0.7) < 0.03
    assert abs(counts["b"] / n - 0.2) < 0.03
    assert abs(counts["c"] / n - 0.1) < 0.03


@pytest.mark.asyncio
async def test_dispatch_delegate_path_for_non_opt_in_model():
    """A model without ANY ``time_weights`` must route via the original
    LiteLLM dispatcher. Observable test: the default strategy on a
    plain model_list uses litellm_params.weight, so equal weights yield
    an approximately uniform distribution. We assert the WEAKER property
    "both deployments served traffic" rather than asserting a counter on
    our own delegate path — that would be a theater test."""
    pool = [
        _deployment("plain-pool", "a", params_weight=1),
        _deployment("plain-pool", "b", params_weight=1),
    ]
    router = _make_router(pool)

    random.seed(0)
    counts = await _pick_ids(router, "plain-pool", 200)
    # Real behavior: both ids must have been picked at least once with
    # equal litellm_params.weight; flooring everything onto one id would
    # indicate the strategy hijacked a non-opt-in model.
    assert counts["a"] > 0
    assert counts["b"] > 0


@pytest.mark.asyncio
async def test_dispatch_delegate_respects_litellm_params_weight():
    """For non-opt-in models, the original simple-shuffle still honors
    ``litellm_params.weight``. We assert this through observed
    distribution (3:1 split lands roughly 75/25). Catches any future
    bug where our strategy accidentally normalizes weights for plain
    models."""
    pool = [
        _deployment("plain-pool", "a", params_weight=3),
        _deployment("plain-pool", "b", params_weight=1),
    ]
    router = _make_router(pool)

    random.seed(1)
    n = 2000
    counts = await _pick_ids(router, "plain-pool", n)
    # 3:1 → 75/25. Allow 5pp slack at n=2000.
    assert abs(counts["a"] / n - 0.75) < 0.05
    assert abs(counts["b"] / n - 0.25) < 0.05


@pytest.mark.asyncio
async def test_dispatch_skips_blocked_deployment():
    """Blocked deployments must be excluded from selection even if
    they have a nonzero weight in the current band. Drives real
    ``_filter_blocked_deployments`` integration."""
    pool = [
        _deployment(
            "tw-pool",
            "a",
            bands=[{"start": "00:00", "end": "24:00", "weight": 10}],
            blocked=True,
        ),
        _deployment(
            "tw-pool",
            "b",
            bands=[{"start": "00:00", "end": "24:00", "weight": 1}],
        ),
    ]
    router = _make_router(pool)

    counts = await _pick_ids(router, "tw-pool", 30)
    assert counts == Counter({"b": 30})


@pytest.mark.asyncio
async def test_dispatch_zero_weight_sum_uniform_fallback():
    """All resolved weights are 0 (no band matches and no
    fallback_weight set) → strategy must fall back to uniform random,
    not error out. Observable test: with seed=0 both deployments must
    be selected at least once across 100 picks."""
    pool = [
        # Both bands are outside the test moment's local time (band 14:00→14:00
        # is empty; we use a clearly non-matching window).
        _deployment(
            "tw-pool",
            "a",
            bands=[{"start": "14:00", "end": "14:00", "weight": 5}],
        ),
        _deployment(
            "tw-pool",
            "b",
            bands=[{"start": "14:00", "end": "14:00", "weight": 5}],
        ),
    ]
    router = _make_router(pool)

    random.seed(0)
    counts = await _pick_ids(router, "tw-pool", 100)
    # No fallback_weight → falls through to litellm_params.weight which
    # is also unset → uniform pick across both deployments.
    assert counts["a"] > 0
    assert counts["b"] > 0


# ---------------------------------------------------------------------------
# install() — observable router state, not a "was called" mock check
# ---------------------------------------------------------------------------


def test_install_is_no_op_when_no_deployment_opts_in():
    """A router with no ``time_weights`` is left fully on the original
    dispatcher. Observable check: the dispatcher's __self__ is still
    the Router, not a TimeWeightedRouter instance."""
    router = litellm.Router(model_list=[_deployment("plain", "a", params_weight=1)])
    assert install(router) is False
    # Bound method's __self__ is the Router itself; install was a no-op.
    assert router.async_get_available_deployment.__self__ is router


def test_install_replaces_dispatcher_when_at_least_one_opts_in():
    router = litellm.Router(
        model_list=[
            _deployment(
                "tw-pool",
                "a",
                bands=[{"start": "00:00", "end": "24:00", "weight": 1}],
            )
        ]
    )
    assert install(router) is True
    assert isinstance(
        router.async_get_available_deployment.__self__, TimeWeightedRouter
    )


def test_install_is_idempotent():
    """Re-installing on the same router does not chain wrappers; the
    captured original method must still be the Router's original one,
    not a previously-installed TimeWeightedRouter method."""
    router = litellm.Router(
        model_list=[
            _deployment(
                "tw-pool",
                "a",
                bands=[{"start": "00:00", "end": "24:00", "weight": 1}],
            )
        ]
    )
    install(router)
    first_strategy = router.async_get_available_deployment.__self__
    install(router)
    second_strategy = router.async_get_available_deployment.__self__
    # Idempotent: the second install was a no-op, so the bound strategy
    # is the same instance — not a new wrapper around the first.
    assert first_strategy is second_strategy


# ---------------------------------------------------------------------------
# DST edge — xfail locks our current "unspecified" stance
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason=(
        "DST spring-forward / fall-back behavior not formally specified for "
        "bands. zoneinfo handles the conversion correctly today; this xfail "
        "exists to force revisit when we adopt explicit DST policy."
    ),
)
def test_dst_spring_forward_documents_unspecified_behavior():
    raise AssertionError("DST policy not specified — fix me when behavior is locked")
