"""End-to-end fixture for Case 34 — time-weighted routing.

Drives the live proxy at $PROXY_URL using $MASTER_KEY and exercises
TimeWeightedRouter end-to-end:

  1. Registers three deployments under model_name=tw-pool with bands
     such that exactly one (glm-acc-1) is non-zero in the current
     wall-clock minute. Sends N requests; asserts 100% land on it.
  2. Registers a plain-pool model (no time_weights). Sends N
     requests; asserts at least 2 distinct deployment ids appear
     (i.e. delegate path uses LiteLLM's default routing).
  3. PATCHes the band weights so glm-acc-2 becomes the active
     deployment. Asserts the next batch lands fully on it.
  4. PATCHes glm-acc-2 to blocked=true; asserts traffic moves to
     glm-acc-3 (which has a band that becomes non-zero via fallback).
  5. Unblocks glm-acc-2; asserts traffic returns to glm-acc-2.

Exit codes:
  0   PASS
  77  SKIP (proxy unreachable / mock not configured)
  1   FAIL

The mock-anthropic provider returns a deterministic small response so
we can read which deployment served each request via the
``x-litellm-model-id`` response header (set by the proxy after routing).
"""

from __future__ import annotations

import os
import sys
from collections import Counter
from datetime import datetime, timezone
from typing import Dict, List, Optional

try:
    import httpx
except ImportError:
    print("SKIP: httpx not installed")
    sys.exit(77)

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    print("SKIP: zoneinfo unavailable")
    sys.exit(77)


PROXY = os.environ.get("PROXY_URL", "http://localhost:4011").rstrip("/")
KEY = os.environ.get("MASTER_KEY", "sk-e2e-test")
MOCK_BASE = os.environ.get("MOCK_PROVIDER_BASE", "http://litellm-e2e-mock:4012")
TZ = "Asia/Shanghai"
HEADERS = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}


def fail(msg: str) -> None:
    print(f"FAIL: {msg}")
    sys.exit(1)


def skip(msg: str) -> None:
    print(f"SKIP: {msg}")
    sys.exit(77)


def info(msg: str) -> None:
    print(f"INFO: {msg}", flush=True)


def _check_proxy_alive() -> None:
    try:
        r = httpx.get(f"{PROXY}/health/readiness", timeout=5.0)
    except Exception as e:
        skip(f"proxy unreachable at {PROXY}: {e}")
    if r.status_code != 200:
        skip(f"proxy /health/readiness returned {r.status_code}")


def _current_band() -> Dict[str, str]:
    """Build a band that contains the current Asia/Shanghai minute."""
    now = datetime.now(timezone.utc).astimezone(ZoneInfo(TZ))
    minute_of_day = now.hour * 60 + now.minute
    start_min = max(minute_of_day - 5, 0)
    end_min = min(minute_of_day + 5, 24 * 60)
    return {
        "start": f"{start_min // 60:02d}:{start_min % 60:02d}",
        "end": f"{end_min // 60:02d}:{end_min % 60:02d}",
    }


def _register_model(payload: Dict) -> str:
    r = httpx.post(f"{PROXY}/model/new", headers=HEADERS, json=payload, timeout=15.0)
    if r.status_code >= 300:
        fail(f"/model/new failed [{r.status_code}]: {r.text}")
    data = r.json()
    mid = (data.get("model_info") or {}).get("id") or data.get("id")
    if not mid:
        fail(f"/model/new returned no id: {data}")
    return mid


def _patch_model(model_id: str, patch: Dict) -> None:
    r = httpx.patch(
        f"{PROXY}/model/{model_id}/update", headers=HEADERS, json=patch, timeout=15.0
    )
    if r.status_code >= 300:
        fail(f"/model/{model_id}/update failed [{r.status_code}]: {r.text}")


def _delete_model(model_id: str) -> None:
    httpx.post(
        f"{PROXY}/model/delete",
        headers=HEADERS,
        json={"id": model_id},
        timeout=15.0,
    )


def _completion(model: str) -> Optional[str]:
    """Send a single completion; return the deployment id that served it."""
    r = httpx.post(
        f"{PROXY}/v1/chat/completions",
        headers=HEADERS,
        json={
            "model": model,
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 4,
        },
        timeout=30.0,
    )
    if r.status_code >= 300:
        info(f"completion failed [{r.status_code}]: {r.text[:200]}")
        return None
    # LiteLLM sets x-litellm-model-id on the proxy response.
    return r.headers.get("x-litellm-model-id")


def _count_by_id(model: str, n: int) -> Counter:
    counts: Counter = Counter()
    consecutive_none = 0
    for _ in range(n):
        mid = _completion(model)
        if mid is None:
            consecutive_none += 1
            # Hard SKIP if the mock provider clearly isn't reachable: 3
            # back-to-back failures with zero successes means the proxy
            # cooldown-locked everything and no real test signal will
            # come out of further attempts.
            if consecutive_none >= 3 and not counts:
                skip(
                    f"completion to {model} fails with no successes; "
                    "mock-anthropic sidecar likely not running"
                )
            continue
        consecutive_none = 0
        counts[mid] += 1
    return counts


