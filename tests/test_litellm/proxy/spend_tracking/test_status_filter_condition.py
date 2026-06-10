"""
Tests for ``_build_status_filter_condition`` — the Prisma WHERE-clause
builder behind the SpendLogs UI ``status_filter`` query param.

The status filter is genuinely binary: ``success | failure``.
Cancellations are NOT a top-level filter category — they carry
``status='success'`` plus a ``cancellation_indicator`` marker, and
surface in the UI as a row-level amber badge. A user who needs to
query cancelled rows specifically filters on the marker directly in
SQL.

These assertions guard against:
1. Re-introducing JSON-path filters that Prisma's Python client
   doesn't support (the cause of the 500 we hit on the first
   refactor attempt).
2. Silently accepting unknown filter values (e.g. ``success_partial``
   from a stale UI bundle) instead of falling through to "no filter".
"""

import os
import sys

sys.path.insert(0, os.path.abspath("../../../.."))

from litellm.proxy.spend_tracking.spend_management_endpoints import (
    _build_status_filter_condition,
)


class TestStatusFilterCondition:
    def test_none_returns_empty(self):
        assert _build_status_filter_condition(None) == {}

    def test_unknown_value_returns_empty(self):
        # Defensive: an unrecognised string from a stale UI build or a
        # typo'd curl shouldn't accidentally match everything or fall
        # through to a generic `status=<input>` clause that returns
        # zero rows silently.
        assert _build_status_filter_condition("nonsense") == {}

    def test_success_matches_status_column_with_null_tolerance(self):
        # status='success' OR NULL (legacy rows pre-date the status
        # column being populated). Critically: no JSON-path filter on
        # metadata — the Prisma Python client doesn't support that
        # form and 500s at request time.
        cond = _build_status_filter_condition("success")
        assert cond == {
            "OR": [
                {"status": {"equals": "success"}},
                {"status": None},
            ]
        }

    def test_failure_matches_status_column(self):
        assert _build_status_filter_condition("failure") == {
            "status": {"equals": "failure"}
        }

    def test_cancel_is_not_a_top_level_filter(self):
        # The "Cancel" value is intentionally NOT a recognised filter
        # — cancellations live inside the Success bucket and are
        # surfaced by the UI as a row-level badge, not a filter tab.
        # An incoming `status_filter=cancel` falls through to the
        # empty-dict default (no filter applied).
        assert _build_status_filter_condition("cancel") == {}

    def test_success_partial_is_not_recognised(self):
        # The legacy three-valued taxonomy never shipped; the
        # success_partial sentinel must not be silently accepted as a
        # valid filter value (it would mask typos and stale UI bundles).
        assert _build_status_filter_condition("success_partial") == {}
