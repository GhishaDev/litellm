"""Time-banded weighted routing for deployment pools.

Routes traffic across deployments of the same ``model_name`` using
weights that vary by time of day. Each deployment declares its own
``model_info.time_weights`` schedule and ``model_info.tz`` timezone;
the router resolves the current weight per deployment at request time
and performs a weighted random pick.

Configuration shape (per deployment) in ``config.yaml``::

    - model_name: glm-pool
      litellm_params:
        model: openai/glm-4.6
        api_key: os.environ/GLM1_KEY
        weight: 1                       # final fallback
      model_info:
        id: glm-account-1
        tz: Asia/Shanghai               # IANA timezone
        time_weights:
          bands:
            - { start: "01:00", end: "08:00", weight: 5 }
            - { start: "08:00", end: "16:00", weight: 2 }
            - { start: "22:00", end: "03:00", weight: 3 }  # crosses midnight
          fallback_weight: 1            # when no band matches

Weight resolution chain per deployment (first non-None wins):
  1. ``time_weights.bands[<current band>].weight``
  2. ``time_weights.fallback_weight``
  3. ``litellm_params.weight``
  4. ``1`` (uniform)

Only deployments under a ``model_name`` where at least one entry has
``time_weights`` configured are routed through this strategy; the
others delegate to the Router's original dispatcher and behave as if
this strategy were never installed.

Healthy-deployment filtering (cooldown, blocked, order, encrypted
content affinity) is delegated to ``Router.async_get_healthy_deployments``
so this strategy composes with all upstream gating.
"""

import random
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Union

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except (
    ImportError
):  # pragma: no cover - Python 3.8 fallback (not supported in this fork)
    from backports.zoneinfo import ZoneInfo, ZoneInfoNotFoundError  # type: ignore

from litellm._logging import verbose_router_logger
from litellm.litellm_core_utils.core_helpers import _get_parent_otel_span_from_kwargs
from litellm.types.router import CustomRoutingStrategyBase

if TYPE_CHECKING:
    from litellm.router import Router as _Router

    LitellmRouter = _Router
else:
    LitellmRouter = Any


# Internal time representation is "minutes since midnight" (0..1440).
# 1440 represents end-of-day so config authors can write `end: "24:00"`
# for a band that runs to midnight. `datetime.time` cannot represent this
# (it caps at 23:59) which is why we use ints instead.
_MIN_PER_DAY = 24 * 60


def _parse_hhmm(s: str) -> int:
    """Parse ``HH:MM`` into minutes since midnight (0..1440).

    ``"24:00"`` returns ``1440`` so bands can run to midnight without being
    treated as the empty interval ``[00:00, 00:00)``.
    """
    parts = s.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"time_weights: invalid HH:MM literal {s!r}")
    hh, mm = int(parts[0]), int(parts[1])
    if hh == 24 and mm == 0:
        return _MIN_PER_DAY
    if not (0 <= hh < 24 and 0 <= mm < 60):
        raise ValueError(f"time_weights: out-of-range HH:MM {s!r}")
    return hh * 60 + mm


def _in_band(now_min: int, start_min: int, end_min: int) -> bool:
    """``True`` when ``now_min`` lies in ``[start_min, end_min)``.

    When ``end_min <= start_min`` the band is interpreted as crossing
    midnight, so e.g. ``22:00 -> 03:00`` matches both ``23:30`` and
    ``02:00``. A degenerate ``start_min == end_min`` matches nothing
    (empty band). ``end_min == 1440`` is treated as "until midnight"
    via the standard ``<`` comparison.
    """
    if start_min == end_min:
        return False
    if start_min < end_min:
        return start_min <= now_min < end_min
    return now_min >= start_min or now_min < end_min