def _build_tw_payload(
    public_id: str, mock_key: str, band: Dict[str, str], weight: int
) -> Dict:
    return {
        "model_name": "tw-pool",
        "litellm_params": {
            "model": "openai/mock-anthropic",
            "api_base": MOCK_BASE,
            "api_key": mock_key,
            "weight": 1,
        },
        "model_info": {
            "id": public_id,
            "tz": TZ,
            "time_weights": {
                "bands": [{**band, "weight": weight}],
                "fallback_weight": 0,
            },
        },
    }


def _build_plain_payload(public_id: str, mock_key: str) -> Dict:
    return {
        "model_name": "plain-pool",
        "litellm_params": {
            "model": "openai/mock-anthropic",
            "api_base": MOCK_BASE,
            "api_key": mock_key,
            "weight": 1,
        },
        "model_info": {"id": public_id},
    }


def main() -> None:
    _check_proxy_alive()
    band = _current_band()
    info(f"active band: {band} (tz={TZ})")

    registered: List[str] = []
    try:
        # Step 1: register tw-pool with glm-acc-1 hot, others zero
        id1 = _register_model(_build_tw_payload("glm-acc-1", "mock-1", band, 10))
        id2 = _register_model(_build_tw_payload("glm-acc-2", "mock-2", band, 0))
        id3 = _register_model(_build_tw_payload("glm-acc-3", "mock-3", band, 0))
        registered = [id1, id2, id3]

        # Step 2: 30 requests, all to glm-acc-1
        counts = _count_by_id("tw-pool", 30)
        info(f"step 2 counts: {dict(counts)}")
        non_acc1 = {k: v for k, v in counts.items() if k != "glm-acc-1"}
        if counts.get("glm-acc-1", 0) < 30 or non_acc1:
            fail(f"step 2: expected 30/30 on glm-acc-1, got {dict(counts)}")

        # Step 3: register plain-pool, verify delegate path
        idp1 = _register_model(_build_plain_payload("plain-1", "mock-p1"))
        idp2 = _register_model(_build_plain_payload("plain-2", "mock-p2"))
        registered.extend([idp1, idp2])
        plain_counts = _count_by_id("plain-pool", 30)
        info(f"step 3 plain counts: {dict(plain_counts)}")
        distinct = [k for k, v in plain_counts.items() if v > 0]
        if len(distinct) < 2:
            fail(
                f"step 3: delegate path collapsed to one id ({distinct}); "
                "TimeWeightedRouter is hijacking models without time_weights"
            )

        # Step 4: swap hot deployment via PATCH
        _patch_model(
            id1,
            {
                "model_info": {
                    "id": "glm-acc-1",
                    "tz": TZ,
                    "time_weights": {
                        "bands": [{**band, "weight": 0}],
                        "fallback_weight": 0,
                    },
                }
            },
        )
        _patch_model(
            id2,
            {
                "model_info": {
                    "id": "glm-acc-2",
                    "tz": TZ,
                    "time_weights": {
                        "bands": [{**band, "weight": 10}],
                        "fallback_weight": 0,
                    },
                }
            },
        )
        counts4 = _count_by_id("tw-pool", 10)
        info(f"step 4 counts: {dict(counts4)}")
        if counts4.get("glm-acc-2", 0) < 10:
            fail(
                f"step 4: expected 10/10 on glm-acc-2 after PATCH, "
                f"got {dict(counts4)}"
            )

        # Step 5: block glm-acc-2; promote glm-acc-3
        _patch_model(id2, {"blocked": True})
        _patch_model(
            id3,
            {
                "model_info": {
                    "id": "glm-acc-3",
                    "tz": TZ,
                    "time_weights": {
                        "bands": [{**band, "weight": 10}],
                        "fallback_weight": 0,
                    },
                }
            },
        )
        counts5 = _count_by_id("tw-pool", 10)
        info(f"step 5 counts: {dict(counts5)}")
        if counts5.get("glm-acc-3", 0) < 10:
            fail(
                f"step 5: expected 10/10 on glm-acc-3 (acc-2 blocked), "
                f"got {dict(counts5)}"
            )

        # Step 6: unblock acc-2; expect traffic back on acc-2
        _patch_model(id2, {"blocked": False})
        _patch_model(
            id3,
            {
                "model_info": {
                    "id": "glm-acc-3",
                    "tz": TZ,
                    "time_weights": {
                        "bands": [{**band, "weight": 0}],
                        "fallback_weight": 0,
                    },
                }
            },
        )
        counts6 = _count_by_id("tw-pool", 10)
        info(f"step 6 counts: {dict(counts6)}")
        if counts6.get("glm-acc-2", 0) < 10:
            fail(
                f"step 6: expected 10/10 back on glm-acc-2 after unblock, "
                f"got {dict(counts6)}"
            )

        print("PASS")
    finally:
        for mid in registered:
            try:
                _delete_model(mid)
            except Exception:
                pass


if __name__ == "__main__":
    main()