class TimeWeightedRouter(CustomRoutingStrategyBase):
    """Custom routing strategy: per-deployment time-banded weights.

    Install via::

        strategy = TimeWeightedRouter(llm_router)
        llm_router.set_custom_routing_strategy(strategy)

    The constructor MUST run before ``set_custom_routing_strategy`` so
    the strategy can capture references to the Router's original
    dispatcher methods. After installation, the dispatcher delegates to
    those originals for any model whose deployments do not declare
    ``time_weights``.
    """

    @classmethod
    def is_needed(cls, router: LitellmRouter) -> bool:
        """Return True if any deployment in ``router.model_list`` has ``time_weights``.

        Used by callers to skip installation entirely when no model in
        the current config opts in, avoiding the per-request delegation
        overhead.
        """
        for d in router.model_list or []:
            info = d.get("model_info") or {}
            tw = info.get("time_weights")
            if tw and tw.get("bands"):
                return True
        return False

    def __init__(self, router: LitellmRouter):
        self.router = router
        self._original_async_get = router.async_get_available_deployment
        self._original_sync_get = router.get_available_deployment

    def _model_uses_time_weights(self, model: str) -> bool:
        """Return True iff at least one deployment under ``model`` has bands.

        Uses ``router.get_model_list`` to be wildcard-aware (same lookup
        used by the order-based fallback handler).
        """
        deployments = self.router.get_model_list(model_name=model) or []
        for d in deployments:
            info = d.get("model_info") or {}
            tw = info.get("time_weights")
            if tw and tw.get("bands"):
                return True
        return False

    def _resolve_weight(self, deployment: Dict, now_utc: datetime) -> int:
        """Compute the current routing weight for a single deployment.

        Order:
          1. matching band weight
          2. fallback_weight
          3. litellm_params.weight
          4. 1 (uniform fallback)
        """
        info = deployment.get("model_info") or {}
        tw = info.get("time_weights") or {}
        bands = tw.get("bands") or []

        if bands:
            tz_name = info.get("tz") or "UTC"
            try:
                tz = ZoneInfo(tz_name)
            except ZoneInfoNotFoundError:
                verbose_router_logger.warning(
                    "time_weights: unknown tz %r on deployment %s; falling back to UTC",
                    tz_name,
                    info.get("id"),
                )
                tz = timezone.utc

            local_now = now_utc.astimezone(tz)
            now_min = local_now.hour * 60 + local_now.minute
            for b in bands:
                try:
                    start = _parse_hhmm(b["start"])
                    end = _parse_hhmm(b["end"])
                    w = int(b["weight"])
                except (KeyError, ValueError, TypeError) as e:
                    verbose_router_logger.warning(
                        "time_weights: malformed band %r on deployment %s: %s",
                        b,
                        info.get("id"),
                        e,
                    )
                    continue
                if _in_band(now_min, start, end):
                    return max(w, 0)

            fb = tw.get("fallback_weight")
            if fb is not None:
                try:
                    return max(int(fb), 0)
                except (ValueError, TypeError):
                    verbose_router_logger.warning(
                        "time_weights: malformed fallback_weight %r on deployment %s",
                        fb,
                        info.get("id"),
                    )

        params = deployment.get("litellm_params") or {}
        try:
            w = params.get("weight")
            if w is not None:
                return max(int(w), 0)
        except (ValueError, TypeError):
            pass
        return 1

    def _weighted_pick(self, deployments: List[Dict], model: str) -> Dict:
        """Select one deployment from ``deployments`` via weighted random.

        Falls back to uniform random if the resolved weights sum to zero
        (e.g. configured weights all zero, or all deployments fell
        through every fallback rung).
        """
        now_utc = datetime.now(timezone.utc)
        weights = [self._resolve_weight(d, now_utc) for d in deployments]
        total = sum(weights)
        if total <= 0:
            verbose_router_logger.debug(
                "time_weights: resolved weights sum to 0 for model=%s; uniform pick",
                model,
            )
            return random.choice(deployments)
        selected = random.choices(deployments, weights=weights, k=1)[0]
        verbose_router_logger.info(
            "time_weights: model=%s weights=%s selected=%s",
            model,
            weights,
            (selected.get("model_info") or {}).get("id"),
        )
        return selected

    async def async_get_available_deployment(
        self,
        model: str,
        messages: Optional[List[Dict[str, str]]] = None,
        input: Optional[Union[str, List]] = None,
        specific_deployment: Optional[bool] = False,
        request_kwargs: Optional[Dict] = None,
    ):
        if not self._model_uses_time_weights(model):
            return await self._original_async_get(
                model=model,
                messages=messages,
                input=input,
                specific_deployment=specific_deployment,
                request_kwargs=request_kwargs or {},
            )

        healthy = await self.router.async_get_healthy_deployments(
            model=model,
            request_kwargs=request_kwargs or {},
            messages=messages,
            input=input,
            specific_deployment=specific_deployment,
            parent_otel_span=_get_parent_otel_span_from_kwargs(request_kwargs or {}),
        )
        if isinstance(healthy, dict):
            # Router already locked onto a single deployment (specific_deployment
            # or encrypted-content-affinity pinning); pass it through.
            return healthy
        if not healthy:
            # No healthy deployment left under this model_name. Delegate
            # back to the original dispatcher so Router's fallback /
            # order-based escalation chain takes over rather than
            # silently returning None.
            return await self._original_async_get(
                model=model,
                messages=messages,
                input=input,
                specific_deployment=specific_deployment,
                request_kwargs=request_kwargs or {},
            )
        return self._weighted_pick(healthy, model)

    def get_available_deployment(
        self,
        model: str,
        messages: Optional[List[Dict[str, str]]] = None,
        input: Optional[Union[str, List]] = None,
        specific_deployment: Optional[bool] = False,
        request_kwargs: Optional[Dict] = None,
    ):
        if not self._model_uses_time_weights(model):
            return self._original_sync_get(
                model=model,
                messages=messages,
                input=input,
                specific_deployment=specific_deployment,
                request_kwargs=request_kwargs or {},
            )

        # Sync path: use Router's sync _common_checks then apply our weighting.
        # We deliberately re-use the original dispatcher up to the point of
        # selecting from healthy deployments. For pools that opt in to
        # time-weighted routing the sync path is rarely hit (proxy is async),
        # but we keep it correct for callers that drive the sync Router.
        _, healthy = self.router._common_checks_available_deployment(
            model=model,
            messages=messages,
            input=input,
            specific_deployment=specific_deployment,
            request_kwargs=request_kwargs,
        )
        if isinstance(healthy, dict):
            return healthy
        cooldown_ids = self.router._get_cooldown_deployments(
            parent_otel_span=_get_parent_otel_span_from_kwargs(request_kwargs or {}),
        )
        healthy = self.router._filter_cooldown_deployments(healthy, cooldown_ids)
        healthy = self.router._filter_blocked_deployments(healthy)
        if not healthy:
            return self._original_sync_get(
                model=model,
                messages=messages,
                input=input,
                specific_deployment=specific_deployment,
                request_kwargs=request_kwargs or {},
            )
        return self._weighted_pick(healthy, model)


def install(router: LitellmRouter) -> bool:
    """Install ``TimeWeightedRouter`` on ``router`` if any model opts in.

    Idempotent: detects an already-installed instance via the dispatcher's
    ``__self__`` and skips reinstall. Returns True when the strategy is
    active on the router (either freshly installed or already present).
    """
    if not TimeWeightedRouter.is_needed(router):
        return False

    existing_self = getattr(router.async_get_available_deployment, "__self__", None)
    if isinstance(existing_self, TimeWeightedRouter):
        return True

    strategy = TimeWeightedRouter(router)
    router.set_custom_routing_strategy(strategy)
    verbose_router_logger.info(
        "TimeWeightedRouter installed (deployments with time_weights detected)"
    )
    return True
